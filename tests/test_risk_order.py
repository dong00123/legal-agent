"""风险点的顺序：按**合同顺序**排（第一条 → 最后一条），不是按严重程度。

2026-09-29 用户看着结果页说「风险按照顺序来」：原来 ``_filter_by_evidence`` 用
``LEVEL_ORDER`` 打头，屏幕上就是「第二十三条、第二十五条、第八条、第九条…」——
律师是拿着合同从头往下核的，这个顺序每看一条都要回原文里翻位置。

四件事必须钉住：

1. 顺序键是**条款在原文条款序列里的下标**，不是条目上那个 ``clause_index`` 字段。
   那个字段模型可以自己给（``setdefault`` 只兜底），模型写错一个数字、卡片就乱排，
   而且**看不出来** —— 屏幕上只是"顺序有点怪"。
2. 同一条款内仍按严重程度（高在前）：等级不是被删掉了，只是退居其次。
3. 没有条款号的（全文性风险）排在最后 —— 它们本来就不属于"某一条"。
4. 回环重扫之后的清单仍是合同顺序：合并在 ``harness.merge`` 里是"保留的上一轮 + 本轮"
   拼起来，天然乱序，靠 ``review_node`` 末尾那一次 ``sort_risks`` 收口。
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.agents import graph
from backend.agents.harness import Harness
from backend.agents.nodes.review_node import review_node, sort_risks

CLAUSES = ["第一条", "第二条", "第三条", "第四条", "第五条"]


def _rp(clause_no: str, risk: str, level: str = "medium", **kw: Any) -> dict[str, Any]:
    d = {"clause_no": clause_no, "clause_title": "", "clause_text": "", "risk": risk,
         "level": level, "suggestion": "", "basis": [], "clause_index": -1}
    d.update(kw)
    return d


def _state() -> dict[str, Any]:
    clauses = [{"clause_no": no, "clause_title": f"{no}标题", "clause_text": f"{no}原文",
                "clause_index": i} for i, no in enumerate(CLAUSES)]
    return {
        "task_id": "task_order_test",
        "query": "帮我审一下这份劳动合同",
        "intent": "contract_review",
        "doc_type": "contract",
        "material_text": "劳动合同书\n" + "\n".join(f"{c['clause_no']}{c['clause_text']}" for c in clauses),
        "clauses": clauses,
        "statutes": [], "cases": [], "knowledges": [],
        "route": ["ReviewNode"],
    }


# --------------------------------------------------------------------------- 排序
def test_sorts_by_clause_order_not_by_level() -> None:
    state = _state()
    shuffled = [
        _rp("第四条", "高危条款", "high"),
        _rp("第一条", "低危条款", "low"),
        _rp("第三条", "中危条款", "medium"),
        _rp("第二条", "高危条款二", "high"),
    ]
    got = [rp["clause_no"] for rp in sort_risks(shuffled, state)]
    assert got == ["第一条", "第二条", "第三条", "第四条"], "必须按合同顺序，不是按等级"


def test_same_clause_keeps_severity_first() -> None:
    """同一条款报了两处：高的在前 —— 等级只是退居其次，没有被删掉。"""
    state = _state()
    items = [_rp("第二条", "低", "low"), _rp("第二条", "高", "high"),
             _rp("第二条", "中", "medium")]
    got = [rp["level"] for rp in sort_risks(items, state)]
    assert got == ["high", "medium", "low"]


def test_model_supplied_clause_index_is_not_trusted() -> None:
    """模型自己写的 ``clause_index`` 不能拿来排序（它可以说第二条排在第 99 位）。"""
    state = _state()
    items = [_rp("第三条", "三"), _rp("第二条", "二", clause_index=99)]
    got = [rp["clause_no"] for rp in sort_risks(items, state)]
    assert got == ["第二条", "第三条"], "排序用的是条款号算出来的下标"


def test_risks_without_a_clause_go_last() -> None:
    state = _state()
    items = [_rp("", "整份合同缺少送达地址条款", clause_index=0),
             _rp("第五条", "五"), _rp("第一条", "一")]
    got = [(rp["clause_no"] or "（全文）") for rp in sort_risks(items, state)]
    assert got == ["第一条", "第五条", "（全文）"]


# --------------------------------------------------------------------------- 回环
def _stub_llm(monkeypatch: pytest.MonkeyPatch, passes: list[list[dict[str, Any]]]) -> dict[str, int]:
    """按**轮次**返回不同清单的桩（游标由用例切换，理由见 test_review_dedupe.py）。"""
    from backend.llm import client as llm_client_mod

    cursor = {"i": 0}

    class _Res:
        degraded = False
        provider = "fake"

    async def _chat_json(messages, **kwargs):
        return {"contract_type": "劳动合同", "risk_points": passes[cursor["i"]],
                "missing_clauses": [], "confidence": 0.8}, _Res()

    monkeypatch.setattr(llm_client_mod.llm_client, "chat_json", _chat_json)
    return cursor


async def test_loop_rescan_result_is_still_in_contract_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """首轮报 [第四条, 第二条]、重扫报 [第三条, 第二条] ⇒ 最终是 二、三、四。

    这一趟同时走了两条路：重扫把首轮的「第二条」顶掉了（去重），
    剩下的首轮条目「第四条」与本轮的两条拼在一起（乱序）——
    末尾不排序的话，屏幕上就是"第四条"孤零零地跑在最前面。
    """
    cursor = _stub_llm(monkeypatch, [
        [_rp("第四条", "四号风险", "high"), _rp("第二条", "二号风险", "medium")],
        [_rp("第三条", "三号风险", "high"), _rp("第二条", "二号风险的另一种说法", "medium")],
    ])
    state = _state()
    harness = Harness.start(state, client="test", username="pytest")
    try:
        await graph._execute("ReviewNode", review_node, state, harness)
        cursor["i"] = 1
        await graph._execute("ReviewNode", review_node, state, harness)
    finally:
        harness.finish()

    assert [rp["clause_no"] for rp in state["risk_points"]] == ["第二条", "第三条", "第四条"]
    assert [rp["risk"] for rp in state["risk_points"]][0] == "二号风险的另一种说法", \
        "第二条留下的应该是重扫那一版（去重的规矩见 test_review_dedupe.py）"
