"""任务服务：把一次 Agent 运行持久化成 ``la_task`` 记录（方案 FR13 双端任务互通）。

为什么需要它：Harness 每跑完一个节点都会调 ``snapshot()`` 落一次快照，
EXE 端发起的任务要在 Web 端能看到进度与结果，任务中心要能回放。
这些都需要一个"把 State 落库"的地方 —— 就是这里。

三个刻意为之的设计：

1. **快照要裁剪，但必须如实声明裁掉了什么。**
   State 里躺着合同全文（``material_text`` 动辄几十万字）与 LLM 草稿，
   而每个节点结束都要写一次。全量落库的代价是 MySQL 被撑爆、
   以及 JSON 列撞上 ``max_allowed_packet``。所以这里对超长文本截断、
   对长列表限量，并把被处理的字段名记进 ``state["_truncated"]``。
   **不做静默截断** —— 静默截断会让"可回放"变成一句假话：
   下次照着快照回放，得到的是残缺输入，而没人知道少了什么。

2. **写入失败绝不上抛。** 快照是旁路观测，不是办案主线。
   数据库抖一下就让律师的分析任务失败，是本末倒置。

3. **未知任务 id 自动补建。** 图执行器可能在 ``create()`` 之前就开始跑了
   （例如脚本直接调 ``graph.run()``），此时若直接丢弃快照，
   任务中心里就会缺掉最关键的那一段过程。补建一行比丢数据好。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Iterable

from pydantic import BaseModel
from sqlalchemy import func, select

from backend.app.db.models.task import TaskRecord
from backend.app.db.session import session_scope

logger = logging.getLogger("legal_agent.services.task")

# 单个文本字段的落库上限。超过就截断并记录，见模块 docstring 第 1 条。
MAX_TEXT_CHARS = 20_000
# 列表字段的落库上限（Trace 可能上千条，全存没有意义）
MAX_LIST_ITEMS = 400
# 报告/草稿这类正文的上限（要能完整展示给律师，所以比一般文本宽松）
MAX_BODY_CHARS = 200_000

# --------------------------------------------------------------- 队列元数据
# 这两个键属于**队列**，但存在 ``state`` 这一列里（``state`` 是全项目唯一
# 一处"结构与内容都不固定"的落点，再为它们开一列不划算）。常量定义在这里
# 而不是 ``task_queue.py``：本模块才是 ``state`` 列的写入方，
# 而保住它们的逻辑（:func:`_carry_queue_meta`）必须和写入发生在同一个地方。
RUN_REQUEST_KEY = "run_request"
QUEUED_KEY = "queued"
QUEUE_META_KEYS = (RUN_REQUEST_KEY, QUEUED_KEY)

# 需要放宽上限的正文字段
BODY_FIELDS = ("report", "draft", "revision_draft")
# 截断时一律"掐头留尾"还是"留头"？留头：
# 报告与合同都是从头读的，尾部多是免责声明与格式尾巴，信息密度低。


def _carry_queue_meta(old_state: Any, new_state: dict[str, Any]) -> list[str]:
    """把队列元数据从旧快照搬到新快照上（就地改 ``new_state``），返回搬过来的键。

    **为什么必须有这一步。** ``state`` 列会被整体覆盖，而覆盖它的那一方
    （``harness.finish`` → :func:`snapshot`）传进来的是**图执行状态** ——
    里面当然没有 ``run_request``。于是任务第一次落快照之后，
    "这个任务是入过队的"这个标记就没了。

    后果不是报错，是**任务永远消失**：
    worker 执行到一半超时/异常时 :func:`task_queue.release` 会把它放回
    ``pending``，而 :func:`task_queue.peek` 只认带 ``run_request`` 的
    ``pending`` 行 —— 于是它静静躺在 ``pending`` 里，再也不被任何 worker 捞起，
    而 ``/health`` 上只表现为"pending 不为零"，看起来只是还没轮到。
    这个坑在本项目里已经以"静默丢失"的形式出现过三次（Trace 被覆盖、
    ``stored_path`` 泄露、``is_admin`` 缺失），所以这里用**保底搬运**
    而不是"每个调用方记得带上"。
    """
    if not isinstance(old_state, dict):
        return []
    carried: list[str] = []
    for key in QUEUE_META_KEYS:
        if old_state.get(key) and not new_state.get(key):
            new_state[key] = old_state[key]
            carried.append(key)
    return carried



def _jsonable(value: Any) -> Any:
    """把 State 里的值转成能进 JSON 列的形式。

    State 里可能出现 ``date``（法条生效日期）、``set``（去重用的集合）、
    pydantic 实体 —— SQLAlchemy 的 JSON 类型不认这些，会抛
    ``TypeError: Object of type date is not JSON serializable``，
    而报错点会在 commit 那一行，跟真正的来源隔着好几层调用。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    return str(value)


