"""L4 验证守门层：同基线回放、指标门禁、三阶段灰度、版本账本。

对应「Skill 自进化飞轮」图 L4 的四格：

- **同基线回放**：新旧版本同 holdout，回放集 A/B 差分对照
- **指标门禁**：Δ成功率 ≥ ε 且零回退 → keep，否则 revert
- **灰度发布**：影子模式 → 小流量 → 全量，三段渐进
- **git 轮转**：版本库只进不退，回滚留痕，可审计

关于「git 轮转」：这里用**只追加的账本**（JSONL）而不是真的 git 仓库。
理由是 Skill 的"版本"是**配置逻辑版本**（``skill_config/v3``），不是源码
提交 —— 配置补丁本身就以 ``.patch.json`` 工件形式落盘，源码树并不变动。
账本保证的是同样的两件事：只进不退（历史条目永不改写）与回滚留痕
（revert 也追加一条，而不是删掉 keep 那条）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

# 三阶段灰度：(阶段名, 流量百分比)。
# 影子模式是 **0%**：候选只在旁路跑、结果不进用户视野。先要它是对的，
# 再谈给它流量。
GRAY_STAGES: tuple[tuple[str, int], ...] = (("shadow", 0), ("canary", 5), ("full", 100))

# 门禁默认容差 ε。要求"候选成功率不低于基线 + ε"而不是"持平即可"：
# 持平意味着这次进化没有收益，为此付一次灰度的风险不划算。
DEFAULT_EPSILON = 0.0
# 延迟允许的劣化比例。法律链路里 +20% 墙钟时间仍在律师可接受区间。
LATENCY_TOLERANCE = 1.2
# 覆盖率允许的劣化绝对值。
COVERAGE_TOLERANCE = 0.02


def stage_from_percent(percent: int) -> str:
    """把流量百分比映射回阶段名。未知百分比按最近的下一档处理。"""
    for name, value in GRAY_STAGES:
        if int(percent) <= value:
            return name
    return GRAY_STAGES[-1][0]


def next_stage(percent: int) -> dict[str, Any] | None:
    """返回下一档灰度；已在最后一档返回 None。"""
    for name, value in GRAY_STAGES:
        if int(percent) < value:
            return {"stage": name, "percent": value}
    return None


def stage_percent(stage: str) -> int | None:
    for name, value in GRAY_STAGES:
        if name == stage:
            return value
    return None


def ab_diff(
    baseline_metrics: dict[str, Any],
    candidate_metrics: dict[str, Any],
    *,
    holdout: str = "same_baseline",
) -> dict[str, Any]:
    """同基线回放的 A/B 差分对照。

    两边**必须来自同一个 holdout**，否则成功率差异里混着数据集差异，
    "候选更好"这个结论就不成立。这一点在返回值的 ``holdout`` 里显式写明，
    让下游能一眼看出这是同集对照还是跨集比数。
    """
    dimensions: dict[str, dict[str, Any]] = {}
    for key, direction in (("success_rate", "higher"), ("p95_latency_ms", "lower"),
                           ("coverage", "higher"), ("critical_failures", "lower")):
        base_value = _as_float(baseline_metrics.get(key))
        cand_value = _as_float(candidate_metrics.get(key))
        if base_value is None or cand_value is None:
            dimensions[key] = {"baseline": base_value, "candidate": cand_value,
                               "delta": None, "direction": direction, "verdict": "unknown"}
            continue
        delta = round(cand_value - base_value, 6)
        if abs(delta) < 1e-9:
            verdict = "tie"
        elif (delta > 0) == (direction == "higher"):
            verdict = "better"
        else:
            verdict = "worse"
        dimensions[key] = {"baseline": base_value, "candidate": cand_value,
                           "delta": delta, "direction": direction, "verdict": verdict}

    better = sum(item["verdict"] == "better" for item in dimensions.values())
    worse = sum(item["verdict"] == "worse" for item in dimensions.values())
    winner = "candidate" if better and not worse else ("baseline" if worse else "tie")
    return {"holdout": holdout, "dimensions": dimensions,
            "better": better, "worse": worse, "winner": winner}


def gate_checks(
    metrics: dict[str, Any],
    baseline: dict[str, Any],
    *,
    epsilon: float = DEFAULT_EPSILON,
    max_runs: int | None = None,
) -> dict[str, Any]:
    """指标门禁（图 L4「指标门禁」那一格）。

    六道闸，全部通过才允许推进灰度：Δ成功率、延迟、覆盖、零严重失败、
    预算、以及"不许有任何一维回退"。
    """
    success = _as_float(metrics.get("success_rate"))
    latency = _as_float(metrics.get("p95_latency_ms"))
    coverage = _as_float(metrics.get("coverage"))
    critical = int(metrics.get("critical_failures") or 0)
    run_count = int(metrics.get("run_count") or 0)

    base_success = _as_float(baseline.get("success_rate"))
    base_latency = _as_float(baseline.get("p95_latency_ms"))
    base_coverage = _as_float(baseline.get("coverage"))

    thresholds = {
        "min_success_rate": (base_success or 0.0) + epsilon,
        "max_p95_latency_ms": (base_latency or 0.0) * LATENCY_TOLERANCE,
        "min_coverage": max(0.0, (base_coverage or 0.0) - COVERAGE_TOLERANCE),
        "max_runs": max_runs,
        "epsilon": epsilon,
    }

    checks = {
        "delta_success_rate": success is not None and base_success is not None
        and success - base_success >= epsilon - 1e-9,
        "latency": latency is not None and thresholds["max_p95_latency_ms"] > 0
        and latency <= thresholds["max_p95_latency_ms"],
        "coverage": coverage is not None and base_coverage is not None
        and coverage >= thresholds["min_coverage"],
        "no_critical_failure": critical == 0,
        "budget": max_runs is None or run_count <= max_runs,
        "no_regression": all((
            success is None or base_success is None or success >= base_success - epsilon,
            latency is None or base_latency is None or latency <= base_latency * LATENCY_TOLERANCE,
            coverage is None or base_coverage is None or coverage >= base_coverage - COVERAGE_TOLERANCE,
        )),
    }
    failed = [name for name, ok in checks.items() if not ok]
    return {
        "checks": checks,
        "passed": not failed,
        "failed": failed,
        "thresholds": thresholds,
        "measured": {"success_rate": success, "p95_latency_ms": latency,
                     "coverage": coverage, "critical_failures": critical,
                     "run_count": run_count},
    }


class VersionLedger:
    """只进不退的版本账本（append-only JSONL）。

    刻意不提供 ``delete`` / ``update``：一旦允许改写历史，"回滚留痕"
    就退化成一个可被编辑的字段，审计价值归零。
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def append(self, entry: dict[str, Any]) -> dict[str, Any]:
        line = json.dumps(entry, ensure_ascii=False)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return entry

    def entries(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue  # 半截行（写入中断）跳过，不因一行坏数据废掉整本账
            if isinstance(parsed, dict):
                rows.append(parsed)
        return rows[-max(1, limit):][::-1]

    def for_skill(self, skill_name: str, limit: int = 20) -> list[dict[str, Any]]:
        return [x for x in self.entries(limit=500) if x.get("skill_name") == skill_name][:limit]

    def latest(self, skill_name: str) -> dict[str, Any] | None:
        items = self.for_skill(skill_name, limit=1)
        return items[0] if items else None


def write_tsv(path: Path, header: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    """把一行行状态**追加**到 results.tsv（图 L5「results.tsv 落盘」）。

    只追加、不带表头重写：TSV 是给人看的时间序列，中途插入表头会打断
    按时间扫读的习惯。表头在文件为空时写一次。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    empty = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8") as handle:
        if empty:
            handle.write("\t".join(header) + "\n")
        for row in rows:
            handle.write("\t".join(_tsv_cell(cell) for cell in row) + "\n")


def _tsv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6g}"
    text = str(value).replace("\t", " ").replace("\n", " ")
    return text


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
