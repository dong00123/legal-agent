"""L1 健康诊断层的漂移检测与先行退化信号。

对应「Skill 自进化飞轮」图 L1 的两格：

- **漂移检测**：PSI · MMD · KL，三检验投票，超阈值触发
- **先行退化信号**：拿当前特征去比成功基线，在 fail 之前先报警

**为什么是三检验投票而不是单指标。** PSI 对分箱方式敏感、KL 对零概率敏感、
MMD 对样本量与核宽敏感 —— 任何一个单独用，都会在正常波动上误报，而误报的
代价是拿"其实没坏"的 Skill 去做一轮进化，白烧预算还扰动生产。三个里至少
两个同时超阈值才判漂移：代价是漏掉一些轻微漂移，换来的是"报了基本就值得看"。
可用检验不足两项时（样本不够）**一律不判漂移** —— 孤证不立。

先行退化信号是另一回事：它不等漂移成立，而是直接问"这一窗口的形态还像不像
成功的样子"。所以它宁可多报：它只用于**排序候选、提前预警**，不直接触发
灰度或上线。
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Iterable, Sequence

from backend.app.services.evolution_features import (
    FAILED_VERDICTS,
    KNOWN_VERDICTS,
    failure_rate_series,
    histogram,
    numeric_series,
)

try:  # pragma: no cover - numpy 是项目直接依赖，缺失只关掉 MMD 一项
    import numpy as _np
except ImportError:  # pragma: no cover
    _np = None

# ------------------------------------------------------------------ 阈值
PSI_THRESHOLD = 0.20
KL_THRESHOLD = 0.10
MMD_THRESHOLD = 0.05
DRIFT_VOTES_REQUIRED = 2
# 三检验需要的最小样本量。比"分布漂移"（TV 距离）的 20 条低：
# 数值型检验对样本量的敏感度不同，12 条已经能稳定给出量级判断。
MIN_TRIPLE_SAMPLES = 12
# TV 距离用的最小样本量，与改造前的行为保持一致。
MIN_DRIFT_SAMPLES = 20
# MMD 是 O(n²)，截断样本量防止大窗口把接口拖死。
MMD_MAX_SAMPLES = 200
DEFAULT_BUCKETS = 10
EPS = 1e-6

# 先行退化信号的判据（比硬阈值松，因为它只用于排序和预警）。
STEP_INFLATION_RATIO = 1.35
DURATION_DRIFT_RATIO = 1.30
COVERAGE_DIP = 0.85
COVERAGE_DROP = 0.05
FAILURE_RATE_RISING = 0.15
STALENESS_DAYS = 7.0

DEFAULT_THRESHOLDS = {"psi": PSI_THRESHOLD, "kl": KL_THRESHOLD, "mmd": MMD_THRESHOLD}


# ------------------------------------------------------------------ 单检验
def psi(base: Sequence[float], current: Sequence[float], *, buckets: int = DEFAULT_BUCKETS) -> float | None:
    """Population Stability Index。

    分箱边界取自**合并样本**的分位点，而不是等宽切分：延迟这类长尾分布里，
    等宽分箱会把 90% 的样本塞进第一个桶，剩下九个桶全是 0 —— PSI 在这种
    分箱下对尾部变化几乎无感（而尾部恰恰是退化最先出现的地方）。

    为什么用合并样本而不是只用基线：基线段样本少时（例如上周全是 500ms），
    基线自身的分位点会退化成几个重复值，"当前段整体平移到 2600ms"会被
    分到同一个桶里，PSI 报出 0 —— **最严重的漂移反而检不出来**。合并分箱
    保证边界覆盖两段的取值范围，退化的那张牌就不存在了。
    """
    if len(base) < MIN_TRIPLE_SAMPLES or len(current) < MIN_TRIPLE_SAMPLES:
        return None
    edges = _quantile_edges(list(base) + list(current), buckets)
    baseline_ratio = histogram(base, edges)
    current_ratio = histogram(current, edges)
    return round(sum((b - a) * math.log((b + EPS) / (a + EPS))
                     for a, b in zip(baseline_ratio, current_ratio)), 6)


def kl_divergence(base: Sequence[float], current: Sequence[float], *, buckets: int = DEFAULT_BUCKETS) -> float | None:
    """对称化的 KL 散度（KL(p‖q) + KL(q‖p) 的一半）。

    取对称版本：单方向的 KL 在"基线窄、当前宽"和"基线宽、当前窄"两种情况下
    数值差一个量级，同一个阈值没法同时管住两边。分箱与 PSI 共用合并样本口径，
    两个检验才是对同一个划分发表意见，投票才有意义。
    """
    if len(base) < MIN_TRIPLE_SAMPLES or len(current) < MIN_TRIPLE_SAMPLES:
        return None
    edges = _quantile_edges(list(base) + list(current), buckets)
    a = histogram(base, edges)
    b = histogram(current, edges)
    return round(0.5 * (_kl(a, b) + _kl(b, a)), 6)


def mmd_rbf(base: Sequence[float], current: Sequence[float]) -> float | None:
    """RBF 核的最大均值差异平方（MMD²）。

    先在**合并样本**上做标准化再算核 —— 否则"延迟从 800ms 涨到 1600ms"
    这种整体平移会被核宽吃掉，报出一个漂亮的小数值。
    """
    if _np is None or len(base) < MIN_TRIPLE_SAMPLES or len(current) < MIN_TRIPLE_SAMPLES:
        return None

    x = _np.asarray(list(base)[:MMD_MAX_SAMPLES], dtype=float).reshape(-1, 1)
    y = _np.asarray(list(current)[:MMD_MAX_SAMPLES], dtype=float).reshape(-1, 1)
    pooled = _np.concatenate([x, y], axis=0)
    center, scale = float(pooled.mean()), float(pooled.std())
    if scale < EPS:
        # 全体取值几乎相同：只要两段不是同一个值，就是完全分离的分布。
        return 0.0 if abs(float(x.mean()) - float(y.mean())) < EPS else 1.0
    x = (x - center) / scale
    y = (y - center) / scale

    gamma = _median_heuristic(x, y)
    kxx = _rbf_kernel(x, x, gamma).mean()
    kyy = _rbf_kernel(y, y, gamma).mean()
    kxy = _rbf_kernel(x, y, gamma).mean()
    return round(max(0.0, float(kxx + kyy - 2.0 * kxy)), 6)


def three_test_vote(
    base: Sequence[float],
    current: Sequence[float],
    *,
    thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    """同一维度上的三检验投票。可用检验不足两项时判不漂移。"""
    limits = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    statistics = {
        "psi": psi(base, current),
        "kl": kl_divergence(base, current),
        "mmd": mmd_rbf(base, current),
    }
    votes = {
        "psi": statistics["psi"] is not None and statistics["psi"] >= limits["psi"],
        "kl": statistics["kl"] is not None and statistics["kl"] >= limits["kl"],
        "mmd": statistics["mmd"] is not None and statistics["mmd"] >= limits["mmd"],
    }
    available = [name for name, value in statistics.items() if value is not None]
    vote_count = sum(votes.values())
    return {
        "statistics": statistics,
        "votes": votes,
        "vote_count": vote_count,
        "available": available,
        "detected": len(available) >= DRIFT_VOTES_REQUIRED and vote_count >= DRIFT_VOTES_REQUIRED,
        "thresholds": limits,
        "sample_size": {"baseline": len(base), "current": len(current)},
    }


# ------------------------------------------------------------------ 多维汇总
def drift_report(
    baseline_observations: Sequence[dict[str, Any]],
    current_observations: Sequence[dict[str, Any]],
    *,
    thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    """对失败率 / 延迟 / 覆盖三个维度分别投票，再给一个可读的汇总。"""
    dimensions = {
        "failure_rate": three_test_vote(
            failure_rate_series(baseline_observations),
            failure_rate_series(current_observations),
            thresholds=thresholds,
        ),
        "latency_ms": three_test_vote(
            numeric_series(baseline_observations, "latency_ms"),
            numeric_series(current_observations, "latency_ms"),
            thresholds=thresholds,
        ),
        "coverage": three_test_vote(
            numeric_series(baseline_observations, "coverage"),
            numeric_series(current_observations, "coverage"),
            thresholds=thresholds,
        ),
    }
    drifted = [name for name, item in dimensions.items() if item["detected"]]
    return {
        "dimensions": dimensions,
        "drifted_dimensions": drifted,
        "detected": bool(drifted),
        # 全维度都检不出来时才算"检验不可用"，单维样本不足不算。
        "insufficient_samples": all(not item["available"] for item in dimensions.values()),
    }


def verdict_drift(items: Sequence[dict[str, Any]]) -> float | None:
    """verdict 分布的前后半段总变差距离（TV）。

    这是改造前就在用的"分布漂移"信号，保留它是为了**不改变既有行为**：
    新加的三检验是补充证据，不是替换。
    """
    if len(items) < MIN_DRIFT_SAMPLES:
        return None
    middle = len(items) // 2
    left, right = list(items[:middle]), list(items[middle:])
    if not left or not right:
        return None
    labels = ("pass", "fail", "degraded", "unknown")
    a, b = Counter(x.get("verdict") for x in left), Counter(x.get("verdict") for x in right)
    return round(0.5 * sum(abs(a[k] / len(left) - b[k] / len(right)) for k in labels), 4)


# ------------------------------------------------------------------ 先行信号
def early_warning(features: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """先行退化信号：「长得不像成功的样子」，早于 fail 的一步预警。

    输入是 L0 的特征向量与 L1 的成功基线，输出是一串**带证据**的信号 ——
    每条都能说清"哪个数超了哪个基线多少"，而不是一个黑箱分数。
    """
    signals: list[dict[str, Any]] = []

    mean_steps = _as_float(features.get("mean_steps"))
    base_steps = _as_float(baseline.get("mean_steps"))
    if mean_steps is not None and base_steps and mean_steps > base_steps * STEP_INFLATION_RATIO:
        signals.append({
            "code": "step_inflation",
            "severity": "warn",
            "detail": f"平均步数 {mean_steps:.1f} 高于成功基线 {base_steps:.1f} 的 "
                      f"{STEP_INFLATION_RATIO:.0%} 以上，像是在绕路。",
        })

    p95 = _as_float(features.get("latency_p95_ms"))
    base_p95 = _as_float(baseline.get("p95_latency_ms"))
    if p95 is not None and base_p95 and p95 > base_p95 * DURATION_DRIFT_RATIO:
        signals.append({
            "code": "duration_drift",
            "severity": "warn",
            "detail": f"P95 延迟 {p95:.0f}ms 超过成功基线 {base_p95:.0f}ms 的 "
                      f"{DURATION_DRIFT_RATIO:.0%}。",
        })

    coverage = _as_float(features.get("coverage_breadth"))
    if coverage is not None and coverage < COVERAGE_DIP:
        signals.append({
            "code": "coverage_dip",
            "severity": "alert" if coverage < COVERAGE_DIP - COVERAGE_DROP else "warn",
            "detail": f"覆盖广度 {coverage:.2f} 低于 {COVERAGE_DIP:.2f}，召回可能已经在缩。",
        })

    failure_rate = _as_float(features.get("failure_rate_ewma"))
    if failure_rate is not None and failure_rate >= FAILURE_RATE_RISING:
        signals.append({
            "code": "failure_rate_rising",
            "severity": "alert",
            "detail": f"失败率 EWMA {failure_rate:.2%} 已达预警线 {FAILURE_RATE_RISING:.0%}，"
                      f"尚未跌破成功率门槛，但方向明确。",
        })

    staleness = _as_float(features.get("staleness_days"))
    if staleness is not None and staleness > STALENESS_DAYS:
        signals.append({
            "code": "staleness",
            "severity": "warn",
            "detail": f"距最近一次成功已 {staleness:.1f} 天，知识快照可能过期。",
        })

    last_gain = _as_float(features.get("last_gain"))
    if last_gain is not None and last_gain <= 0:
        signals.append({
            "code": "gain_exhausted",
            "severity": "warn",
            "detail": "最近一次进化收益为 0，继续在同一方向加码大概率无效。",
        })

    alerts = sum(item["severity"] == "alert" for item in signals)
    return {
        "looks_like_success": not signals,
        "lead_signals": signals,
        "signal_codes": [item["code"] for item in signals],
        "alert_count": alerts,
        "risk": round(min(1.0, 0.2 * len(signals) + 0.15 * alerts), 4),
    }


# ------------------------------------------------------------------ 内部工具
def _quantile_edges(values: Sequence[float], buckets: int) -> list[float]:
    """按样本分位点切分箱边界（保证严格递增且覆盖全量）。

    边界**跳过重复值**而不是硬凑到 ``buckets + 1`` 个：取值本身离散
    （比如覆盖率只有 0.6/0.9 两档）时，硬凑会造出一堆零宽桶，
    比例永远为 0，反而给 PSI 贡献噪声。
    """
    ordered = sorted(float(v) for v in values)
    n = len(ordered)
    edges: list[float] = [ordered[0] - EPS]
    for i in range(1, buckets):
        candidate = ordered[min(n - 1, int(n * i / buckets))]
        if candidate > edges[-1]:
            edges.append(candidate)
    edges.append(ordered[-1] + EPS)
    return edges


def _kl(p: Sequence[float], q: Sequence[float]) -> float:
    return sum((pi + EPS) * math.log((pi + EPS) / (qi + EPS)) for pi, qi in zip(p, q))


def _rbf_kernel(a: Any, b: Any, gamma: float) -> Any:
    diff = a - b.T
    return _np.exp(-gamma * (diff ** 2))


def _median_heuristic(x: Any, y: Any) -> float:
    """核宽的中位数启发式：取合并样本两两距离中位数的倒数。"""
    pooled = _np.concatenate([x, y], axis=0)
    if len(pooled) > 100:
        pooled = pooled[:: max(1, len(pooled) // 100)]
    diff = pooled - pooled.T
    distances = _np.sqrt(_np.maximum(diff ** 2, 0.0))
    median = float(_np.median(distances[distances > 0])) if (distances > 0).any() else 1.0
    if not math.isfinite(median) or median <= EPS:
        return 1.0
    return 1.0 / (2.0 * median ** 2)


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def summarize_verdicts(observations: Iterable[dict[str, Any]]) -> dict[str, int]:
    """verdict 计数（供诊断层与接口共用）。"""
    counter = Counter(
        str(x.get("verdict")) if x.get("verdict") in KNOWN_VERDICTS else "unknown"
        for x in observations
        if isinstance(x, dict)
    )
    return {label: counter.get(label, 0) for label in (*KNOWN_VERDICTS, "unknown")}


__all__ = [
    "FAILED_VERDICTS",
    "drift_report",
    "early_warning",
    "kl_divergence",
    "mmd_rbf",
    "psi",
    "summarize_verdicts",
    "three_test_vote",
    "verdict_drift",
]