def _trim_list(items: list[Any]) -> list[Any]:
    out = [_jsonable(v) for v in items[:MAX_LIST_ITEMS]]
    if len(items) > MAX_LIST_ITEMS:
        # 截断要留痕，否则前端看到的条数与实际不符
        out.append({"_truncated": f"原始 {len(items)} 条，仅存前 {MAX_LIST_ITEMS} 条"})
    return out


def _strip_source_paths(value: Any) -> Any:
    """从（广义的）任务 ``result`` 里剥掉 ``source_path``。

    **为什么递归剥而不是只剥 ``result.statutes``。** 引用条目在哪一层是不固定的：
    实测同一份结果里 ``result.statutes[]``、``result.cases[]`` 都带，
    而换个意图可能就藏在别的键下。只按已知路径删，漏掉的那个就会一路带到前端 ——
    **半修比不修更糟**，因为问题还留着，却没人再去找了。

    按值递归的代价是每次序列化多一趟遍历，但 result 是单条任务的产物（KB 级），
    相对一次 HTTP 可以忽略。

    数据结构保持原样，只是删键；``result`` 里其余字段一个不动。
    """
    if isinstance(value, dict):
        return {k: _strip_source_paths(v) for k, v in value.items()
                if k != "source_path"}
    if isinstance(value, list):
        return [_strip_source_paths(v) for v in value]
    return value


