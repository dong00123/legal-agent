"""评测模块回归测试。

评测的价值全在**数字可信**上，所以这里守的是三类会让数字失真的东西：

1. **分母为 0 时不能报 0。** 0 条引用时"可采信率"不是 0%，是"无从判断"。
   报 0 会让看报告的人以为引用全被拦了 —— 而真相是这次压根没有引用。
2. **测试数据不能混进真实样本。** 测试夹具的任务会被记成 failed，
   混进来之后"本周失败率 34%"里的绝大多数都是假的。
3. **比较键必须归一。** 「第八十二条」与「第82条」是同一法条；
   直接比字符串会把**正确的**引用判成错的，而评测结论会直接影响要不要重调检索。
"""

from __future__ import annotations

import pytest

from backend.evals import metrics as M
from backend.evals import runner


def _task(**kw) -> M.TaskEval:
    base = dict(task_id="task_0123456789ab", status="succeeded", intent="consult",
                kind="chat", elapsed_ms=1000, steps=5, confidence=0.8,
                citation_check="passed")
    base.update(kw)
    return M.TaskEval(**base)


def _cite(verified: bool = True, article: str = "第八十二条") -> dict:
    return {"raw": f"《劳动合同法》{article}", "name": "《劳动合同法》",
            "type": "statute", "article": article, "verified": verified,
            "status": "现行有效"}


# --------------------------------------------------------------------------- 分母
def test_rate_is_none_not_zero_when_denominator_empty() -> None:
    """核心：分母 0 时比率为 ``None``，不是 ``0``。"""
    value, hit, total, _ = M.citation_trust([])
    assert value is None and hit == 0 and total == 0
    assert M.as_dict((value, hit, total, "x"))["value"] is None


def test_citation_trust_counts_verified_only() -> None:
    records = [
        _task(citations=[_cite(True), _cite(False)]),
        _task(citations=[_cite(True)]),
    ]
    value, hit, total, caliber = M.citation_trust(records)
    assert (hit, total) == (2, 3)
    assert value == pytest.approx(0.6667, abs=1e-4)
    assert caliber, "口径说明不能空 —— 数字必须能对上来源"


def test_block_rate_sums_blocking_and_rejected() -> None:
    """拦截有两种记法（``blocking`` 计数与 ``rejected`` 列表），都要算进去。"""
    records = [_task(citation_summary={"checked": 10, "blocking": 2,
                                       "rejected": [{"raw": "a"}, {"raw": "b"}]})]
    value, hit, total, _ = M.citation_block(records)
    assert (hit, total) == (4, 10)
    assert value == 0.4


def test_validator_ran_rate_separates_zero_block_from_no_validator() -> None:
    """拦截率变 0 有两种原因，这条负责把它们分开。

    有引用但终审没跑（``pending``）的任务不该进分母 —— 它既不是"过了"也不是"没过"。
    """
    records = [
        _task(citations=[_cite()], citation_check="passed"),
        _task(citations=[_cite()], citation_check="pending"),   # 终审没跑
        _task(citations=[], citation_check="pending"),           # 压根没引用
    ]
    value, hit, total, _ = M.citation_checked_rate(records)
    assert (hit, total) == (1, 2), "没引用的任务不该进分母"


# --------------------------------------------------------------------------- 结局
def test_degraded_and_failed_are_separate() -> None:
    """降级完成是**可用交付物**，不能并进失败 —— 混算会让降级看着像故障。"""
    records = [_task(status="degraded"), _task(status="failed"), _task(status="succeeded")]
    assert M.degraded_rate(records)[1] == 1
    assert M.failure_rate(records)[1] == 1
    assert M.status_distribution(records)["degraded"] == 1


def test_confidence_stats_reveal_bimodal() -> None:
    """只看均值会漏掉"一半很高一半很低" —— 所以要给分位数。"""
    records = [_task(confidence=v) for v in (0.9, 0.9, 0.1, 0.1)]
    stats = M.confidence_stats(records)
    assert stats["mean"] == pytest.approx(0.5)
    assert stats["min"] == 0.1 and stats["max"] == 0.9


def test_gap_by_kind_ranks_by_count() -> None:
    records = [
        _task(evidence_gap=[{"kind": "tool"}, {"kind": "tool"}]),
        _task(evidence_gap=[{"kind": "document"}]),
    ]
    kinds = M.gap_by_kind(records)
    assert list(kinds) == ["tool", "document"]
    assert kinds["tool"] == 2


# --------------------------------------------------------------------------- 耗时
def test_node_latency_uses_events_with_elapsed_ms() -> None:
    """耗时取"任何带 ``elapsed_ms`` 的事件"，**不是只取 step**。

    项目里 ``step`` 事件的 ``elapsed_ms`` 恒为 0（节点进入时记的），
    只按它取会让这一整块永远是空的 —— 而报告上看起来像"数据缺失"。
    """
    records = [_task(trace=[
        {"node": "IntentNode", "event": "step", "elapsed_ms": 0},
        {"node": "statute", "event": "tool:statute_search", "elapsed_ms": 500},
        {"node": "statute", "event": "tool:statute_search", "elapsed_ms": 100},
    ])]
    rows = M.node_latency(records)
    assert [r["node"] for r in rows] == ["statute"], "step 事件（0ms）不该产生一行"
    assert rows[0]["calls"] == 2
    # 偶数样本取**上中位数**（这里是 500 不是 100）。耗时指标宁可偏悲观：
    # [100, 500] 报 100 会让人以为"有一半请求都在 100ms 内"。
    assert rows[0]["p50_ms"] == 500 and rows[0]["max_ms"] == 500
    assert rows[0]["total_ms"] == 600


