"""L0 数据观测层的特征工程与窗口聚合。

对应「Skill 自进化飞轮」图 L0 的两格：

- **特征工程**：调用量 · 失败率 EWMA · 覆盖广度 · staleness · last_gain
- **窗口聚合**：把一个窗口（默认一周）的观测压成 feature store 快照

本模块**只做纯计算** —— 不读文件、不写文件、不碰全局状态。落盘由
``evolution_service`` 负责。这样同一条口径能被接口、脚本、测试同时复用，
不会出现"接口里算一个数、脚本里又算另一个数"的分叉。

分箱边界刻意用普通浮点数而不是 ``math.inf``：``json.dumps`` 会把 inf 写成
非标准 JSON 的 ``Infinity``，读回来虽然 Python 认得，但任何非 Python 的
消费者（前端、jq、下游服务）都会解析失败。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

# EWMA 的默认衰减系数。0.3 ≈ "最近 3 条观测的权重占一半"：
# 足够快地反映本周退化，又不至于让单条抖动直接翻掉结论。
DEFAULT_ALPHA = 0.3

# verdict 的三分法。unknown 既不算成功也不算失败 —— 把它算进分母会
# 让"没标注"稀释掉真实的成功率下降。
KNOWN_VERDICTS = ("pass", "fail", "degraded")
FAILED_VERDICTS = ("fail", "degraded")

# 分箱边界（上界用大数而不是 inf，理由见模块 docstring）。
LATENCY_EDGES: tuple[float, ...] = (0.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 1e9)
STEP_EDGES: tuple[float, ...] = (0.0, 1.0, 2.0, 4.0, 6.0, 8.0, 1e9)
COVERAGE_EDGES: tuple[float, ...] = (0.0, 0.5, 0.7, 0.85, 0.95, 1.0001)


# 参数键名的合法形态。中文值片段、路径、数字都会被这个模式挡掉。
PARAM_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def extract_param_keys(args_summary: Any) -> list[str]:
    """从 ``args_summary`` 里**只**抽出参数键名（图 L0「参数模式」的原料）。

    ``args_summary`` 由 ``state_manager._summarize_args`` 生成，形如
    ``query=劳动合同纠纷 top_k=5 docs[3]``：键值对以空格分隔，字符串值截到
    80 字符 —— 也就是说**值里既可能含空格，也可能含当事人信息**。直接按空格
    split 再取 ``=`` 左边，会把值里的中文片段当成键名（``与`` / ``李四``），
    那等于把正文悄悄写进了轨迹。所以只保留匹配标识符的片段：宁可漏记一个
    非常规键名，也不让正文片段落进 feature store 或 trace store。
    """
    keys: list[str] = []
    for part in str(args_summary or "").split():
        key = part.split("=", 1)[0].split("[", 1)[0].strip()
        if key and PARAM_KEY_PATTERN.match(key) and key not in keys:
            keys.append(key)
    return keys


def window_key(moment: datetime | None = None) -> str:
    """窗口标识，形如 ``2026-W41``（ISO 周）。

    用 ISO 周而不是"最近 7 天"：后者每来一条观测就整体平移，
    快照文件会一天一份、永远对不齐，也就没法做"同窗口对照"。
    """
    moment = moment or datetime.now(timezone.utc)
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


def parse_ts(raw: Any) -> datetime | None:
    """把观测里的 ISO 时间串解析成 aware datetime；解析不了返回 None。"""
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def ewma(values: Sequence[float], alpha: float = DEFAULT_ALPHA) -> float | None:
    """指数加权移动平均。``alpha`` 越大越看重近期观测。"""
    if not values:
        return None
    acc = float(values[0])
    for value in values[1:]:
        acc = alpha * float(value) + (1.0 - alpha) * acc
    return round(acc, 6)


def histogram(values: Iterable[float | None], edges: Sequence[float]) -> list[float]:
    """把数值落进分箱，返回**归一化**后的比例分布（长度 = 桶数）。

    返回比例而不是计数：基线要跟当前窗口对拍，两边样本量天然不等
    （基线是历史累积、当前是本窗口），计数直接比是没意义的。
    """
    bins = len(edges) - 1
    counts = [0] * bins
    total = 0
    for value in values:
        if value is None:
            continue
        counts[bucket_index(float(value), edges)] += 1
        total += 1
    if not total:
        return [0.0] * bins
    return [round(count / total, 6) for count in counts]


def bucket_index(value: float, edges: Sequence[float]) -> int:
    """定位 value 落在第几个桶。越界一律收进首尾桶（不丢样本）。"""
    for index in range(len(edges) - 1):
        if edges[index] <= value < edges[index + 1]:
            return index
    return 0 if value < edges[0] else len(edges) - 2


def percentile(values: Sequence[float], q: float) -> float | None:
    """升序样本的 q 分位（最近秩法）。空集合返回 None。"""
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    idx = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * q) - 1))
    return ordered[idx]


def feature_vector(
    observations: Sequence[dict[str, Any]],
    *,
    alpha: float = DEFAULT_ALPHA,
    now: datetime | None = None,
) -> dict[str, Any]:
    """把一个窗口内的观测压成特征向量（图 L0「特征工程」那一格）。"""
    now = now or datetime.now(timezone.utc)
    ordered = [x for x in observations if isinstance(x, dict)]

    failure_flags = [1.0 if x.get("verdict") in FAILED_VERDICTS else 0.0 for x in ordered]
    coverages = [float(x["coverage"]) for x in ordered if x.get("coverage") is not None]
    gains = [float(x["last_gain"]) for x in ordered if x.get("last_gain") is not None]
    latencies = [float(x.get("latency_ms") or 0.0) for x in ordered if x.get("latency_ms")]
    steps = [float(x.get("tool_calls") or 0.0) for x in ordered]

    last_pass: datetime | None = None
    for item in ordered:
        if item.get("verdict") == "pass":
            stamp = parse_ts(item.get("created_at"))
            if stamp is not None:
                last_pass = stamp
    staleness_days = None
    if last_pass is not None:
        staleness_days = round(max(0.0, (now - last_pass).total_seconds() / 86_400.0), 4)

    return {
        "call_volume": len(ordered),
        "failure_rate_ewma": ewma(failure_flags, alpha),
        "coverage_breadth": round(sum(coverages) / len(coverages), 6) if coverages else None,
        "staleness_days": staleness_days,
        "last_gain": round(gains[-1], 6) if gains else None,
        "mean_gain": round(sum(gains) / len(gains), 6) if gains else None,
        "latency_p95_ms": percentile(latencies, 0.95),
        "mean_steps": round(sum(steps) / len(steps), 4) if steps else None,
        "window": window_key(now),
    }


def success_baseline(
    observations: Sequence[dict[str, Any]],
    *,
    traces: Sequence[dict[str, Any]] = (),
    now: datetime | None = None,
) -> dict[str, Any]:
    """成功基线建模（图 L1「成功基线建模」那一格）。

    只拿 **pass 的观测**建模 —— 拿全体建模会把退化期的特征也当成"正常长相"，
    基线随之下沉，退化就再也检不出来了（这正是漂移检测失效的常见原因）。
    四维：步数直方图 / 工具组合 / 参数模式 / 时长直方图。
    """
    now = now or datetime.now(timezone.utc)
    passing = [x for x in observations if isinstance(x, dict) and x.get("verdict") == "pass"]

    steps = [float(x.get("tool_calls") or 0.0) for x in passing]
    durations = [float(x.get("latency_ms") or 0.0) for x in passing if x.get("latency_ms")]
    coverages = [float(x["coverage"]) for x in passing if x.get("coverage") is not None]

    tool_mix: Counter[str] = Counter()
    param_pattern: Counter[str] = Counter()
    for trace in traces:
        if not isinstance(trace, dict) or not trace.get("ok"):
            continue
        for step in trace.get("steps") or []:
            if not isinstance(step, dict):
                continue
            tool = str(step.get("tool") or "").strip()
            if not tool:
                continue
            tool_mix[tool] += 1
            keys = step.get("param_keys") or []
            if keys:
                param_pattern["+".join(sorted(str(k) for k in keys))] += 1

    return {
        "sample_count": len(passing),
        "steps": histogram(steps, STEP_EDGES),
        "duration": histogram(durations, LATENCY_EDGES),
        "coverage": histogram(coverages, COVERAGE_EDGES),
        "tool_mix": _normalize(tool_mix),
        "param_pattern": _normalize(param_pattern),
        "mean_steps": round(sum(steps) / len(steps), 4) if steps else None,
        "p95_latency_ms": percentile(durations, 0.95),
        "coverage_edges": list(COVERAGE_EDGES),
        "step_edges": list(STEP_EDGES),
        "latency_edges": list(LATENCY_EDGES),
        "built_at": now.isoformat(timespec="seconds"),
    }


def _normalize(counter: Counter[str]) -> dict[str, float]:
    total = sum(counter.values())
    if not total:
        return {}
    return {key: round(value / total, 6) for key, value in counter.most_common(20)}


def failure_rate_series(observations: Sequence[dict[str, Any]]) -> list[float]:
    """逐条失败标志（0/1），供漂移检验对拍用。"""
    return [1.0 if x.get("verdict") in FAILED_VERDICTS else 0.0 for x in observations if isinstance(x, dict)]


def numeric_series(observations: Sequence[dict[str, Any]], field: str) -> list[float]:
    """抽取某个数值字段的序列（跳过空值），供 PSI/KL/MMD 使用。"""
    out: list[float] = []
    for item in observations:
        if not isinstance(item, dict):
            continue
        value = item.get(field)
        if value is None or value == "":
            continue
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            continue
    return out
