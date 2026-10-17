"""离线评测的指标计算。**纯函数，不碰数据库** —— 便于单测与复算。

设计原则：每个指标都要能回答"这个数字从哪来"，所以

1. **分母必须一起给。** 只报"引用可采信率 0.87"是没用的 ——
   它可能是"13 条里 11 条通过"，也可能是"100 条里 87 条通过"，
   而这两件事对"该不该信这份报告"的意义完全不同。所有指标都返回 ``(值, 分子, 分母)``。
2. **样本不足时给 ``None`` 而不是 0。** 零条引用时"可采信率"不是 0%，
   是"无从判断"。报 0 会让人以为引用全被拦了。
3. **降级单独统计，不混进成功率。** 一份"降级完成"的报告是可用交付物
   （它带着缺口声明），和"失败"不是一回事。混在一起会让降级看着像故障。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# ``Rate`` = (比率, 分子, 分母, 一句口径说明)。分母为 0 时比率为 None。
Rate = tuple[float | None, int, int, str]


def _rate(hit: int, total: int, caliber: str) -> Rate:
    return (None if total <= 0 else round(hit / total, 4), hit, total, caliber)


def as_dict(rate: Rate) -> dict[str, Any]:
    """转成可进 JSON 的形状。``None`` 保留为 ``None``（不是 0）。"""
    value, hit, total, caliber = rate
    return {"value": value, "hit": hit, "total": total, "caliber": caliber}


# --------------------------------------------------------------------------- 任务级
@dataclass
class TaskEval:
    """从一行 ``la_task`` 提取出的、评测用得上的字段。

    **只取交付物与 Trace，不取正文。** 报告正文动辄上万字，
    而评测要的是"引用过没过终审""有没有声明缺口""各节点花了多久"。
    把正文读进来只会让报告文件大到没人看。
    """

    task_id: str
    status: str
    intent: str
    kind: str
    elapsed_ms: int
    steps: int
    confidence: float
    citation_check: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    citation_summary: dict[str, Any] = field(default_factory=dict)
    evidence_gap: list[dict[str, Any]] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""


def task_eval_from_row(row: Any) -> TaskEval:
    """把 ``TaskRecord`` 摊平成 :class:`TaskEval`。"""
    result = row.result if isinstance(row.result, dict) else {}
    state = row.state if isinstance(row.state, dict) else {}
    return TaskEval(
        task_id=row.task_id,
        status=str(row.status or ""),
        intent=str(row.intent or ""),
        kind=str(row.kind or ""),
        elapsed_ms=int(row.elapsed_ms or 0),
        steps=int(state.get("steps") or 0),
        confidence=float(row.confidence or 0.0),
        citation_check=str(row.citation_check or "pending"),
        citations=list(result.get("citations") or []),
        citation_summary=dict(result.get("citation_summary") or {}),
        # ``result`` 里没有就用列（列是权威的：它是独立字段，不被结果裁剪影响）
        evidence_gap=list(result.get("evidence_gap") or row.evidence_gap or []),
        trace=list(row.trace or []),
        tool_calls=list(row.tool_calls or []),
        created_at=row.created_at.isoformat(timespec="seconds") if row.created_at else "",
    )


# --------------------------------------------------------------------------- 引用
def citation_trust(records: Iterable[TaskEval]) -> Rate:
    """引用可采信率 = 经终审核实为真的引用 / 全部引用。

    **这是本项目最重要的一个质量指标。** 一份报告可以写得很好、格式完美，
    但里面的法条如果状态已失效或被张冠李戴，它就是在给律师帮倒忙。
    """
    verified = total = 0
    for r in records:
        for c in r.citations:
            total += 1
            if c.get("verified"):
                verified += 1
    return _rate(verified, total, "verified=true 的引用 / 全部引用条目")


def citation_block(records: Iterable[TaskEval]) -> Rate:
    """引用拦截率 = 终审拦截的引用 / 全部送审引用。

    拦截不是坏事 —— 它说明防线起了作用。**这个数字应该看趋势而不是绝对值**：
    突然变成 0 更值得警惕（要么引用真的都干净，要么终审没跑）。
    """
    blocked = checked = 0
    for r in records:
        summary = r.citation_summary or {}
        checked += int(summary.get("checked") or 0)
        blocked += int(summary.get("blocking") or 0) + len(summary.get("rejected") or [])
    return _rate(blocked, checked, "blocking + rejected 条数 / checked 条数")


def citation_checked_rate(records: Iterable[TaskEval]) -> Rate:
    """有引用的任务里，真正跑过终审的比例。

    这条是给上一行的"保险"：拦截率变 0 有两种原因，
    而这条能把它们分开 —— 如果它也很低，说明终审压根没执行。
    """
    with_cites = ran = 0
    for r in records:
        if not r.citations:
            continue
        with_cites += 1
        if r.citation_check in ("passed", "failed"):
            ran += 1
    return _rate(ran, with_cites, "citation_check 为 passed/failed 的任务 / 有引用的任务")


# --------------------------------------------------------------------------- 缺口
def gap_rate(records: Iterable[TaskEval]) -> Rate:
    """证据缺口比例 = 声明了至少一条缺口的任务 / 全部任务。

    **不是越低越好。** 缺口是"这几条我没查到证据"的诚实声明；
    一份零缺口的复杂合同审查报告反而可疑 —— 要么真的查全了，
    要么该声明的地方没声明。
    """
    with_gap = total = 0
    for r in records:
        total += 1
        if r.evidence_gap:
            with_gap += 1
    return _rate(with_gap, total, "evidence_gap 非空的任务 / 全部任务")


def gap_by_kind(records: Iterable[TaskEval]) -> dict[str, int]:
    """缺口按类型计数（``tool`` / ``skill`` / ``document`` / ...）。

    按类型分开看才能定位：``tool`` 类缺口扎堆说明某个工具在挂，
    ``document`` 类扎堆说明材料本身不齐 —— 前者要修系统，后者要找当事人要材料。
    """
    out: dict[str, int] = {}
    for r in records:
        for g in r.evidence_gap:
            key = str(g.get("kind") or "unknown")
            out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# --------------------------------------------------------------------------- 状态
def status_distribution(records: Iterable[TaskEval]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in records:
        out[r.status] = out.get(r.status, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def degraded_rate(records: Iterable[TaskEval]) -> Rate:
    """降级完成比例。**单独算，不并进"失败"。**

    "降级完成"是**可用交付物**（带着缺口声明），和"失败"不是一回事。
    混在一起会让降级看着像故障，而它的实际含义是"结论范围缩小了"。
    """
    degraded = total = 0
    for r in records:
        total += 1
        if r.status == "degraded":
            degraded += 1
    return _rate(degraded, total, "status=degraded 的任务 / 全部任务")


def failure_rate(records: Iterable[TaskEval]) -> Rate:
    failed = total = 0
    for r in records:
        total += 1
        if r.status == "failed":
            failed += 1
    return _rate(failed, total, "status=failed 的任务 / 全部任务")


def confidence_stats(records: Iterable[TaskEval]) -> dict[str, Any]:
    """置信度分布。只看均值会漏掉"一半很高一半很低"这种双峰。"""
    vals = sorted(float(r.confidence or 0.0) for r in records)
    if not vals:
        return {"count": 0, "mean": None, "min": None, "p50": None, "max": None}
    n = len(vals)
    return {
        "count": n,
        "mean": round(sum(vals) / n, 4),
        "min": vals[0],
        "p50": vals[n // 2],
        "max": vals[-1],
    }


# --------------------------------------------------------------------------- 耗时
def node_latency(records: Iterable[TaskEval]) -> list[dict[str, Any]]:
    """各节点耗时分布。取**任何带 ``elapsed_ms`` 的 Trace 事件**按下表聚合。

    **为什么不是只取 ``event=step``。** 实测项目里的 ``step`` 事件
    ``elapsed_ms`` **恒为 0**（节点进入时记一条，还没跑完），
    真正带耗时的是 ``tool:<name>`` 事件。只按 ``step`` 取会让这一整块永远
    "没有数据" —— 而报告上显示"（Trace 里没有 step 事件的耗时数据）"
    看起来像数据缺失，实际是取错了字段。这是本轮实际踩到的：第一次跑评测
    这一节是空的，改成"有耗时就取"之后立刻能看出 `statute` 节点冷启动 17 秒。

    **按节点中位数排序而不是总耗时**：总耗时会放大"调用次数多"的节点
    （检索类天然次数多），而排查性能问题要看的是**单次**有多慢。
    所以给 ``p50`` 与 ``p95`` 两个数。

    **偶数样本取上中位数**（``vals[n // 2]``）。耗时指标宁可偏悲观：
    [100, 500] 报 100 会让人以为"有一半请求在 100ms 内"，报 500 才是
    "至少一半不快于 500ms"。样本量小的时候这个差别很明显。
    """
    buckets: dict[str, list[int]] = {}
    for r in records:
        for ev in r.trace:
            ms = int(ev.get("elapsed_ms") or 0)
            if ms <= 0:
                continue
            node = str(ev.get("node") or "") or str(ev.get("event") or "unknown")
            buckets.setdefault(node, []).append(ms)

    rows: list[dict[str, Any]] = []
    for node, vals in buckets.items():
        vals.sort()
        n = len(vals)
        rows.append({
            "node": node,
            "calls": n,
            "p50_ms": vals[n // 2],
            "p95_ms": vals[min(n - 1, int(n * 0.95))],
            "max_ms": vals[-1],
            "total_ms": sum(vals),
        })
    rows.sort(key=lambda d: -d["p50_ms"])
    return rows


def step_stats(records: Iterable[TaskEval]) -> dict[str, Any]:
    """步数分布。**接近上限的样本要单独点出来** ——
    那是"回环差点没跑完"的信号，而它不会表现为失败，只表现为结果偶尔变浅。
    """
    steps = sorted(int(r.steps or 0) for r in records if int(r.steps or 0) > 0)
    if not steps:
        return {"count": 0, "mean": None, "max": None}
    return {
        "count": len(steps),
        "mean": round(sum(steps) / len(steps), 2),
        "p50": steps[len(steps) // 2],
        "max": steps[-1],
    }


def tool_failures(records: Iterable[TaskEval]) -> list[dict[str, Any]]:
    """工具失败率。**按工具分开算** —— 混在一起时一个挂掉的工具
    会被另外十几个正常工具的调用量稀释到看不见。
    """
    buckets: dict[str, list[int]] = {}     # tool -> [总次数, 失败次数, 降级次数]
    for r in records:
        for call in r.tool_calls:
            tool = str(call.get("tool") or call.get("name") or "unknown")
            slot = buckets.setdefault(tool, [0, 0, 0])
            slot[0] += 1
            if call.get("ok") is False or call.get("error"):
                slot[1] += 1
            if call.get("degraded"):
                slot[2] += 1
    rows = [
        {
            "tool": tool,
            "calls": v[0],
            "failed": v[1],
            "degraded": v[2],
            "fail_rate": round(v[1] / v[0], 4) if v[0] else None,
        }
        for tool, v in buckets.items()
    ]
    rows.sort(key=lambda d: (-(d["fail_rate"] or 0), -d["calls"]))
    return rows


# --------------------------------------------------------------------------- 检索
def citation_identity(doc_name: str, article_no: str) -> str:
    """把「法规名 + 条文号」归一成一个可比较的标识，形如 ``劳动合同法#82``。

    **比较必须在归一之后做。** 语料里写「第八十七条」，模型输出常写「第87条」，
    直接比字符串会把同一条法条算成两个 —— 于是一个**正确的**引用会被评测判成错的，
    而评测结论会直接影响"要不要重新调检索"的决定。

    条文号归一复用 ``parser.structure.article_key``（项目里唯一的比较键实现）。
    法规名只去空格与书名号，不做简称映射 —— 简称映射需要一份人工维护的对照表，
    而那份表一旦过期，评测就会开始报假错。宁可让「劳动合同法」与
    「中华人民共和国劳动合同法」算成两条，也不要引入会过期的猜测。
    """
    from backend.parser.structure import article_key

    name = "".join((doc_name or "").split()).strip("《》")
    key = article_key(article_no)
    return f"{name}#{key}" if key else name


def retrieval_metrics(expected: set[str], hits: list[str], *, k: int = 10) -> dict[str, Any]:
    """检索质量：命中率 / 精确率 / 召回率 / MRR / 首个命中位次。

    ``expected`` 与 ``hits`` 都是 :func:`citation_identity` 归一后的标识。

    MRR 取首个命中的倒数位次：法条检索和网页搜索不一样，
    **第一条对不对比前十条里有没有**重要得多 —— 律师几乎只会看前几条。
    """
    top = list(hits[:k])
    hit_set = [h for h in top if h in expected]
    recall = (len(set(hit_set)) / len(expected)) if expected else None
    first = next((i + 1 for i, h in enumerate(top) if h in expected), 0)
    return {
        "expected": sorted(expected),
        "returned": top,
        "hit_count": len(hit_set),
        "precision": (round(len(hit_set) / len(top), 4) if top else None),
        "recall": (round(recall, 4) if recall is not None else None),
        "mrr": (round(1 / first, 4) if first else 0.0),
        "first_hit_rank": first,
        "k": k,
    }
