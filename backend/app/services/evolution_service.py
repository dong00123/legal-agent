"""Skill 自进化飞轮：L0 观测 → L5 治理的编排门面。

对应「Skill 自进化飞轮」图的主链：

===========  ==========================================================
L0 数据观测  轨迹采集（trace store）· verdict 标注 · 特征工程 · 窗口聚合
L1 健康诊断  四维画像 · 成功基线建模 · PSI/MMD/KL 三检验漂移 · 先行退化信号
L2 进化决策  四目标帕累托前沿 · 预算补足 K=3 · Thompson 采样 bandit 定序
L3 进化执行  失败场景切片 · 失败聚类 · 根因分析 · 最小可审计补丁
L4 验证守门  同基线回放 A/B · 指标门禁 · 影子→小流量→全量灰度 · 版本账本
L5 状态治理  状态刷新 · results.tsv 落盘 · 元控制预算 · 安全红线与熔断
===========  ==========================================================

**本模块只做编排与持久化**，纯计算分别落在同目录的
``evolution_features`` / ``evolution_drift`` / ``evolution_execution`` /
``evolution_gate`` 四个模块里。这样"算得对不对"和"有没有写进生产状态"
是两件可以分别验证的事 —— 也是审"进化系统本身"是否可信时的第一刀。

三条贯穿全链的纪律：

1. **进化不能改代码。** 所有产出都是**配置补丁工件**（``.patch.json``），
   落在 ``data/evolution/artifacts/``，由人 Keep 之后才写进版本账本。
2. **平台期不折腾。** 没有健康信号就不产生候选（``candidates`` 返回空），
   宁可这周不进化，也不为了"每周都有产出"去动一个本来正常的 Skill。
3. **冻结优先于一切。** ``freeze()`` 之后所有写操作一律拒绝，**不降级放行** ——
   熔断的意义就在于它不给自己留例外。
"""

from __future__ import annotations

import json
import random
import threading
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from backend.app.config import DATA_DIR
from backend.app.services import evolution_drift as drift_mod
from backend.app.services import evolution_embedding as evolution_embedding
from backend.app.services import evolution_execution as execution
from backend.app.services import evolution_features as features_mod
from backend.app.services import evolution_gate as gate
from backend.skills.capabilities import CAPABILITY_SKILLS

# 状态文件结构版本。v3 = 加入熔断、灰度、预算三块状态。
STATE_VERSION = 3
MAX_OBSERVATIONS = 5000
WINDOW_SIZE = 100
COOLDOWN_DAYS = 7

# 三阶段灰度：(阶段名, 流量百分比)。影子模式为 0%，候选只旁路跑。
GRAY_STAGES = gate.GRAY_STAGES
GRAY_PERCENTS = tuple(percent for _, percent in gate.GRAY_STAGES)

# 分布漂移（TV 距离）的最小样本量，取自 drift 模块以保持单一口径。
MIN_DRIFT_SAMPLES = drift_mod.MIN_DRIFT_SAMPLES

# feature store 每个 Skill 保留最近多少个窗口快照。
FEATURE_WINDOW_LIMIT = 12

# 先行退化信号的 staleness 判据（与 drift 模块同源）。
STALENESS_DAYS = drift_mod.STALENESS_DAYS

# 元控制：每周期的进化预算配额。超了就直接拒绝创建任务 ——
# "这周名额用完了"是可见的、可解释的，而"悄悄多跑几个"不是。
DEFAULT_BUDGET = {
    "max_tasks_per_cycle": 5,
    "max_tokens_per_cycle": 1_000_000,
    "max_runs_per_task": 100,
}

VALID_VERDICTS = ("pass", "fail", "degraded", "unknown")

# 这些状态下不允许 Keep：要么压根没通过门禁，要么线上实测已经证伪。
KEEP_BLOCKED_STATUSES = frozenset({"validation_failed", "patch_rejected", "rolled_back", "reverted"})

# 事前漂移预演允许的基线偏移上限。超过它，说明候选是在一个**已经过期**的
# 基线上验证的 —— 那份"同基线回放"的结论不再可信，拿它开灰度等于用过期凭据开门。
BASELINE_DRIFT_TOLERANCE = 0.15

# bandit 后验的周期衰减系数（图 L5「后验更新」）。
# 0.85 相当于证据半衰期约 4 个周期：让新证据占主导，又不把历史一笔勾销。
# 没有这一步，半年前一次偶然的成功会永久抬高某个补丁类型的中选概率，
# 而 Skill 的输入分布早就变了。
BANDIT_DECAY_FACTOR = 0.85

# HITL 抽查比例：灰度走到全量后按此概率进入人工抽查队列。0 = 不抽。
# 取 0.3 而不是 1.0：全量候选每周都有，逐条人工核会让队列迅速积压，
# 而积压的抽查队列和没有抽查是一回事（没人看）。
HITL_REVIEW_RATE = 0.3