def shrink_state(state: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """裁剪 State 供落库，返回 ``(裁剪后的 state, 被裁剪的字段名)``。"""
    from backend.agents.state import LIST_FIELDS

    out: dict[str, Any] = {}
    trimmed: list[str] = []

    for k, v in state.items():
        if k.startswith("_"):
            continue   # 运行时/诊断字段不进库
        if isinstance(v, str):
            limit = MAX_BODY_CHARS if k in BODY_FIELDS else MAX_TEXT_CHARS
            if len(v) > limit:
                out[k] = v[:limit]
                trimmed.append(f"{k}({len(v)}→{limit} 字符)")
            else:
                out[k] = v
        elif isinstance(v, (list, tuple, set, frozenset)):
            vals = list(v)
            out[k] = _trim_list(vals) if len(vals) > MAX_LIST_ITEMS or k in LIST_FIELDS else _jsonable(vals)
            if len(vals) > MAX_LIST_ITEMS:
                trimmed.append(f"{k}({len(vals)}→{MAX_LIST_ITEMS} 条)")
        else:
            out[k] = _jsonable(v)

    return out, trimmed


class TaskService:
    """``la_task`` 的读写封装。"""

    # ------------------------------------------------------------------ 写
    def create(
        self,
        task_id: str,
        *,
        kind: str = "chat",
        intent: str = "",
        query: str = "",
        case_id: str = "",
        user_id: int | None = None,
        team_id: int | None = None,
        source_client: str = "web",
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """建任务记录（幂等：已存在则更新关键字段并返回）。"""
        snapshot, _ = shrink_state(state or {})
        try:
            with session_scope() as db:
                row = db.execute(
                    select(TaskRecord).where(TaskRecord.task_id == task_id)
                ).scalar_one_or_none()
                if row is None:
                    row = TaskRecord(
                        task_id=task_id, case_id=case_id, user_id=user_id, team_id=team_id,
                        kind=kind, intent=intent, query=(query or "")[:MAX_TEXT_CHARS],
                        status="pending", progress=0, source_client=source_client,
                    )
                    db.add(row)
                    logger.info("任务已创建：%s（kind=%s client=%s）", task_id, kind, source_client)
                else:
                    # 同一 task_id 重复创建：更新而不是报错（重试/双端并发都可能撞上）
                    row.kind = kind or row.kind
                    row.intent = intent or row.intent
                    row.query = (query or row.query or "")[:MAX_TEXT_CHARS]
                    row.case_id = case_id or row.case_id
                    row.source_client = source_client or row.source_client
                if snapshot:
                    row.state = snapshot
                db.flush()
                return {"task_id": row.task_id, "status": row.status}
        except Exception as exc:  # noqa: BLE001
            logger.warning("任务创建失败（不影响主流程）：%s | %s", task_id, exc)
            return None

    def save_snapshot(
        self,
        *,
        task_id: str,
        status: str = "running",
        state: dict[str, Any] | None = None,
        trace: list[dict[str, Any]] | None = None,
        confidence: float = 0.0,
        citation_check: str = "pending",
        error: str = "",
        progress: int | None = None,
        result: dict[str, Any] | None = None,
        elapsed_ms: int = 0,
        finished: bool = False,
    ) -> bool:
        """落一次快照。返回是否写成功（失败只记日志，见模块 docstring 第 2 条）。"""
        state = state or {}
        snapshot, trimmed = shrink_state(state)

        try:
            with session_scope() as db:
                row = db.execute(
                    select(TaskRecord).where(TaskRecord.task_id == task_id)
                ).scalar_one_or_none()
                if row is None:
                    # 见模块 docstring 第 3 条：补建而不是丢弃
                    row = TaskRecord(
                        task_id=task_id,
                        case_id=str(state.get("case_id") or ""),
                        kind="chat",
                        intent=str(state.get("intent") or ""),
                        query=str(state.get("query") or "")[:MAX_TEXT_CHARS],
                        source_client="exe" if not state.get("user_id") else "api",
                    )
                    db.add(row)
                    db.flush()
                    logger.info("快照写入时补建任务记录：%s", task_id)

                if trimmed:
                    snapshot["_truncated"] = trimmed
                # 队列元数据必须在覆盖前搬回来，见 ``_carry_queue_meta``。
                _carry_queue_meta(row.state, snapshot)
                row.state = snapshot
                row.status = status or row.status
                row.confidence = float(confidence or 0.0)
                row.citation_check = citation_check or "pending"
                row.error = (error or "")[:MAX_TEXT_CHARS]
                row.intent = str(state.get("intent") or row.intent or "")
                row.trace = _trim_list(trace or [])
                row.tool_calls = _trim_list(list(state.get("tool_calls") or []))
                row.evidence_gap = _trim_list(list(state.get("evidence_gap") or []))
                # 进度**单调不减**。收尾归档会再落一次快照，而那时 route 已跑完、
                # Trace 里却没有全部节点的 step 事件（归档用的是新 Tracer），
                # 重算出来的进度会从 100 掉回十几 —— 进度条倒退在任何产品里都是 bug，
                # 而且它恰好发生"任务刚跑完"的时刻，看起来像任务出了问题。
                new_progress = self._progress(state) if progress is None else int(progress)
                row.progress = max(int(row.progress or 0), new_progress)

                if result:
                    row.result = _jsonable(result)
                if elapsed_ms:
                    row.elapsed_ms = int(elapsed_ms)
                if finished:
                    row.finished_at = datetime.now().isoformat(timespec="seconds")
                    row.progress = 100
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("任务快照写入失败（不影响主流程）：%s | %s", task_id, exc)
            return False

    def finish(
        self,
        task_id: str,
        *,
        status: str,
        state: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        elapsed_ms: int = 0,
        error: str = "",
    ) -> bool:
        """收尾：状态 + 最终结果 + 结束时间。"""
        state = state or {}
        return self.save_snapshot(
            task_id=task_id,
            status=status,
            state=state,
            trace=list(state.get("trace") or []),
            confidence=float(state.get("confidence") or 0.0),
            citation_check=str(state.get("citation_check") or "pending"),
            error=error or str(state.get("error") or ""),
            progress=100 if status in ("succeeded", "degraded", "failed") else None,
            result=result,
            elapsed_ms=elapsed_ms,
            finished=True,
        )

    def set_progress(self, task_id: str, progress: int, status: str = "running",
                     *, clear_error: bool = False) -> bool:
        """只更新进度（长任务的心跳，避免每次都写全量 State）。

        ``clear_error`` 只在**认领成功**时用：``claim`` 为了把"认领"做成
        一条带条件的 UPDATE，临时把 worker 标识写进 ``error`` 列，
        随后必须自己擦掉。默认不清 —— error 是给律师看的失败原因，
        让普通的心跳顺手把它清空，会把真实的失败原因抹掉。
        """
        try:
            with session_scope() as db:
                row = db.execute(
                    select(TaskRecord).where(TaskRecord.task_id == task_id)
                ).scalar_one_or_none()
                if row is None:
                    return False
                row.progress = max(0, min(100, int(progress)))
                if status:
                    row.status = status
                if clear_error:
                    row.error = ""
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("进度更新失败：%s | %s", task_id, exc)
            return False

    # ------------------------------------------------------------------ 读
    def get(self, task_id: str) -> dict[str, Any] | None:
        with session_scope() as db:
            row = db.execute(
                select(TaskRecord).where(TaskRecord.task_id == task_id)
            ).scalar_one_or_none()
            return self._to_dict(row) if row else None

    def state_of(self, task_id: str) -> dict[str, Any]:
        """取回 State 快照（供回放/续跑）。不存在时返回空 dict。"""
        row = self.get(task_id)
        return dict((row or {}).get("state") or {})

    def list(
        self,
        *,
        user_id: int | None = None,
        team_id: int | None = None,
        case_id: str = "",
        status: str = "",
        kind: str = "",
        limit: int = 50,
        offset: int = 0,
        with_trace: bool = False,
    ) -> list[dict[str, Any]]:
        """任务列表（默认按更新时间倒序）。

        ``with_trace=False`` 时**不返回** trace / state —— 列表页一次拉 50 条，
        带上每个任务的完整 Trace 会让响应体膨胀到几 MB，
        而列表页只显示状态与进度。需要细节时再调 get()。
        """
        limit = max(1, min(int(limit or 50), 200))
        stmt = select(TaskRecord)
        if user_id is not None:
            stmt = stmt.where(TaskRecord.user_id == user_id)
        if team_id is not None:
            stmt = stmt.where(TaskRecord.team_id == team_id)
        if case_id:
            stmt = stmt.where(TaskRecord.case_id == case_id)
        if status:
            stmt = stmt.where(TaskRecord.status == status)
        if kind:
            stmt = stmt.where(TaskRecord.kind == kind)
        stmt = stmt.order_by(TaskRecord.updated_at.desc()).limit(limit).offset(max(0, int(offset)))

        with session_scope() as db:
            rows = db.execute(stmt).scalars().all()
            return [self._to_dict(r, with_trace=with_trace) for r in rows]

    def count(self, **kw: Any) -> int:
        stmt = select(func.count()).select_from(TaskRecord)
        if kw.get("status"):
            stmt = stmt.where(TaskRecord.status == kw["status"])
        if kw.get("user_id") is not None:
            stmt = stmt.where(TaskRecord.user_id == kw["user_id"])
        if kw.get("case_id"):
            stmt = stmt.where(TaskRecord.case_id == kw["case_id"])
        with session_scope() as db:
            return int(db.execute(stmt).scalar_one() or 0)

    def stats(self, user_id: int | None = None) -> dict[str, Any]:
        """任务统计。``user_id`` 给定时只统计该用户的任务（律师端工作台用）。

        ⚠ **两条分支的字段必须完全一致**。早先非管理员分支只返回 total + by_status，
        而工作台的「平均置信度 / 引用拦截」读的是 avg_confidence / citation_blocked，
        结果这两张卡对律师永远是「—」和「0 次」——
        更糟的是"0 次"看着像真实统计（`|| 0` 把 undefined 兜成了 0），
        律师会以为自己从来没有被拦过引用。要么都给，要么两边都别给。
        """
        cond = [] if user_id is None else [TaskRecord.user_id == user_id]
        with session_scope() as db:
            rows = db.execute(
                select(TaskRecord.status, func.count()).where(*cond)
                .group_by(TaskRecord.status)
            ).all()
            total = int(db.execute(
                select(func.count()).select_from(TaskRecord).where(*cond)
            ).scalar_one_or_none() or 0)
            avg_conf = db.execute(
                select(func.avg(TaskRecord.confidence)).where(*cond)
            ).scalar_one_or_none()
            blocked = int(db.execute(
                select(func.count()).select_from(TaskRecord)
                .where(TaskRecord.citation_check == "failed", *cond)
            ).scalar_one_or_none() or 0)
        by_status = {str(s or ""): int(c or 0) for s, c in rows}
        return {
            "total": total,
            "by_status": by_status,
            "citation_blocked": blocked,
            "avg_confidence": round(float(avg_conf or 0.0), 3),
        }

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _progress(state: dict[str, Any]) -> int:
        """由"计划节点里已执行的比例"推进度。

        为什么按节点数而不是按耗时：耗时随材料大小变化几个数量级，
        用它算出来的进度条会在 90% 卡十几分钟。节点比例至少是单调可解释的。
        """
        route = [n for n in (state.get("route") or []) if isinstance(n, str)]
        if not route:
            return 0
        done: set[str] = set()
        for e in state.get("trace") or []:
            if isinstance(e, dict) and (e.get("event") or "") == "step":
                node = e.get("node") or ""
                if node:
                    done.add(node)
        executed = len([n for n in route if n in done])
        # 留 5% 给收尾（置信度计算、快照落库），避免进度条永远到不了头
        return max(0, min(99, int(executed / len(route) * 95)))

    @staticmethod
    def _to_dict(row: TaskRecord, *, with_trace: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {
            "task_id": row.task_id,
            "case_id": row.case_id,
            "user_id": row.user_id,
            "team_id": row.team_id,
            "kind": row.kind,
            "intent": row.intent,
            "query": row.query,
            "status": row.status,
            "progress": row.progress,
            "citation_check": row.citation_check,
            "confidence": row.confidence,
            "elapsed_ms": row.elapsed_ms,
            "source_client": row.source_client,
            "error": row.error,
            "evidence_gap": row.evidence_gap or [],
            "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
            "updated_at": row.updated_at.isoformat(timespec="seconds") if row.updated_at else "",
            "finished_at": row.finished_at or "",
            "result": row.result or {},
        }
        if with_trace:
            # **队列元数据不出接口。** 它们会被 :func:`_carry_queue_meta` 留在
            # 落库快照里（那是必要的，否则任务重试时认领不到），但 ``run_request``
            # 里带着 ``material_paths``（服务端绝对路径）与客户端 IP ——
            # 这和 ``stored_path`` / ``exported_path`` 是同一类泄露，
            # 而 ``state`` 是原样透出的，不在这里剥就会一路带到前端。
            # 前端要展示的 State 字段没有一个是这两个键。
            d["state"] = {k: v for k, v in (row.state or {}).items()
                          if k not in QUEUE_META_KEYS}
            # **``result`` 里的引用条目要剥掉 ``source_path``。** 它是语料文件
            # 在服务器上的绝对路径（`C:\...\data\raw\劳动法\劳动合同法.pdf`）：
            # 既泄露部署目录，类案文件名里还可能带当事人姓名。落库时保留有内部
            # 价值（追溯这条引用出自哪个文件），但出接口必须剥 —— 和上面
            # ``state`` 剥队列元数据是同一种处理：**只在边界上剥**。
            d["result"] = _strip_source_paths(row.result or {})
            d["trace"] = row.trace or []
            d["tool_calls"] = row.tool_calls or []
        return d


task_service = TaskService()

__all__ = ["MAX_LIST_ITEMS", "MAX_TEXT_CHARS", "QUEUED_KEY", "QUEUE_META_KEYS",
           "RUN_REQUEST_KEY", "TaskService", "_strip_source_paths",
           "iter_statuses", "shrink_state", "task_service"]


def iter_statuses() -> Iterable[str]:
    """任务状态枚举（前端筛选项与文档共用一处定义）。"""
    return ("pending", "running", "succeeded", "degraded", "failed")