def test_node_latency_sorts_by_single_call_p50() -> None:
    """按单次 p50 排序：总耗时会放大调用次数多的节点，看不出"单次有多慢"。"""
    records = [_task(trace=[
        {"node": "cheap", "event": "tool:a", "elapsed_ms": 1},
        {"node": "cheap", "event": "tool:a", "elapsed_ms": 1},
        {"node": "cheap", "event": "tool:a", "elapsed_ms": 1},
        {"node": "cheap", "event": "tool:a", "elapsed_ms": 1},
        {"node": "slow", "event": "tool:b", "elapsed_ms": 900},
    ])]
    rows = M.node_latency(records)
    assert [r["node"] for r in rows] == ["slow", "cheap"]


def test_tool_failures_are_per_tool() -> None:
    """按工具分开算失败率 —— 混在一起会被高频工具稀释到看不见。"""
    records = [_task(tool_calls=[
        {"tool": "statute_search", "ok": True},
        {"tool": "statute_search", "ok": True},
        {"tool": "material_parse", "ok": False, "error": "boom"},
    ])]
    rows = {r["tool"]: r for r in M.tool_failures(records)}
    assert rows["material_parse"]["fail_rate"] == 1.0
    assert rows["statute_search"]["fail_rate"] == 0.0
    assert rows["material_parse"]["calls"] == 1


# --------------------------------------------------------------------------- 检索
def test_citation_identity_normalizes_chinese_and_arabic() -> None:
    """「第八十二条」与「第82条」必须是同一个键。

    不归一的话，一个**正确的**引用会被评测判成错的，
    而评测结论会直接影响"要不要重新调检索"的决定。
    """
    assert M.citation_identity("《劳动合同法》", "第八十二条") == \
        M.citation_identity("劳动合同法", "第82条")


def test_retrieval_metrics_reports_first_hit_rank_and_mrr() -> None:
    expected = {M.citation_identity("《劳动合同法》", "第八十七条")}
    hits = [
        M.citation_identity("《劳动法》", "第五十条"),
        M.citation_identity("《劳动合同法》", "第87条"),      # 第 2 位命中
        M.citation_identity("《劳动合同法》", "第八十七条"),
    ]
    m = M.retrieval_metrics(expected, hits, k=10)
    assert m["first_hit_rank"] == 2
    assert m["mrr"] == 0.5
    assert m["hit_count"] == 2, "精确率按**位次**算（重复命中会浪费名额）"
    assert m["recall"] == 1.0, "召回率按**去重**算（一条法条找到就行）"


def test_retrieval_metrics_missing_expected_returns_zero_mrr() -> None:
    m = M.retrieval_metrics({"a#1"}, ["b#2", "c#3"], k=10)
    assert m["first_hit_rank"] == 0
    assert m["mrr"] == 0.0
    assert m["recall"] == 0.0


# --------------------------------------------------------------------------- 样本
def test_production_task_id_filter() -> None:
    """真实任务编号形如 ``task_`` + 12 位十六进制；测试夹具不符合这个形状。

    用形状判而不是维护黑名单：黑名单每加一个测试就得同步改一次，
    而漏掉的那个会让失败率虚高。
    """
    assert runner.is_production_task("task_0454722c7ef3")
    for bogus in ("task_q_survive_a6416c0f", "task_auto_create", "task_x",
                  "task_progress_advance_deadbeef", "", "search_2"):
        assert not runner.is_production_task(bogus), bogus


def test_render_survives_empty_sample() -> None:
    """空样本也要能出报告，且不能打印 0%（那是"无从判断"，不是 0）。"""
    text = runner.render(runner.summarize([]))
    assert "样本不足" in text
    assert "0.0%" not in text, "分母 0 时不该出现 0%"
    assert "评测报告" in text


def test_render_reports_excluded_sample() -> None:
    """被排除的样本数必须写在报告里 —— 静默排除会让数字对不上明细。"""
    text = runner.render(runner.summarize([], excluded_nonstandard=62))
    assert "62" in text
    assert "非标准编号" in text


def test_render_warns_when_degraded_ratio_is_high() -> None:
    """降级率接近 100% 时要点出来 —— 表面"零失败"，实际是子系统长期不在。"""
    records = [_task(status="degraded") for _ in range(10)]
    text = runner.render(runner.summarize(records))
    assert "降级率接近 100%" in text
    text_low = runner.render(runner.summarize(
        [_task(status="degraded")] + [_task(status="succeeded") for _ in range(9)]))
    assert "降级率接近 100%" not in text_low