def _parse_iso(value: Any) -> datetime | None:
    """把 ``_now()`` 写出的 ISO 串解析回带时区的 datetime；解析不了返回 ``None``。

    调度到期判定必须有这一步兜底：历史状态里 ``next_run_at`` 可能是 ``None``
    （老文件没有这个键），也可能被人手工改坏。两种情况都该"当作没有记录"，
    而不是让调度线程抛异常死掉。
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class EvolutionService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._path = Path(DATA_DIR) / "evolution" / "state.json"

    # ------------------------------------------------------------ 路径
    # 全部**由 ``self._path`` 推导**而不是在 ``__init__`` 里固定：
    # 测试改 ``service._path`` 指向 tmp 目录时，工件、特征、账本必须一起搬过去，
    # 否则用例会往真实的 data/evolution 里写东西（跑一次测试污染一次生产状态）。
    @property
    def _root(self) -> Path:
        return self._path.parent

    @property
    def _artifact_dir(self) -> Path:
        return self._root / "artifacts"

    @property
    def _feature_dir(self) -> Path:
        return self._root / "features"

    @property
    def _trace_dir(self) -> Path:
        return self._root / "traces"

    @property
    def _results_path(self) -> Path:
        return self._root / "results.tsv"

    @property
    def _ledger(self) -> gate.VersionLedger:
        return gate.VersionLedger(self._root / "versions.jsonl")

    def _ensure_dirs(self) -> None:
        for path in (self._root, self._artifact_dir, self._feature_dir, self._trace_dir):
            path.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ 状态读写
    def _load(self) -> dict[str, Any]:
        try:
            state = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            state = {}
        # 向后兼容旧文件：升级时不丢历史观测与审批记录。
        state.setdefault("version", STATE_VERSION)
        state.setdefault("observations", [])
        state.setdefault("baselines", {})
        state.setdefault("tasks", [])
        state.setdefault("audit", [])
        state.setdefault("active_versions", {})
        state.setdefault("feature_snapshots", [])
        state.setdefault("cycles", [])
        state.setdefault("bandit", {})
        state.setdefault("rollouts", {})
        state.setdefault("failures", [])
        state.setdefault("reviews", [])
        state.setdefault("budget", dict(DEFAULT_BUDGET))
        state.setdefault("frozen", {"frozen": False, "reason": "", "at": None, "by": None})
        state.setdefault("scheduler", {"enabled": True, "interval_days": 7, "last_run_at": None})
        # ``next_run_at`` 是周期调度器的**下次到期时间**，持久化而不是放内存变量：
        # 否则每次重启都会把计时清零，"每 7 天一次"在频繁重启 / 单机多副本下永远等不到。
        state["scheduler"].setdefault("next_run_at", None)
        state["version"] = STATE_VERSION
        return state

    def _save(self, state: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def state_signature(self) -> str:
        """状态文件 ``(mtime_ns, size)`` 签名，供上层做读缓存失效判据。

        只看文件元数据、不读内容 —— 要回答的是"变了没有"，不是"变成了什么"。
        任何进程（含 worker）写入 state.json 都会改变签名，所以跨进程写同样
        能让缓存失效，不必依赖"每个写接口记得来清缓存"。
        """
        try:
            st = self._path.stat()
        except OSError:
            return "0:0"
        return f"{st.st_mtime_ns}:{st.st_size}"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    # ------------------------------------------------------------ 熔断
    def _assert_writable(self, state: dict[str, Any]) -> None:
        """安全红线：熔断期间一切写操作拒绝。"""
        frozen = state.get("frozen") or {}
        if frozen.get("frozen"):
            raise ValueError(
                f"自进化已熔断（原因：{frozen.get('reason') or '未注明'}），"
                f"请先解除熔断再操作。"
            )

    def freeze(self, reason: str, *, user_id: int | None = None) -> dict[str, Any]:
        """人工一键熔断（图 L5「人工一键熔断」）。"""
        with self._lock:
            state = self._load()
            state["frozen"] = {"frozen": True, "reason": str(reason or "人工熔断"),
                               "at": self._now(), "by": user_id}
            state["audit"].append({"action": "freeze", "reason": state["frozen"]["reason"],
                                   "created_at": state["frozen"]["at"], "created_by": user_id})
            self._save(state)
            return state["frozen"]

    def unfreeze(self, *, user_id: int | None = None) -> dict[str, Any]:
        with self._lock:
            state = self._load()
            state["frozen"] = {"frozen": False, "reason": "", "at": self._now(), "by": user_id}
            state["audit"].append({"action": "unfreeze", "created_at": state["frozen"]["at"],
                                   "created_by": user_id})
            self._save(state)
            return state["frozen"]

    # ============================================================ L0 数据观测层
    def observe(self, payload: dict[str, Any], *, user_id: int | None = None) -> dict[str, Any]:
        """记录一条 Skill 观测。

        ``confidence`` 是 verdict 标注的置信度（图 L0「按置信度加权评分」）：
        默认 1.0，即"标注可信"。带权重的成功率让"高置信的失败"比"低置信的
        失败"更能把健康分拉下来，而不是一条样本一票。
        """
        skill = str(payload.get("skill_name") or "").strip()
        if not skill:
            raise ValueError("skill_name 不能为空")
        verdict = str(payload.get("verdict") or "unknown").lower()
        if verdict not in VALID_VERDICTS:
            raise ValueError("verdict 必须是 pass、fail、degraded 或 unknown")
        item = {
            "observation_id": "obs_" + uuid.uuid4().hex[:12],
            "skill_name": skill,
            "version": str(payload.get("version") or "unknown"),
            "verdict": verdict,
            "confidence": _bounded(payload.get("confidence"), default=1.0),
            "latency_ms": max(0.0, float(payload.get("latency_ms") or 0)),
            "trace_id": str(payload.get("trace_id") or ""),
            "task_id": str(payload.get("task_id") or ""),
            "error_type": str(payload.get("error_type") or ""),
            "coverage": _bounded(payload.get("coverage"), default=None),
            "freshness_days": _non_negative(payload.get("freshness_days"), default=None),
            "last_gain": _bounded(payload.get("last_gain"), default=None),
            "tool_calls": max(0, int(payload.get("tool_calls") or 0)),
            "created_at": self._now(),
            "user_id": user_id,
        }
        with self._lock:
            state = self._load()
            self._assert_writable(state)
            state["observations"].append(item)
            state["observations"] = state["observations"][-MAX_OBSERVATIONS:]
            self._save(state)
        return item

    def observe_outcome(self, state: dict[str, Any], *, elapsed_ms: int, ok: bool) -> list[dict[str, Any]]:
        """把一次真实 Agent 运行自动折算成各原子 Skill 的观测（L0 主入口）。

        同时把结构化轨迹写进 trace store —— 观测是**聚合**（一个 Skill 一行），
        轨迹是**明细**（每一步工具调用），两者缺一不可：没有轨迹就做不了失败
        场景切片，没有观测就算不出健康画像。轨迹写入是 best-effort。
        """
        try:
            self.record_trace(state)
        except Exception:  # noqa: BLE001 - 轨迹是明细账，写失败不能影响观测主账
            pass

        calls = [x for x in (state.get("tool_calls") or []) if isinstance(x, dict)]
        tool_to_skill = _tool_skill_map()
        by_skill: dict[str, list[dict[str, Any]]] = {}
        for call in calls:
            skill_id = tool_to_skill.get(str(call.get("tool") or ""))
            if skill_id:
                by_skill.setdefault(skill_id, []).append(call)

        observations = []
        for skill_id, skill_calls in by_skill.items():
            succeeded = sum(bool(x.get("ok")) for x in skill_calls)
            degraded = any(bool(x.get("degraded")) for x in skill_calls)
            verdict = "pass" if ok and succeeded == len(skill_calls) and not degraded else (
                "degraded" if succeeded else "fail"
            )
            latency = sum(float(x.get("elapsed_ms") or 0) for x in skill_calls)
            observations.append(self.observe({
                "skill_name": skill_id,
                "version": self._active_version(skill_id),
                "verdict": verdict,
                "latency_ms": latency or elapsed_ms,
                "trace_id": state.get("trace_id") or "",
                "task_id": state.get("task_id") or "",
                "error_type": "" if verdict == "pass" else _error_type(skill_calls),
                "coverage": succeeded / len(skill_calls),
                "tool_calls": len(skill_calls),
            }, user_id=_as_int(state.get("user_id"))))
        return observations

    def record_trace(self, state: dict[str, Any]) -> dict[str, Any]:
        """轨迹采集：把一次运行的结构化轨迹追加到 trace store（按天分片 JSONL）。

        **字段严格对齐** ``state_manager.record_tool_call`` 真正写进
        ``state["tool_calls"]`` 的那一套：``step / tool / agent / args_summary /
        ok / elapsed_ms / retries / error / cached / degraded / note``。

        这里踩过一个坑，必须写下来：本模块最初按"想当然"的字段名读
        （``index`` / ``error_type`` / ``param_keys``），而真实记录里**这三个
        一个都不存在**。结果不是报错，而是轨迹照写、步序恒为 0、错误类型
        恒为空 —— 后面的失败定位与聚类全部基于空字段静默失准。凡是跨模块
        读字典，字段名必须去源头的 pydantic 模型或写入点对一遍。

        **只记结构性字段**：工具名、耗时、成败、重试次数，以及从
        ``args_summary`` 里**只抽出键名**。``args_summary`` 形如
        ``query=劳动合同… top_k=5``，值本身可能带当事人信息，所以这里按
        标识符模式过滤，只留 ``query`` / ``top_k`` 这类英文键名，
        正文一律不进 trace store（与 ``overview``"仅暴露聚合指标，
        不泄露用户输入或材料正文"是同一条纪律）。
        """
        calls = [x for x in (state.get("tool_calls") or []) if isinstance(x, dict)]
        trace = {
            "trace_id": str(state.get("trace_id") or "trace_" + uuid.uuid4().hex[:12]),
            "task_id": str(state.get("task_id") or ""),
            "case_id": str(state.get("case_id") or ""),
            # session / turn 是图 L0 要求的三级（session → turn → tool-call）中的
            # 前两级。缺了它们，"同一个会话第 3 轮开始变差"这类现象无从归拢。
            "session_id": str(state.get("session_id") or state.get("conversation_id") or ""),
            "turn_index": _as_int(state.get("turn_index") or state.get("turn")) or 0,
            "started_at": self._now(),
            "elapsed_ms": float(state.get("elapsed_ms") or 0.0),
            "ok": bool(state.get("ok", True)),
            "steps": [
                {
                    "step": _step_of(call, index),
                    "tool": str(call.get("tool") or ""),
                    "agent": str(call.get("agent") or ""),
                    "ok": bool(call.get("ok")),
                    "degraded": bool(call.get("degraded")),
                    "elapsed_ms": float(call.get("elapsed_ms") or 0.0),
                    "retries": _as_int(call.get("retries")) or 0,
                    "error": str(call.get("error") or "")[:200],
                    "cached": bool(call.get("cached")),
                    "param_keys": features_mod.extract_param_keys(call.get("args_summary")),
                }
                for index, call in enumerate(calls)
            ],
        }
        self._ensure_dirs()
        shard = self._trace_dir / f"{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"
        with shard.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(trace, ensure_ascii=False) + "\n")
        return trace

    def traces(self, *, limit: int = 200, day: str | None = None) -> list[dict[str, Any]]:
        """读回轨迹（默认当天）。用于失败切片与人工复盘。"""
        shard = self._trace_dir / f"{day or datetime.now(timezone.utc):%Y-%m-%d}.jsonl"
        if not shard.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in shard.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                rows.append(parsed)
        return rows[-max(1, limit):][::-1]

    def trace_sessions(self, *, limit: int = 50, day: str | None = None) -> list[dict[str, Any]]:
        """按会话聚合轨迹（图 L0 的 session → turn → tool-call 三级）。

        没有 session_id 的轨迹（离线脚本、单次探测）归到 ``unassigned`` ——
        丢掉它们会让"今天到底跑了多少条"对不上，留着又必须有个归处。
        """
        buckets: dict[str, list[dict[str, Any]]] = {}
        for trace in self.traces(limit=1000, day=day):
            key = str(trace.get("session_id") or "") or "unassigned"
            buckets.setdefault(key, []).append(trace)

        sessions: list[dict[str, Any]] = []
        for session_id, items in buckets.items():
            ordered = sorted(items, key=lambda x: int(x.get("turn_index") or 0))
            steps = sum(len(item.get("steps") or []) for item in items)
            failed = sum(1 for item in items if not item.get("ok"))
            sessions.append({
                "session_id": session_id,
                "turns": len(items),
                "tool_calls": steps,
                "failed_turns": failed,
                "first_at": ordered[0].get("started_at") if ordered else None,
                "last_at": ordered[-1].get("started_at") if ordered else None,
                "turn_indexes": [int(x.get("turn_index") or 0) for x in ordered],
            })
        return sorted(sessions, key=lambda x: str(x["last_at"] or ""), reverse=True)[:max(1, limit)]

    def observation_stats(
        self, skill_name: str | None = None, *, hours: int = 24,
    ) -> dict[str, Any]:
        """图 L0 读数：判定分布 · 小时吞吐 · 延迟 · 数据延迟（原始计数）。

        ``health.success_rate`` 是 confidence 加权、剔除 unknown 后的健康口径，
        不能拿来当"这段时间写了几条、坏了几条"的读数 —— 这里给原始计数。
        """
        span = max(1, min(int(hours or 24), 168))
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=span)
        with self._lock:
            state = self._load()

        items: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        latest: datetime | None = None
        for x in state["observations"]:
            if not isinstance(x, dict):
                continue
            if skill_name and x.get("skill_name") != skill_name:
                continue
            at = features_mod.parse_ts(x.get("created_at"))
            # 数据延迟取**全量**最新一条，不受时间窗影响：窗口内为空恰恰就是
            # "数据停了"的信号本身，用空窗算延迟会把该信号一并吃掉。
            if at is not None and (latest is None or at > latest):
                latest = at
            if at is None or at < cutoff:
                continue
            items.append(x)
            counts[at.strftime("%Y-%m-%dT%H")] += 1

        verdicts = drift_mod.summarize_verdicts(items)
        known = verdicts["pass"] + verdicts["fail"] + verdicts["degraded"]
        latencies = [float(x.get("latency_ms") or 0) for x in items if x.get("latency_ms")]
        return {
            "skill_name": skill_name or "",
            "hours": span,
            "verdicts": verdicts,
            "total": len(items),
            "success_rate": round(verdicts["pass"] / known, 4) if known else None,
            "latency": {
                "p50_ms": features_mod.percentile(latencies, 0.5),
                "p95_ms": features_mod.percentile(latencies, 0.95),
            },
            "throughput": [{"bucket": b, "count": c} for b, c in sorted(counts.items())],
            "data_lag_seconds": round((now - latest).total_seconds()) if latest else None,
        }

    def _active_version(self, skill_name: str) -> str:
        with self._lock:
            return str(self._load()["active_versions"].get(skill_name) or "current")

    def features(self, skill_name: str, *, window: int = WINDOW_SIZE) -> dict[str, Any]:
        """图 L0「特征工程」：调用量 · 失败率 EWMA · 覆盖广度 · staleness · last_gain。

        取**当前窗**（观测窗的最近一半）而不是整个观测窗：特征是用来跟基线
        对拍的，混进上一周期的数据等于把基线也拌进当前值里。
        """
        with self._lock:
            state = self._load()
        recent = self._recent(state, skill_name, window)
        _, current = split_window(recent)
        return features_mod.feature_vector(current)

    def aggregate_window(self, skill_name: str, *, persist: bool = True) -> dict[str, Any]:
        """图 L0「窗口聚合」：把一个窗口的特征压成 feature store 快照。

        按 **Skill 一个文件**存多个窗口，而不是"每窗口一个文件"：后者跑一年
        会产生数千个小文件，删除与迁移都变成负担，而真正被读的永远只有最近几个。
        """
        with self._lock:
            state = self._load()
        recent = self._recent(state, skill_name, WINDOW_SIZE)
        snapshot = features_mod.feature_vector(recent)
        snapshot["skill_name"] = skill_name
        baseline_obs, _ = split_window(recent)
        if persist:
            # 图 L5「漂移基线重建」：成功形态基线随窗口一起落盘，而不是每次
            # 临时从观测里现算。现算的问题是同一条观测会被反复用于建基线，
            # 上一周期的数据混进本周基线，漂移阈值整体漂移（听起来像绕口令，
            # 但这就是"漂移检测自己也在漂"的实际成因）。
            self._write_feature_snapshot(
                skill_name, snapshot,
                baseline_model=features_mod.success_baseline(baseline_obs),
            )
        return snapshot

    def _write_feature_snapshot(
        self,
        skill_name: str,
        snapshot: dict[str, Any],
        *,
        baseline_model: dict[str, Any] | None = None,
    ) -> None:
        self._ensure_dirs()
        path = self._feature_dir / f"{_safe_name(skill_name)}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            payload = {"skill_name": skill_name, "windows": {}}
        windows = payload.setdefault("windows", {})
        windows[snapshot["window"]] = snapshot
        for stale in sorted(windows)[:-FEATURE_WINDOW_LIMIT]:
            windows.pop(stale, None)
        if baseline_model is not None:
            payload["success_baseline"] = baseline_model
            payload["success_baseline_window"] = snapshot["window"]
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # ============================================================ L1 健康诊断层
    def health(self, skill_name: str) -> dict[str, Any]:
        """四维健康画像 + 成功基线 + 三检验漂移 + 先行退化信号。"""
        with self._lock:
            state = self._load()
        recent = self._recent(state, skill_name, WINDOW_SIZE)
        baseline = state["baselines"].get(skill_name) or {
            "success_rate": 0.95, "p95_latency_ms": 1000, "coverage": 0.85,
        }

        # 成功率按 confidence 加权（缺省 1.0 → 与逐条计数等价）。
        passed = sum(_weight(x) for x in recent if x.get("verdict") == "pass")
        known = sum(_weight(x) for x in recent if x.get("verdict") in drift_mod.KNOWN_VERDICTS)
        success_rate = round(passed / known, 6) if known else None

        latencies = [float(x.get("latency_ms") or 0) for x in recent if x.get("latency_ms")]
        p95 = features_mod.percentile(latencies, 0.95)
        coverage_values = [float(x["coverage"]) for x in recent if x.get("coverage") is not None]
        coverage = round(sum(coverage_values) / len(coverage_values), 6) if coverage_values else None
        gain_values = [float(x["last_gain"]) for x in recent if x.get("last_gain") is not None]
        last_gain = round(sum(gain_values) / len(gain_values), 6) if gain_values else None
        drift_score = drift_mod.verdict_drift(recent)

        signals: list[str] = []
        if success_rate is not None and success_rate < float(baseline["success_rate"]) - 0.05:
            signals.append("success_rate_degraded")
        if p95 is not None and p95 > float(baseline["p95_latency_ms"]) * 1.3:
            signals.append("latency_degraded")
        if coverage is not None and coverage < float(baseline.get("coverage", 0.85)) - 0.05:
            signals.append("coverage_degraded")
        if drift_score is not None and drift_score >= 0.20:
            signals.append("distribution_drift")
        if last_gain is not None and last_gain <= 0:
            signals.append("staleness")

        # 基线窗与当前窗必须真的分开：拿同一批数据既建基线又描述"当前形态"，
        # 任何变化都会被自己平均掉，先行信号永远不触发（这是最隐蔽的一种失效）。
        baseline_obs, current_obs = split_window(recent)
        feature_vector = features_mod.feature_vector(current_obs)
        baseline_model = features_mod.success_baseline(baseline_obs)
        profile = self._profile(success_rate, coverage, feature_vector, baseline)
        early = drift_mod.early_warning(feature_vector, baseline_model)
        if early["risk"] >= 0.4:
            signals.append("early_degradation")

        return {
            "skill_name": skill_name,
            "sample_count": len(recent),
            "success_rate": success_rate,
            "p95_latency_ms": p95,
            "coverage": coverage,
            "last_gain": last_gain,
            "drift_score": drift_score,
            "drift": drift_mod.drift_report(baseline_obs, current_obs),
            "baseline": baseline,
            "success_baseline": baseline_model,
            "features": feature_vector,
            "profile": profile,
            "radar": {key: item["score"] for key, item in profile.items()},
            "early_warning": early,
            "signals": signals,
            "active_version": state["active_versions"].get(skill_name, "current"),
            "status": "degraded" if signals else "healthy",
        }

    def _recent(self, state: dict[str, Any], skill_name: str, window: int) -> list[dict[str, Any]]:
        return [x for x in state["observations"] if x.get("skill_name") == skill_name][-window:]

    def _profile(
        self,
        success_rate: float | None,
        coverage: float | None,
        feature_vector: dict[str, Any],
        baseline: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        """四维健康画像。每维都给 value / threshold / status / score(0~1)。

        score 是"归一化健康度"，越大越好，专供前端画雷达图 —— 四个维度量纲
        完全不同（比率、天数、收益），不归一化根本画不到一张图上。
        """
        base_success = float(baseline["success_rate"])
        base_coverage = float(baseline.get("coverage", 0.85))
        failure_rate = None if success_rate is None else round(1.0 - success_rate, 6)
        failure_threshold = round(1.0 - base_success, 6)
        staleness = feature_vector.get("staleness_days")
        last_gain = feature_vector.get("last_gain")

        def level(value: float | None, warn: float, alert: float | None = None) -> str:
            if value is None:
                return "unknown"
            if alert is not None and value >= alert:
                return "alert"
            return "warn" if value >= warn else "ok"

        def level_low(value: float | None, warn: float, alert: float | None = None) -> str:
            if value is None:
                return "unknown"
            if alert is not None and value <= alert:
                return "alert"
            return "warn" if value < warn else "ok"

        return {
            "failure_rate": {
                "value": failure_rate, "threshold": failure_threshold,
                "status": level(failure_rate, failure_threshold, failure_threshold + 0.05),
                "score": None if failure_rate is None else round(max(0.0, 1.0 - failure_rate), 4),
            },
            "coverage": {
                "value": coverage, "threshold": base_coverage,
                "status": level_low(coverage, base_coverage - 0.02, base_coverage - 0.05),
                "score": None if coverage is None else round(min(1.0, max(0.0, coverage)), 4),
            },
            "staleness": {
                "value": staleness, "threshold": STALENESS_DAYS,
                "status": level(staleness, STALENESS_DAYS, STALENESS_DAYS * 2),
                "score": None if staleness is None else round(max(0.0, 1.0 - min(1.0, float(staleness) / 30.0)), 4),
            },
            "last_gain": {
                "value": last_gain, "threshold": 0.0,
                "status": "unknown" if last_gain is None else ("warn" if float(last_gain) <= 0 else "ok"),
                "score": None if last_gain is None else round(min(1.0, max(0.0, float(last_gain) / 0.1)), 4),
            },
        }

    def overview(self) -> dict[str, Any]:
        """L0-L5 汇总视图：仅暴露聚合指标，不泄露用户输入或材料正文。"""
        with self._lock:
            state = self._load()
        names = sorted(set(CAPABILITY_SKILLS) | {
            str(x.get("skill_name")) for x in state["observations"] if x.get("skill_name")
        })
        health = [self.health(name) for name in names]
        tasks = state["tasks"]
        return {
            "version": state["version"],
            "cycle": "weekly",
            "layers": ["observation", "diagnosis", "decision", "execution", "validation", "governance"],
            "skills": health,
            "degraded_count": sum(x["status"] == "degraded" for x in health),
            "task_counts": dict(Counter(str(x.get("status") or "unknown") for x in tasks)),
            "guardrails": {"pre": "offline_validation", "during": "gray_rollout", "post": "snapshot_revert"},
            "frozen": state["frozen"],
            "budget": self._budget_usage(state),
            "scheduler": state["scheduler"],
            "last_cycle": state["cycles"][0] if state["cycles"] else None,
            "north_star": self.north_star(_state=state, _health=health),
            "artifacts": {
                "results_tsv": str(self._results_path),
                "feature_store": str(self._feature_dir),
                "trace_store": str(self._trace_dir),
                "version_ledger": str(self._ledger.path),
            },
        }

    # ============================================================ L2 进化决策层
    def candidates(self, skill_name: str, limit: int = 3) -> list[dict[str, Any]]:
        """本周 Top-K 进化候选：四目标帕累托 → 预算补足 → Thompson 定序。

        **无健康信号时返回空**：平台期不折腾（见模块 docstring 第 2 条）。
        """
        health = self.health(skill_name)
        with self._lock:
            state = self._load()
        budget = max(1, min(int(limit or execution.CANDIDATE_BUDGET), execution.CANDIDATE_BUDGET))

        recipes = {
            "success_rate_degraded": ("failure_clustering", "按错误类型聚类并收紧失败分支", 1.00, 0.90),
            "latency_degraded": ("timeout_budget", "调整超时、重试和缓存预算", 0.85, 0.80),
            "coverage_degraded": ("retrieval_coverage", "扩大召回并补充校验依赖", 0.95, 0.85),
            "distribution_drift": ("routing_threshold", "按近期分布重估路由阈值", 0.80, 0.70),
            "staleness": ("knowledge_refresh", "刷新知识快照与版本标签", 0.75, 0.65),
            "early_degradation": ("guardrail_tuning", "先行信号触发，先收紧门禁再谈改动", 0.70, 0.60),
        }

        signals = health["signals"]
        if not signals:
            return []

        out: list[dict[str, Any]] = []
        for signal in signals:
            kind, hypothesis, quality, cost = recipes.get(signal, recipes["success_rate_degraded"])
            out.append(self._make_candidate(
                state, skill_name, signal=signal, patch_type=kind,
                hypothesis=hypothesis, quality=quality, cost=cost,
                health=health, source="signal",
            ))

        frontier = execution.pareto_frontier(out)

        # 预算补足：帕累托前沿不足 K 个时，用**低风险通用项**补齐候选池。
        # 为什么非要补满：只有一两个候选时，"选哪个"退化成"只能选这个"，
        # 报错解无法与备选对照，而对照正是回放验证能下结论的前提。
        if len(frontier) < budget:
            used = {item["patch_type"] for item in frontier}
            for patch_type in execution.PATCH_TEMPLATES:
                if len(frontier) >= budget:
                    break
                if patch_type in used:
                    continue
                frontier.append(self._make_candidate(
                    state, skill_name, signal="budget_fill", patch_type=patch_type,
                    hypothesis="预算补足：低风险通用项，用于与主候选做对照回放",
                    quality=0.55, cost=0.50, health=health, source="budget_fill",
                ))

        ranked = sorted(frontier, key=lambda x: x["score"], reverse=True)[:budget]
        return ranked

    def _make_candidate(
        self,
        state: dict[str, Any],
        skill_name: str,
        *,
        signal: str,
        patch_type: str,
        hypothesis: str,
        quality: float,
        cost: float,
        health: dict[str, Any],
        source: str,
    ) -> dict[str, Any]:
        posterior = state["bandit"].get(f"{skill_name}:{patch_type}") or {"alpha": 1.0, "beta": 1.0}
        alpha, beta = float(posterior.get("alpha", 1.0)), float(posterior.get("beta", 1.0))
        sampled = _thompson(alpha, beta)
        coverage = float(health.get("coverage") or health["baseline"].get("coverage", 0.85))
        # 冷启动的判据是「后验还没有任何证据」（Beta(1,1)），**与候选从哪来无关**。
        # 早先这里还挂了一个 ``source != "signal"`` 的条件，等于说"信号候选永远
        # 不算冷启动" —— 而信号候选恰恰是最需要这个兜底的那一类（新退化模式
        # 都从信号来，它们当然没有历史后验）。那个条件把图 L2 写的"冷启动
        # coverage 优先"从里到外反了过来。
        cold_start = alpha + beta <= 2.0 + 1e-9
        rank_score = coverage if cold_start else quality * 0.7 + sampled * 0.3
        return {
            "candidate_id": "cand_" + uuid.uuid5(uuid.NAMESPACE_URL, f"{skill_name}:{patch_type}").hex[:12],
            "skill_name": skill_name,
            "signal": signal,
            "patch_type": patch_type,
            "hypothesis": hypothesis,
            "source": source,
            "objectives": {
                "quality": round(quality, 3),
                "latency": 1.0 if signal != "latency_degraded" else 0.7,
                "coverage": 1.0 if signal != "coverage_degraded" else 0.7,
                "cost": round(cost, 3),
            },
            "bandit": {"alpha": round(alpha, 4), "beta": round(beta, 4),
                       "sampled": round(sampled, 4), "cold_start": cold_start},
            "score": round(rank_score, 4),
            "scope": "skill_config",
            "requires_human_approval": True,
        }

    def update_bandit(self, skill_name: str, patch_type: str, reward: float) -> dict[str, Any]:
        """用真实收益更新后验（图 L2「last_gain 为奖励」）。

        reward 取 [0,1] 的成功率增量，累加进 Beta 后验 —— 这是 Thompson 采样
        的输入。**只有 Keep 之后才更新**：Revert 的候选不该拿到"试过就算数"的
        信用，否则每个被否掉的方案都会稀释掉真正有效那条的证据。
        """
        reward = min(1.0, max(0.0, float(reward)))
        with self._lock:
            state = self._load()
            key = f"{skill_name}:{patch_type}"
            posterior = state["bandit"].get(key) or {"alpha": 1.0, "beta": 1.0}
            posterior["alpha"] = round(float(posterior["alpha"]) + reward, 6)
            posterior["beta"] = round(float(posterior["beta"]) + (1.0 - reward), 6)
            posterior["updated_at"] = self._now()
            state["bandit"][key] = posterior
            self._save(state)
            return posterior

    # ============================================================ L3 进化执行层
    def failure_cases(self, skill_name: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """图 L3「失败场景回滚」：切片轨迹 + 证据包。

        取当天 trace store 里**成功的同类轨迹**当灰事实对照 —— 没有对照，
        失败定位只能给出"这一步错了"，给不出"它为什么不像正常的一步"。
        """
        skill = CAPABILITY_SKILLS.get(skill_name)
        tools = (skill.tool, *skill.dependencies) if skill else ()
        traces = self.traces(limit=500)
        success_traces = [t for t in traces if t.get("ok")]
        cases: list[dict[str, Any]] = []
        for trace in traces:
            case = execution.slice_failure_case(
                trace, skill_name=skill_name, skill_tools=tools,
                success_traces=success_traces,
            )
            if case is not None:
                cases.append(case)
            if len(cases) >= limit:
                break
        with self._lock:
            state = self._load()
            state["failures"] = cases[:limit]
            self._save(state)
        return cases

    def cluster_failures(
        self,
        skill_name: str,
        *,
        threshold: float = 0.6,
        semantic: bool = False,
    ) -> list[dict[str, Any]]:
        """图 L3「失败聚类」：相似失败归并，防止重复诊断。

        ``semantic=True`` 时走 embedding 语义归并（图里写的就是这条），
        否则回退字符集相似度。**默认关闭**的理由见 ``evolution_embedding``：
        加载 BGE 实测 16.8 秒，而这是个在线可点的接口。无论走哪条，
        结果里的 ``similarity`` 字段都会如实写明本次用的是什么。
        """
        cases = self.failure_cases(skill_name)
        embedder = evolution_embedding.get_embedder(semantic)
        return execution.cluster_failures(cases, threshold=threshold, embedder=embedder)

    def root_cause(
        self,
        skill_name: str,
        *,
        cluster_id: str | None = None,
        llm: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """图 L3「根因分析（离线 LLM）」：诊断 → 假设 → 证据校验。

        ``llm`` 由调用方注入（默认走确定性规则引擎），模块自身不 import
        任何模型客户端 —— 离线任务不占用在线推理通道的预算与时限。
        """
        clusters = self.cluster_failures(skill_name)
        if not clusters:
            return {"skill_name": skill_name, "cluster_id": None, "engine": "none",
                    "diagnosis": {"finding": "该 Skill 暂无失败场景样本，无需根因分析。", "evidence": []},
                    "hypotheses": [], "evidence_check": {"passed": False, "case_count": 0,
                                                         "missing": ["没有失败样本"],
                                                         "statement": "无样本，不做根因推断。"},
                    "patch_type": None}
        target = next((c for c in clusters if c["cluster_id"] == cluster_id), clusters[0])
        cases = [c for c in self.failure_cases(skill_name) if c["case_id"] in set(target["case_ids"])]
        return execution.diagnose_cluster(target, cases, llm=llm)

    # ============================================================ L4 验证守门层
    def create_task(self, payload: dict[str, Any], *, user_id: int | None = None) -> dict[str, Any]:
        skill = str(payload.get("skill_name") or "").strip()
        if not skill:
            raise ValueError("skill_name 不能为空")
        now = self._now()
        candidate_id = str(payload.get("candidate_id") or "")
        task = {
            "task_id": "evo_" + uuid.uuid4().hex[:12],
            "skill_name": skill,
            "base_version": str(payload.get("base_version") or "current"),
            "hypothesis": str(payload.get("hypothesis") or "根据健康信号执行回归验证"),
            "candidate_id": candidate_id,
            "patch_scope": "skill_config",
            "budget": {
                "max_runs": int(payload.get("max_runs") or DEFAULT_BUDGET["max_runs_per_task"]),
                "max_tokens": int(payload.get("max_tokens") or 200_000),
            },
            "status": "candidate",
            "decision": None,
            "result": None,
            "require_gray": bool(payload.get("require_gray", False)),
            "patch_artifact": None,
            "rollout": {"stage": "pending", "percent": 0, "history": []},
            "created_at": now,
            "updated_at": now,
            "created_by": user_id,
        }
        with self._lock:
            state = self._load()
            self._assert_writable(state)
            usage = self._budget_usage(state)
            if usage["tasks_used"] >= usage["max_tasks_per_cycle"]:
                raise ValueError(
                    f"本周期进化预算已用尽（{usage['tasks_used']}/{usage['max_tasks_per_cycle']} 个任务）——"
                    f"元控制拒绝继续创建，下一周期自动恢复。"
                )
            state["tasks"].insert(0, task)
            self._save(state)
        return self.build_patch(task["task_id"]) or task

    def build_patch(self, task_id: str) -> dict[str, Any] | None:
        """生成隔离的配置补丁工件；不会改注册表源码或生产配置。"""
        with self._lock:
            state = self._load()
            task = next((x for x in state["tasks"] if x["task_id"] == task_id), None)
            if task is None:
                return None
            candidates = self.candidates(task["skill_name"])
            chosen = next((x for x in candidates if x["candidate_id"] == task.get("candidate_id")), None)
            chosen = chosen or (candidates[0] if candidates else {
                "patch_type": "guardrail_tuning", "signal": "manual",
                "hypothesis": task["hypothesis"],
            })
            changes = execution.patch_changes(chosen["patch_type"], skill_name=task["skill_name"])
            guard = execution.guard_changes(changes)
            artifact = {
                "schema": "skill-config-patch/v1",
                "task_id": task_id,
                "skill_name": task["skill_name"],
                "base_version": task["base_version"],
                "patch_type": chosen["patch_type"],
                "root_cause": chosen["signal"],
                "hypothesis": chosen["hypothesis"],
                "changes": changes,
                "guard": guard,
                "permissions_changed": False,
                "generated_at": self._now(),
            }
            # 安全红线命中 → **拒绝落盘**，而不是打个警告继续。
            if not guard["ok"]:
                task["status"] = "patch_rejected"
                task["patch_artifact"] = None
                task["guard_violations"] = guard["violations"]
                task["updated_at"] = self._now()
                self._save(state)
                return task

            self._ensure_dirs()
            path = self._artifact_dir / f"{task_id}.patch.json"
            path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
            task["patch_artifact"] = str(path)
            task["root_cause"] = artifact["root_cause"]
            task["status"] = "patch_ready"
            task["updated_at"] = self._now()
            self._save(state)
            return task

    def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return self._load()["tasks"][:max(1, min(limit, 200))]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            return next((x for x in self._load()["tasks"] if x["task_id"] == task_id), None)

    def replay(self, task_id: str) -> dict[str, Any] | None:
        """图 L4「同基线回放」：新旧版本同 holdout 的 A/B 差分对照。"""
        with self._lock:
            state = self._load()
            task = next((x for x in state["tasks"] if x["task_id"] == task_id), None)
        if task is None:
            return None
        result = task.get("result") or {}
        baseline_metrics = result.get("baseline_metrics")
        candidate_metrics = result.get("candidate_metrics")
        if not baseline_metrics or not candidate_metrics:
            return None
        return {
            "task_id": task_id,
            "skill_name": task["skill_name"],
            "holdout": result.get("holdout", "same_baseline"),
            "ab": gate.ab_diff(baseline_metrics, candidate_metrics),
            "gate": result.get("gate"),
        }

    def run_task(self, task_id: str, candidate_metrics: dict[str, Any]) -> dict[str, Any] | None:
        """指标门禁：Δ成功率 ≥ ε 且零回退 → passed，否则 failed。"""
        with self._lock:
            state = self._load()
            self._assert_writable(state)
            task = next((x for x in state["tasks"] if x["task_id"] == task_id), None)
            if task is None:
                return None
            health = self.health(task["skill_name"])
            # 参考基线：实测优先，无实测（样本不足）时用登记基线。
            # 不用"实测 or 0" —— 那会让"没数据"变成"要求 0"，门禁形同虚设。
            reference = {
                "success_rate": _first(health["success_rate"], health["baseline"]["success_rate"]),
                "p95_latency_ms": _first(health["p95_latency_ms"], health["baseline"]["p95_latency_ms"]),
                "coverage": _first(health["coverage"], health["baseline"].get("coverage", 0.85)),
            }
            measured = {
                "success_rate": float(candidate_metrics["success_rate"]),
                "p95_latency_ms": float(candidate_metrics["p95_latency_ms"]),
                "coverage": float(candidate_metrics.get("coverage", reference["coverage"])),
                "critical_failures": int(candidate_metrics.get("critical_failures") or 0),
                "run_count": int(candidate_metrics.get("run_count") or task["budget"]["max_runs"]),
            }
            gate_result = gate.gate_checks(
                measured, reference, epsilon=gate.DEFAULT_EPSILON,
                max_runs=int(task["budget"]["max_runs"]),
            )
            ab = gate.ab_diff(reference, measured)

            task["status"] = "validation_passed" if gate_result["passed"] else "validation_failed"
            task["result"] = {
                "health_before": health,
                "baseline_metrics": reference,
                "candidate_metrics": measured,
                "checks": gate_result["checks"],
                "failed_checks": gate_result["failed"],
                "thresholds": gate_result["thresholds"],
                "ab": ab,
                "gate": gate_result,
                "passed": gate_result["passed"],
                "holdout": "same_baseline",
                "candidate_version": task["base_version"] + ".candidate",
            }
            task["updated_at"] = self._now()

            self._ensure_dirs()
            replay_path = self._artifact_dir / f"{task_id}.replay.json"
            replay_path.write_text(json.dumps({
                "schema": "skill-replay/v1", "task_id": task_id,
                "skill_name": task["skill_name"], "baseline": reference,
                "candidate": measured, "ab": ab, "gate": gate_result,
                "generated_at": self._now(),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            task["replay_artifact"] = str(replay_path)

            self._save(state)
            return task

    # ---------------------------------------------------------------- 灰度
    def preflight(self, skill_name: str, *, task: dict[str, Any] | None = None) -> dict[str, Any]:
        """三道防线的第一道：事前漂移预演（图右侧「事前｜漂移预演」）。

        它问的**不是**"有没有退化"（退化恰恰是启动灰度的前提），而是两个更
        基本的问题：

        1. **回放结论还成立吗** —— 候选是在某个基线上验证的。如果此后观测
           已经漂走，那份"同基线回放"的 A/B 结论就失效了，拿它去上灰度等于
           用一个过期的凭据开门。这一条是硬阻断。
        2. **分布有没有先验** —— 检验样本不足时只是"暂时看不出来"，
           记为提示而不是阻断：没数据不等于有问题，把"不知道"当成"不合格"
           会让系统在小流量 Skill 上永远开不了灰度。

        只有"自相矛盾"（漂移已成立、却没有任何诊断信号）才升级为阻断 ——
        那意味着诊断层和漂移层对同一批数据给出了矛盾的结论，此时上灰度
        是在赌哪一层是对的。
        """
        health = self.health(skill_name)
        drift = health["drift"]
        early = health["early_warning"]

        blocking: list[str] = []
        notes: list[str] = []

        if drift["detected"] and not health["signals"]:
            blocking.append(
                f"漂移已在 {'/'.join(drift['drifted_dimensions'])} 上成立，"
                f"但诊断层没有给出任何健康信号 —— 两层结论矛盾，先查清再灰度。"
            )

        if task is not None:
            recorded = (task.get("result") or {}).get("baseline_metrics") or {}
            now_success = health["success_rate"]
            recorded_success = recorded.get("success_rate")
            if now_success is not None and recorded_success is not None:
                gap = abs(float(now_success) - float(recorded_success))
                if gap > BASELINE_DRIFT_TOLERANCE:
                    blocking.append(
                        f"回放时的基线成功率 {float(recorded_success):.3f} 与当前实测 "
                        f"{float(now_success):.3f} 相差 {gap:.3f}，基线已漂移，"
                        f"同基线回放的结论不再成立。"
                    )

        if drift["insufficient_samples"]:
            notes.append("三个维度的漂移检验都缺样本，当前分布没有先验（仅提示，不阻断）。")
        if early["risk"] >= 0.6:
            notes.append(f"先行风险 {early['risk']:.2f} 偏高，建议优先看先行信号。")

        return {
            "skill_name": skill_name,
            "passed": not blocking,
            "blocking": blocking,
            "notes": notes,
            "drift": {"detected": drift["detected"], "dimensions": drift["drifted_dimensions"],
                      "insufficient_samples": drift["insufficient_samples"]},
            "early_warning": {"risk": early["risk"], "signal_codes": early["signal_codes"]},
            "samples": health["sample_count"],
            "signals": health["signals"],
            "checked_at": self._now(),
        }

    def start_rollout(self, task_id: str) -> dict[str, Any] | None:
        """启动三阶段灰度：影子模式（0% 流量）开始。

        **先过漂移预演，再开灰度**（图右侧「未过守门不可全量上线」）。
        预演结果随任务落库，事后能回答"当时是基于什么判断放行的"。
        """
        with self._lock:
            state = self._load()
            self._assert_writable(state)
            task = next((x for x in state["tasks"] if x["task_id"] == task_id), None)
            if task is None:
                return None
            if task["status"] != "validation_passed":
                raise ValueError("候选未通过指标门禁，不允许进入灰度")

            rehearsal = self.preflight(task["skill_name"], task=task)
            if not rehearsal["passed"]:
                task["preflight"] = rehearsal
                task["updated_at"] = self._now()
                self._save(state)
                raise ValueError("事前漂移预演未通过，不允许启动灰度：" + "；".join(rehearsal["blocking"]))

            rollout = {"stage": "shadow", "percent": 0, "history": [
                {"stage": "shadow", "percent": 0, "at": self._now(), "action": "start",
                 "preflight": rehearsal["passed"]},
            ]}
            task["preflight"] = rehearsal
            task["rollout"] = rollout
            task["status"] = "rolling_out"
            task["updated_at"] = self._now()
            state["rollouts"][task_id] = rollout
            self._save(state)
            return task

    def advance_rollout(self, task_id: str, metrics: dict[str, Any]) -> dict[str, Any] | None:
        """推进灰度：每一档都要重新过门禁，不过就地回滚到影子。"""
        with self._lock:
            state = self._load()
            self._assert_writable(state)
            task = next((x for x in state["tasks"] if x["task_id"] == task_id), None)
            if task is None:
                return None
            if task["status"] != "rolling_out":
                raise ValueError("任务不在灰度中，无法推进")
            rollout = task.get("rollout") or {"stage": "shadow", "percent": 0, "history": []}
            health = self.health(task["skill_name"])
            reference = {
                "success_rate": _first(health["success_rate"], health["baseline"]["success_rate"]),
                "p95_latency_ms": _first(health["p95_latency_ms"], health["baseline"]["p95_latency_ms"]),
                "coverage": _first(health["coverage"], health["baseline"].get("coverage", 0.85)),
            }
            gate_result = gate.gate_checks(
                metrics, reference, epsilon=gate.DEFAULT_EPSILON,
                max_runs=int(task["budget"]["max_runs"]),
            )
            now = self._now()
            if not gate_result["passed"]:
                rollout.update({"stage": "shadow", "percent": 0})
                rollout["history"].append({
                    "stage": "shadow", "percent": 0, "at": now, "action": "rollback",
                    "reason": f"门禁未通过：{', '.join(gate_result['failed'])}",
                })
                task["status"] = "rolled_back"
            else:
                nxt = gate.next_stage(rollout["percent"])
                if nxt is None:
                    rollout["history"].append({"stage": rollout["stage"], "percent": rollout["percent"],
                                               "at": now, "action": "full"})
                    task["status"] = "rollout_complete"
                    # HITL：走到全量才抽样。影子/小流量阶段抽了也没用 ——
                    # 那时线上几乎没有真实流量，人工看到的样本量不足以判断。
                    review = self._maybe_queue_review(
                        task, state, reason="灰度已全量，抽样核对线上真实表现",
                    )
                    if review is not None:
                        task["review_id"] = review["review_id"]
                else:
                    rollout.update({"stage": nxt["stage"], "percent": nxt["percent"]})
                    rollout["history"].append({"stage": nxt["stage"], "percent": nxt["percent"],
                                               "at": now, "action": "advance"})
                    task["status"] = "rolling_out"
            task["rollout"] = rollout
            task["updated_at"] = now
            state["rollouts"][task_id] = rollout
            task["rollout_gate"] = gate_result
            self._save(state)
            return task

    # ---------------------------------------------------------------- 决策
    def decide(self, task_id: str, decision: str) -> dict[str, Any] | None:
        if decision not in {"keep", "revert"}:
            raise ValueError("decision 必须是 keep 或 revert")
        with self._lock:
            state = self._load()
            self._assert_writable(state)
            task = next((x for x in state["tasks"] if x["task_id"] == task_id), None)
            if task is None:
                return None
            # Keep 的判据是「门禁过了」，而不是「当前 status 恰好是 validation_passed」。
            # 这两者在灰度之后就分岔了：灰度期间 status 是 rolling_out，
            # 按 status 判会把"已经全量灰度完的候选"挡在门外，只能先回滚再 Keep。
            # 反过来，被灰度否掉的（rolled_back）即便当初门禁过了也不许 Keep ——
            # 线上实测已经推翻了离线结论，以线上为准。
            if decision == "keep":
                if task["status"] in KEEP_BLOCKED_STATUSES:
                    raise ValueError("候选版本未通过验证，不能 Keep")
                if not bool((task.get("result") or {}).get("passed")):
                    raise ValueError("候选版本未通过验证，不能 Keep")

            task["decision"] = decision
            task["status"] = "active" if decision == "keep" else "reverted"
            task["updated_at"] = self._now()
            # 记下"这一票投在第几个周期"。周期收口时要统计"本周期内的决策"，
            # 而时间戳精度只到秒 —— 同一秒内决策再收口，用时间比大小会漏计，
            # 用周期序号则完全确定。
            task["decided_cycle"] = len(state["cycles"])
            metrics = (task.get("result") or {}).get("candidate_metrics") or {}
            reference = (task.get("result") or {}).get("baseline_metrics") or {}
            # 候选版本与被否掉的版本**都要进账本**：Revert 的那条记录是
            # 唯一能回答"我们试过什么、为什么没上"的证据，只记 Keep 等于
            # 把一半的进化史删了。
            candidate_version = str((task.get("result") or {}).get("candidate_version") or "current")
            version = candidate_version

            if decision == "keep":
                state["active_versions"][task["skill_name"]] = version
                state["baselines"][task["skill_name"]] = {
                    "success_rate": float(metrics.get("success_rate") or 0),
                    "p95_latency_ms": float(metrics.get("p95_latency_ms") or 0),
                    "coverage": float(metrics.get("coverage") or 0.85),
                    "updated_at": self._now(),
                    "source_task_id": task_id,
                }
                gain = max(0.0, float(metrics.get("success_rate") or 0)
                           - float(reference.get("success_rate") or 0))
                self._record_gain(state, task["skill_name"], gain)

            state["audit"].append({
                "task_id": task_id,
                "skill_name": task["skill_name"],
                "decision": decision,
                "base_version": task["base_version"],
                "candidate_version": version,
                "snapshot": {"baseline": (task.get("result") or {}).get("health_before")},
                "created_at": self._now(),
            })
            self._save(state)

        # 版本账本：既在锁外写（避免持锁做 IO），也**必须**写在决策之后 ——
        # 账本记录的是"已经发生的事"，不是"打算做的事"。
        self._ledger.append({
            "entry_id": "ver_" + uuid.uuid4().hex[:12],
            "task_id": task_id,
            "skill_name": task["skill_name"],
            "action": decision,
            "from_version": task["base_version"],
            "to_version": version if decision == "keep" else task["base_version"],
            "rolled_back_from": version if decision == "revert" else None,
            "note": ("Keep：新版本生效，基线已刷新。"
                     if decision == "keep" else
                     "Revert：保持旧版本，账本留痕（版本库只进不退）。"),
            "created_at": self._now(),
        })
        if decision == "keep":
            self.update_bandit(
                task["skill_name"],
                str(task.get("root_cause") or "guardrail_tuning"),
                max(0.0, float(metrics.get("success_rate") or 0) - float(reference.get("success_rate") or 0)),
            )
        return task

    def version_ledger(self, skill_name: str | None = None, *, limit: int = 50) -> list[dict[str, Any]]:
        """版本账本（图 L4「git 轮转」）：只进不退，回滚留痕，可审计。"""
        if skill_name:
            return self._ledger.for_skill(skill_name, limit=limit)
        return self._ledger.entries(limit=limit)

    # ---------------------------------------------------------------- HITL
    def _maybe_queue_review(
        self,
        task: dict[str, Any],
        state: dict[str, Any],
        *,
        reason: str,
        force: bool = False,
    ) -> dict[str, Any] | None:
        """把候选放进人工抽查队列（图右侧「人在环 HITL：人工抽检」）。"""
        review_id = task.get("review_id")
        existing = next((x for x in state["reviews"] if x.get("review_id") == review_id), None) if review_id else None
        if existing is not None:
            return existing
        if not force and not _hitl_sample(str(task["task_id"])):
            return None
        review = {
            "review_id": "rev_" + uuid.uuid4().hex[:12],
            "task_id": task["task_id"],
            "skill_name": task["skill_name"],
            "patch_type": task.get("root_cause") or "unknown",
            "reason": reason,
            "status": "pending",
            "verdict": "",
            "note": "",
            "created_at": self._now(),
        }
        state["reviews"].insert(0, review)
        state["reviews"] = state["reviews"][:200]
        return review

    def request_review(
        self,
        task_id: str,
        *,
        reason: str = "人工主动送审",
    ) -> dict[str, Any] | None:
        """人工把某条候选**主动**送进抽查队列（绕过抽样概率）。

        抽查概率是为了控制队列长度，不该顺手把"律师自己觉得这条可疑"也挡在
        门外 —— 那种情况下人已经做出判断了，系统没有理由说"这次没抽到你"。
        """
        with self._lock:
            state = self._load()
            self._assert_writable(state)
            task = next((x for x in state["tasks"] if x["task_id"] == task_id), None)
            if task is None:
                return None
            review = self._maybe_queue_review(task, state, reason=reason, force=True)
            if review is not None:
                task["review_id"] = review["review_id"]
            self._save(state)
            return review

    def list_reviews(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._load()["reviews"])
        if status:
            items = [x for x in items if x.get("status") == status]
        return items[:max(1, limit)]

    def resolve_review(
        self,
        review_id: str,
        verdict: str,
        *,
        note: str = "",
        user_id: int | None = None,
    ) -> dict[str, Any] | None:
        """人工填抽查结论。``verdict`` 取 ``approve`` / ``reject``。

        ``reject`` 不自动回滚 —— 它只是把"人工认为不对"记下来，是否回滚由人
        用 ``decide`` 决定。让抽查结论直接触发回滚，等于把一次点击放大成
        一次生产变更，而抽查样本本来就只有几条。
        """
        if verdict not in {"approve", "reject"}:
            raise ValueError("verdict 必须是 approve 或 reject")
        with self._lock:
            state = self._load()
            self._assert_writable(state)
            review = next((x for x in state["reviews"] if x["review_id"] == review_id), None)
            if review is None:
                return None
            review.update({
                "status": "resolved",
                "verdict": verdict,
                "note": str(note or "")[:500],
                "resolved_at": self._now(),
                "resolved_by": user_id,
            })
            state["audit"].append({
                "action": f"hitl_{verdict}", "skill_name": review["skill_name"],
                "task_id": review["task_id"], "review_id": review_id,
                "note": review["note"], "created_at": review["resolved_at"],
                "created_by": user_id,
            })
            self._save(state)
            return review

    # ---------------------------------------------------------------- 审计台
    def audit_log(self, *, skill_name: str | None = None, limit: int = 100) -> dict[str, Any]:
        """人工巡检审计台（图 L5 元控制那一格）。

        把散在三处的记录合成**同一条时间线**：``state["audit"]``（熔断与决策）、
        版本账本（Keep/Revert 与回滚留痕）、人工抽查结论。三处分开看时每一处
        都很干净，只有并排放在时间轴上才看得出"先 Keep、两天后又被回滚"
        这类需要追问的序列。
        """
        with self._lock:
            state = self._load()
        entries: list[dict[str, Any]] = []
        for item in state["audit"]:
            if skill_name and item.get("skill_name") != skill_name:
                continue
            entries.append({"at": item.get("created_at"), "kind": item.get("action") or "decision",
                            "skill_name": item.get("skill_name"), "task_id": item.get("task_id"),
                            "detail": item})
        for item in self.version_ledger(skill_name, limit=200):
            entries.append({"at": item.get("created_at"), "kind": f"ledger:{item.get('action')}",
                            "skill_name": item.get("skill_name"), "task_id": item.get("task_id"),
                            "detail": item})
        for item in state["reviews"]:
            if skill_name and item.get("skill_name") != skill_name:
                continue
            entries.append({"at": item.get("resolved_at") or item.get("created_at"),
                            "kind": f"review:{item.get('status')}",
                            "skill_name": item.get("skill_name"), "task_id": item.get("task_id"),
                            "detail": item})
        entries.sort(key=lambda x: str(x.get("at") or ""), reverse=True)
        return {"skill_name": skill_name, "count": len(entries),
                "entries": entries[:max(1, limit)]}

    # ============================================================ L5 状态治理层
    def refresh_state(self, *, write_tsv: bool = True) -> dict[str, Any]:
        """周期收口：重算特征与基线、落 feature store、写 results.tsv、回到下一周期。"""
        with self._lock:
            state = self._load()
            names = sorted(set(CAPABILITY_SKILLS) | {
                str(x.get("skill_name")) for x in state["observations"] if x.get("skill_name")
            })

        snapshots = []
        rows: list[list[Any]] = []
        window = features_mod.window_key()
        for name in names:
            snapshot = self.aggregate_window(name)
            snapshots.append(snapshot)
            health = self.health(name)
            rows.append([
                window, name, health["active_version"], health["status"],
                _fmt(health["success_rate"]), _fmt(health["p95_latency_ms"]),
                _fmt(health["coverage"]), _fmt(health["last_gain"]),
                _fmt(health["drift_score"]), len(health["signals"]),
                ",".join(health["signals"]), self._now(),
            ])

        if write_tsv and rows:
            self._ensure_dirs()
            gate.write_tsv(
                self._results_path,
                ["window", "skill", "version", "status", "success_rate", "p95_latency_ms",
                 "coverage", "last_gain", "drift_score", "signal_count", "signals", "recorded_at"],
                rows,
            )

        cycle = {
            "cycle_id": "cyc_" + uuid.uuid4().hex[:12],
            "window": window,
            "skills": len(names),
            "degraded": sum(row[3] == "degraded" for row in rows),
            "feature_snapshots": len(snapshots),
            "results_tsv": str(self._results_path),
            "started_at": self._now(),
        }
        with self._lock:
            state = self._load()
            # 图 L5 的「后验更新」：bandit 后验按周期往先验收缩，否则半年前
            # 一次偶然的成功会永久抬高某个补丁类型的中选概率。
            decayed = self._decay_bandit(state)

            # 统计"**本周期内**发生的决策"，判据是决策时记下的周期序号，
            # 而不是时间戳：时间戳只到秒，同一秒内决策再收口会漏计，
            # 而"上一周期投的票"混进来又会让环比失真。
            current_cycle = len(state["cycles"])
            window_decisions = [
                x for x in state["tasks"]
                if x.get("decision") and _as_int(x.get("decided_cycle")) == current_cycle
            ]
            kept = sum(1 for x in window_decisions if x["decision"] == "keep")
            cycle["keep_ratio"] = round(kept / len(window_decisions), 4) if window_decisions else None
            cycle["decisions"] = {"keep": kept, "revert": len(window_decisions) - kept}
            # 图底部的「闭环回流」三件事，显式写出来而不是散落各处：
            # results.tsv 落盘 ─┬─ 成功基线重建 ─┬─ bandit 后验更新 → 回到 L0。
            cycle["reflow"] = {
                "results_tsv": str(self._results_path),
                "baseline_rebuilt": len(snapshots),
                "bandit_decayed": decayed,
                "next_window": window,
                "statement": ("闭环回流：results.tsv 已落盘、成功基线已重建、"
                              "bandit 后验已按周期衰减 → 回到 L0，进入下一周期。"),
            }
            state["feature_snapshots"].append({"window": window, "count": len(snapshots),
                                               "created_at": cycle["started_at"]})
            state["feature_snapshots"] = state["feature_snapshots"][-FEATURE_WINDOW_LIMIT:]
            state["cycles"].insert(0, cycle)
            state["cycles"] = state["cycles"][:52]
            state["scheduler"]["last_run_at"] = cycle["started_at"]
            self._save(state)
        return cycle

    def _decay_bandit(self, state: dict[str, Any], *, factor: float = BANDIT_DECAY_FACTOR) -> int:
        """把 bandit 后验往先验方向收缩（图 L5「后验更新」）。

        ``0.85`` 相当于证据半衰期约 4 个周期：既让新证据占主导，又不把历史
        一笔勾销。返回被更新的后验条数（收益表现要看得见）。
        """
        touched = 0
        for posterior in state["bandit"].values():
            alpha, beta = float(posterior.get("alpha", 1.0)), float(posterior.get("beta", 1.0))
            new_alpha = 1.0 + (alpha - 1.0) * factor
            new_beta = 1.0 + (beta - 1.0) * factor
            if abs(new_alpha - alpha) > 1e-9 or abs(new_beta - beta) > 1e-9:
                touched += 1
            posterior["alpha"] = round(new_alpha, 6)
            posterior["beta"] = round(new_beta, 6)
            posterior["decayed_at"] = self._now()
        return touched

    # ---------------------------------------------------------------- 周期调度
    def set_scheduler(
        self,
        *,
        enabled: bool | None = None,
        interval_days: float | None = None,
    ) -> dict[str, Any]:
        """在线开关周期调度 / 调整周期。返回更新后的 ``scheduler`` 段。

        **任何一次修改都会把到期时间按新周期从现在起重排**，理由有两个：

        - 把 7 天改成 1 天之后，如果还留着原来那个"7 天后的到期点"，用户会以为
          改了没生效 —— 这正是"配置改了但行为没变"最典型的坑。
        - 从"关闭"再打开时，若到期时间早已过期，重排能避免一开启就突然跑一轮
          （用户可能只是想恢复调度，并不希望立刻扰动生产）。
        """
        with self._lock:
            state = self._load()
            sch = state["scheduler"]
            changed = False
            if enabled is not None and bool(enabled) != bool(sch.get("enabled")):
                sch["enabled"] = bool(enabled)
                changed = True
            if interval_days is not None:
                interval = float(interval_days)
                if interval <= 0:
                    raise ValueError("进化周期必须大于 0 天")
                if interval != float(sch.get("interval_days") or 7):
                    sch["interval_days"] = interval
                    changed = True
            if changed:
                interval = float(sch.get("interval_days") or 7)
                sch["next_run_at"] = (
                    datetime.now(timezone.utc) + timedelta(days=interval)
                ).isoformat(timespec="seconds")
                self._save(state)
            return dict(sch)

    def scheduler_tick(self, *, now: datetime | None = None) -> dict[str, Any]:
        """调度器的一次检查：到点就跑一轮周期。返回本次做了什么。

        四件事按顺序定：

        1. **到期时间持久化在 state**（``scheduler.next_run_at``），不是内存变量 ——
           否则每次重启都把计时清零，"每 7 天一次"在频繁重启 / 多副本下永远等不到。
        2. **首次启用只排期、不立刻跑**：服务重启不该被理解成"该进化了"。
        3. **先占位再执行**：把 ``next_run_at`` 推进之后才去跑周期。多副本同时到点时
           只有一个进程抢得到；代价是进程若在周期中途崩溃，这一次就丢了 ——
           相比"两个进程同时进化同一批 Skill"，丢一次远比重复执行安全。
        4. **熔断期间不跑、也不推进到期时间**：解除熔断后立刻补跑一次。
           冻结是"暂停"，不是"静默跳过这一轮"。
        """
        now_dt = now or datetime.now(timezone.utc)
        with self._lock:
            state = self._load()
            sch = state["scheduler"]
            if not sch.get("enabled"):
                return {"action": "disabled", "next_run_at": sch.get("next_run_at")}
            # 允许小数天（如 0.5 = 12 小时、0.0007 ≈ 1 分钟），本地验证时刻把周期调短。
            interval = float(sch.get("interval_days") or 7)
            if interval <= 0:
                interval = 7.0
            due = _parse_iso(sch.get("next_run_at"))
            if due is None:
                sch["next_run_at"] = (now_dt + timedelta(days=interval)).isoformat(timespec="seconds")
                self._save(state)
                return {"action": "seeded", "next_run_at": sch["next_run_at"]}
            if now_dt < due:
                return {"action": "waiting", "next_run_at": sch["next_run_at"]}
            if (state.get("frozen") or {}).get("frozen"):
                return {"action": "frozen", "next_run_at": sch["next_run_at"]}
            # 占位：先推进到期时间并落盘，再放锁去跑周期。
            nxt = (now_dt + timedelta(days=interval)).isoformat(timespec="seconds")
            sch["next_run_at"] = nxt
            self._save(state)

        cycle = self.run_cycle()
        return {"action": "ran", "cycle": cycle, "next_run_at": nxt}

    def run_cycle(self, *, max_skills: int = 3) -> dict[str, Any]:
        """外环（周级主循环）：L0 观测 → L1 诊断 → L2 决策 → L3 建任务。

        只给**退化最严重的**几个 Skill 开任务（``max_skills`` 上限 + 元控制
        预算双闸）：一轮就把所有退化 Skill 都推一遍，等于同时扰动多个链路，
        出了问题根本归不了因。
        """
        cycle = self.refresh_state()
        with self._lock:
            state = self._load()
            names = sorted(set(CAPABILITY_SKILLS) | {
                str(x.get("skill_name")) for x in state["observations"] if x.get("skill_name")
            })
        health = [self.health(name) for name in names]
        degraded = sorted(
            [x for x in health if x["status"] == "degraded"],
            key=lambda x: (-len(x["signals"]), x["skill_name"]),
        )[:max_skills]

        created: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        for item in degraded:
            try:
                task = self.create_task({
                    "skill_name": item["skill_name"],
                    "base_version": item["active_version"],
                    "hypothesis": "周期主循环：按诊断信号生成候选",
                    "max_runs": DEFAULT_BUDGET["max_runs_per_task"],
                })
                created.append({"task_id": task["task_id"], "skill_name": task["skill_name"],
                                "status": task["status"], "patch_type": task.get("patch_type")})
            except ValueError as exc:
                skipped.append({"skill_name": item["skill_name"], "reason": str(exc)})

        return {
            "cycle": cycle,
            "degraded_skills": [x["skill_name"] for x in degraded],
            "tasks_created": created,
            "tasks_skipped": skipped,
            "next": "候选待回放验证（L4），通过后人工 Keep 才会写进版本账本",
        }

    def micro_cycle(self, task_id: str, metrics: dict[str, Any]) -> dict[str, Any] | None:
        """内环（亚周级微循环）：L3 根因 → L4 回放，快速迭代单个候选。"""
        task = self.get_task(task_id)
        if task is None:
            return None
        validation = self.run_task(task_id, metrics)
        if validation is None:
            return None
        skill = task["skill_name"]
        return {
            "task_id": task_id,
            "skill_name": skill,
            "status": validation["status"],
            "gate": (validation.get("result") or {}).get("gate"),
            "ab": (validation.get("result") or {}).get("ab"),
            "root_cause": self.root_cause(skill),
        }

    def meta(self) -> dict[str, Any]:
        """元控制（图 L5）：进化进度曝光 + 预算配额。"""
        with self._lock:
            state = self._load()
        usage = self._budget_usage(state)
        return {
            "scheduler": state["scheduler"],
            "frozen": state["frozen"],
            "budget": usage,
            "counts": {
                "observations": len(state["observations"]),
                "tasks": len(state["tasks"]),
                "cycles": len(state["cycles"]),
                "audit": len(state["audit"]),
                "feature_snapshots": len(state["feature_snapshots"]),
            },
            "safety": {
                "forbidden_tokens": list(execution.FORBIDDEN_TOKENS),
                "permissions_changed": False,
                "human_in_the_loop": True,
                "kill_switch": "POST /evolution/freeze",
            },
        }

    def _budget_usage(self, state: dict[str, Any]) -> dict[str, Any]:
        budget = {**DEFAULT_BUDGET, **(state.get("budget") or {})}
        window = features_mod.window_key()
        current = [x for x in state["tasks"] if _same_week(x.get("created_at"), window)]
        used = len(current)
        tokens_used = sum(int((x.get("budget") or {}).get("max_tokens") or 0) for x in current)
        return {
            "window": window,
            "max_tasks_per_cycle": int(budget["max_tasks_per_cycle"]),
            "max_tokens_per_cycle": int(budget["max_tokens_per_cycle"]),
            "tasks_used": used,
            "tokens_reserved": tokens_used,
            "tasks_remaining": max(0, int(budget["max_tasks_per_cycle"]) - used),
        }

    def north_star(
        self,
        *,
        _state: dict[str, Any] | None = None,
        _health: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """北极星指标（图右下）：周进化收益 / 漂移召回 / 先行指标覆盖 / 门禁通过率。

        ``_state`` / ``_health`` 是给 :meth:`overview` 复用的内部入参：overview 已经
        加载过 state、也算过每个 Skill 的 health，再让本方法重算一遍会让一次
        overview 变成 2×N 次健康诊断（每次又是一轮 PSI/KL/MMD 三检验）。
        复用后降到 N 次。单独调用（路由）时二者为 None，行为与原来一致。
        """
        if _state is None:
            with self._lock:
                _state = self._load()
        state = _state
        tasks = state["tasks"]
        decided = [x for x in tasks if x.get("decision")]
        kept = [x for x in decided if x["decision"] == "keep"]
        reverted = [x for x in decided if x["decision"] == "revert"]
        # 按 **result.gate 有没有落过** 判"验证过"，而不是看当前 status：
        # status 会被后续状态覆盖（Keep 之后是 active、灰度之后是 rolling_out），
        # 用它统计门禁通过率，会随着任务往前走而**越统计越少**。
        validated = [x for x in tasks if isinstance(x.get("result"), dict) and x["result"].get("gate")]
        gains = []
        for task in kept:
            metrics = (task.get("result") or {}).get("candidate_metrics") or {}
            reference = (task.get("result") or {}).get("baseline_metrics") or {}
            if metrics and reference:
                gains.append(float(metrics.get("success_rate") or 0) - float(reference.get("success_rate") or 0))

        if _health is not None:
            health = _health
        else:
            names = sorted(set(CAPABILITY_SKILLS) | {
                str(x.get("skill_name")) for x in state["observations"] if x.get("skill_name")
            })
            health = [self.health(name) for name in names]
        drifted = [x for x in health if "distribution_drift" in x["signals"]]
        with_lead = [x for x in health if x["early_warning"]["lead_signals"]]

        # keep 占比取**最新周期**的值，而不是全局累计：北极星指标看的是"这一轮
        # 螺旋走得怎么样"。全局累计会把上个月的票也算进来，涨跌就被抹平了 ——
        # 而环比恰恰是要看涨跌。
        cycle_ratio = state["cycles"][0].get("keep_ratio") if state["cycles"] else None
        keep_ratio = cycle_ratio if cycle_ratio is not None else (
            round(len(kept) / len(decided), 4) if decided else None
        )
        previous_ratio = state["cycles"][1].get("keep_ratio") if len(state["cycles"]) > 1 else None
        previous_value = float(previous_ratio) if previous_ratio is not None else None
        # 环比要的是**趋势**而不是当期的点值：一个恒在 0.9 的 keep 占比和
        # 一个从 0.4 爬到 0.9 的，含义完全不同。
        keep_ratio_delta = (
            round(keep_ratio - previous_value, 4)
            if keep_ratio is not None and previous_value is not None else None
        )

        reviews = state["reviews"]
        resolved = [x for x in reviews if x.get("status") == "resolved"]
        approved = sum(1 for x in resolved if x.get("verdict") == "approve")

        return {
            "weekly_gain": round(sum(gains) / len(gains), 6) if gains else None,
            "drift_recall": round(len(drifted) / len(health), 4) if health else None,
            "lead_signal_coverage": round(len(with_lead) / len(health), 4) if health else None,
            "gate_pass_rate": round(
                sum(1 for x in validated if (x["result"]["gate"] or {}).get("passed")) / len(validated), 4
            ) if validated else None,
            "keep_ratio": keep_ratio,
            "keep_ratio_delta": keep_ratio_delta,
            "revert_ratio": round(len(reverted) / len(decided), 4) if decided else None,
            "decided_tasks": len(decided),
            # 人在环是横切能力，但它有可量化的形态：待办积压与人工否决率。
            # 回滚率（revert_ratio）说明这轮螺旋走得稳不稳；人工否决率说明
            # 离线门禁漏放了多少 —— 两者一起看才知道该收紧门禁还是收紧抽样。
            "hitl": {
                "pending": sum(1 for x in reviews if x.get("status") == "pending"),
                "resolved": len(resolved),
                "approve_ratio": round(approved / len(resolved), 4) if resolved else None,
            },
            "cycle_count": len(state["cycles"]),
        }

    def _record_gain(self, state: dict[str, Any], skill_name: str, gain: float) -> None:
        """把本周收益追加到观测流，供下一窗口算 last_gain（回到 L0）。"""
        state["observations"].append({
            "observation_id": "obs_" + uuid.uuid4().hex[:12],
            "skill_name": skill_name,
            "version": state["active_versions"].get(skill_name, "current"),
            "verdict": "pass",
            "confidence": 1.0,
            "latency_ms": 0.0,
            "trace_id": "",
            "task_id": "",
            "error_type": "",
            "coverage": None,
            "freshness_days": None,
            "last_gain": round(min(1.0, max(0.0, gain)), 6),
            "tool_calls": 0,
            "created_at": self._now(),
            "user_id": None,
            "kind": "gain_marker",
        })
        state["observations"] = state["observations"][-MAX_OBSERVATIONS:]


# ================================================================ 模块级工具
# 当前窗最多取多少条。设上限是为了让"当前的样子"真的是最近的：
# 观测窗有 100 条，一半是 50 条 —— 再往回看就把上一周期的行为也算成当前了。
CURRENT_WINDOW_LIMIT = 50


def split_window(recent: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把观测窗切成 ``(基线段, 当前段)``。

    基线取更早的那半、当前取最近的那半。样本不足 4 条时两段相同
    （退化成"和自己比"），此时任何漂移/先行检验都不该报信号 ——
    这在 ``three_test_vote`` 与 ``early_warning`` 里各自有硬门槛兜着。
    """
    if len(recent) < 4:
        return recent, recent
    size = max(1, min(len(recent) // 2, CURRENT_WINDOW_LIMIT))
    return recent[:-size], recent[-size:]


def _tool_skill_map() -> dict[str, str]:
    return {
        name: skill.id
        for skill in CAPABILITY_SKILLS.values()
        for name in (skill.tool, *skill.dependencies)
    }


def _error_type(calls: list[dict[str, Any]]) -> str:
    """从失败的工具调用里取错误类型。

    读的是 ``error``（``ToolCallRecord`` 的字段名），不是 ``error_type`` ——
    后者在真实记录里不存在，读它会让所有失败都退化成同一个笼统的
    ``tool_failure``，而根因分析正是靠 ``error`` 里的关键词（timeout /
    degraded / coverage）来分流假设的。
    """
    for call in calls:
        if not call.get("ok") or call.get("degraded"):
            explicit = str(call.get("error") or "").strip()
            if explicit:
                return explicit[:120]
    for call in calls:
        if int(call.get("retries") or 0) > 0:
            return "retry_exhausted"
    return "tool_failure"


def _step_of(call: dict[str, Any], fallback: int) -> int:
    """取工具调用序号。真实字段是 ``step``（见 ``ToolCallRecord``）。"""
    for key in ("step", "index"):
        if key in call:
            value = _as_int(call.get(key))
            if value is not None:
                return value
    return int(fallback)


def _hitl_sample(task_id: str) -> bool:
    """HITL 抽样的**确定性**判定：同一条任务每次结果一致。

    用 ``task_id`` 的稳定哈希分桶，而不是 ``random()`` —— 抽样是"这条要不要
    人来看"的开关。用真随机会让同一任务重跑一次换个答案，抽查队列因此不可
    复现：测试随机红绿，人工事后追问"上次为什么没抽到"也无从回答。哈希分桶
    保留了随机分流的统计性质，同时是确定的。
    """
    if HITL_REVIEW_RATE <= 0:
        return False
    bucket = int(uuid.uuid5(uuid.NAMESPACE_URL, task_id).hex[:8], 16) % 100
    return bucket < int(round(HITL_REVIEW_RATE * 100))


def _thompson(alpha: float, beta: float) -> float:
    """Thompson 采样：从 Beta(alpha, beta) 抽一个数当排序分。

    ``max(0.5, ...)`` 是数值保护：``betavariate`` 要求两个参数都 > 0，
    后验在极端情况下可能被写成 0。
    """
    return random.betavariate(max(0.5, alpha), max(0.5, beta))


def _weight(item: dict[str, Any]) -> float:
    try:
        return float(item.get("confidence", 1.0))
    except (TypeError, ValueError):
        return 1.0


def _first(*values: Any) -> float:
    """返回第一个非空值（用于"实测优先，无实测用登记基线"）。"""
    for value in values:
        if value is not None:
            return float(value)
    return 0.0


def _same_week(raw: Any, window: str) -> bool:
    stamp = features_mod.parse_ts(raw)
    return stamp is not None and features_mod.window_key(stamp) == window


def _safe_name(skill_name: str) -> str:
    """Skill 名直接当文件名不安全（可能含 ``/`` ``:`` 等），先转义。"""
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in skill_name)[:80] or "unknown"


def _fmt(value: Any, digits: int = 6) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):.{digits}g}"
    except (TypeError, ValueError):
        return str(value)


def _bounded(value: Any, *, default: float | None) -> float | None:
    if value is None or value == "":
        return default
    return min(1.0, max(0.0, float(value)))


def _non_negative(value: Any, *, default: float | None) -> float | None:
    if value is None or value == "":
        return default
    return max(0.0, float(value))


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


evolution_service = EvolutionService()
