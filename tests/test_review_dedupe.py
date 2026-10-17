"""回环重扫的风险点去重：同一条款只留本轮结论。

**这条用例钉的是一次真实故障**（``task_9bc893f93fe5``）：引用终审不过 ⇒ 图回环
重跑 ⇒ ``ReviewNode`` 在重跑尾巴上（``graph.LOOP_RERUN_TAIL``）⇒ 同一份劳动合同
被逐条审了两遍，两遍的清单在 ``harness.merge`` 里**追加**，前端风险卡 22 条
（= 11 + 11），条条读起来都是同一件事换了个说法。

合并键（条款号 + 风险描述前 60 字）挡不住这种重述 —— 两遍的措辞几乎不可能逐字
相同。所以判据换成**条款号**这个确定性字段：本轮报到的条款以本轮为准。

⚠ 两条边界必须保住，它们决定了这个改动是"去重"还是"删证据"：
1. 本轮**没报到**的条款一律保留上一轮（重扫没提不等于风险消失）；
2. 本轮**一条都没报**（重扫失败/降级）时不许动旧清单 —— 否则一次失败的扫描
   会把上一轮的有效结论整份抹掉。
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.agents import graph
from backend.agents.harness import Harness
from backend.agents.nodes.review_node import _supersede_rescanned, review_node

# 条款号 -> 两遍各自的措辞。**同一件事两种说法**，正是合并键挡不住的那类重述：
# 措辞不同、长度不同，前 60 字自然也不同。
PASS1_WORDING = {
    "第二条": "培训期内未签订书面劳动合同，用工关系处于无合同状态，发生争议时用人单位难以举证",
    "第四条": "甲方可根据业务需要单方调整工作岗位与工作地点，属于排除劳动者协商权的条款",
    "第八条": "培训七天期间不支付任何劳动报酬，违反最低工资与同工同酬的基本要求",
    "第九条": "工资标准未经双方协商一致即由甲方确定，缺少劳动者知情同意的证据链",
    "第十五条": "要求劳动者提供家庭成员和主要社会关系，超出用工管理必要范围且涉及个人信息",
    "第十六条": "以门店关闭为由即可立即解除劳动合同，解除了法定解除条件与预告程序",
    "第十八条": "以旷工为由拒绝发放当月工资，属于克扣工资，违反工资支付暂行规定",
    "第二十条": "违反规章制度或工作失误即不予录用，未区分过错程度，惩罚性条款过重",
    "第二十三条": "未约定竞业限制补偿，离职后仍受竞业约束，权利义务明显不对等",
    "第二十五条": "争议由甲方所在地仲裁委管辖，排除了劳动者法定管辖选择权",
    "第二十六条": "合同解释权归属甲方，属于单方解释条款，对劳动者不利",
}

PASS2_WORDING = {
    "第二条": "本条款未约定书面劳动合同的签订时限，存在未签书面合同的双倍工资风险",
    "第四条": "工作岗位与工作地点可由甲方单方决定，劳动者缺乏协商与救济途径",
    "第八条": "培训期内无薪，低于当地最低工资标准，该约定无效",
    "第九条": "工资数额由甲方单方决定，未见协商一致的意思表示",
    "第十五条": "要求提供家庭成员及主要社会关系等信息，涉嫌超出必要范围收集个人信息",
    "第十六条": "解除条件过于宽泛，未能体现法定解除的程序要件",
    "第十八条": "以旷工为由拒发工资，构成无故克扣，劳动者可主张补发",
    "第二十条": "以违纪或失误为由不予录用，未设比例原则，处罚畸重",
    "第二十四条": "试用期长度与合同期限不匹配，超出法定上限",
    "第二十五条": "仲裁管辖地被限定在甲方所在地，限制劳动者选择权",
}

# 第二遍没报到、只在上一轮出现过的条款：本轮没提，必须原样保留。
# 第二十四条是反过来的情形（第二遍新报的），用来确认"以本轮为准"是真的按轮次换，
# 而不是把两轮的清单做并集。
CARRY_OVER_CLAUSES = ("第二十三条", "第二十六条")
NEW_IN_PASS2 = "第二十四条"


def _rp(clause_no: str, risk: str, level: str = "high") -> dict[str, Any]:
    return {"clause_no": clause_no, "clause_title": f"{clause_no}标题",
            "clause_text": f"{clause_no}原文", "risk": risk, "level": level,
            "suggestion": "建议修改", "basis": [], "clause_index": 0}


def _pack(wording: dict[str, str], level: str = "high") -> list[dict[str, Any]]:
    return [_rp(no, text, level) for no, text in wording.items()]


def _state() -> dict[str, Any]:
    """一份最小 State：有正文、有拆条，够 ReviewNode 跑完一轮。"""
    clauses = [{"clause_no": no, "clause_title": f"{no}标题",
                "clause_text": f"{no}原文", "clause_index": i}
               for i, no in enumerate(PASS1_WORDING, start=1)]
    return {
        "task_id": "task_dedupe_test",
        "query": "帮我审一下这份劳动合同",
        "intent": "contract_review",
        "doc_type": "contract",
        "material_text": "劳动合同书\n" + "\n".join(
            f"{c['clause_no']}{c['clause_text']}" for c in clauses),
        "clauses": clauses,
        "statutes": [], "cases": [], "knowledges": [],
        "route": ["ReviewNode"],
    }


def _stub_llm(monkeypatch: pytest.MonkeyPatch, passes: list[dict[str, str]]) -> dict[str, int]:
    """把 ``chat_json`` 换成按**轮次**返回不同措辞的桩；返回可写的轮次游标。

    ⚠ 游标由用例显式切换，不按 ``chat_json`` 的调用次数自增：条款多时
    ``_scan_chunked`` 会把条款分批并发跑，**每批各调一次** ``chat_json``
    （外加一次缺失条款的全局判断），按调用次数换措辞会让同一轮里
    第一批拿到甲稿、第二批拿到乙稿 —— 那是"一轮里自相矛盾"，
    现实里不会发生，测出来的却是假红。
    """
    from backend.llm import client as llm_client_mod

    cursor = {"i": 0}

    class _Res:
        degraded = False
        provider = "fake"

    async def _chat_json(messages, **kwargs):
        return {"contract_type": "劳动合同", "risk_points": _pack(passes[cursor["i"]]),
                "missing_clauses": [], "confidence": 0.8}, _Res()

    monkeypatch.setattr(llm_client_mod.llm_client, "chat_json", _chat_json)
    return cursor


# --------------------------------------------------------------------------- 纯函数
def test_supersede_drops_previous_pass_for_reported_clauses() -> None:
    previous = _pack(PASS1_WORDING)
    fresh = _pack(PASS2_WORDING)
    kept, touched = _supersede_rescanned(previous, fresh)

    assert len(touched) == len(PASS2_WORDING)
    # 恰好剩下本轮没报到的那两条
    assert [rp["clause_no"] for rp in kept] == list(CARRY_OVER_CLAUSES)
    # 撤下的都是本轮报到过的条款
    assert all((rp["clause_no"] or "").strip() in touched for rp in previous
               if rp["clause_no"] not in CARRY_OVER_CLAUSES)


def test_supersede_keeps_everything_when_nothing_reported() -> None:
    """本轮一条都没报（重扫失败/降级）：旧清单一条不许少。"""
    previous = _pack(PASS1_WORDING)
    kept, touched = _supersede_rescanned(previous, [])
    assert kept == previous
    assert touched == set()


def test_supersede_treats_blank_clause_no_as_one_bucket() -> None:
    """模型没把风险挂到条款上时（clause_no 空）算同一个桶，不许两条并存。"""
    previous = [_rp("", "整份合同未约定送达地址条款"), _rp("第三条", "试用期约定过长")]
    fresh = [_rp("", "全文缺少争议解决方式的约定")]
    kept, _ = _supersede_rescanned(previous, fresh)
    assert [rp["clause_no"] for rp in kept] == ["第三条"]


# --------------------------------------------------------------------------- 节点
async def test_rescan_does_not_duplicate_risk_points(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一个 State 上跑两遍 ReviewNode（= 回环重扫），清单不许翻倍。

    走 ``graph._execute`` 而不是直接调节点：节点判"这是不是回环重扫"看的是
    ``harness.node_runs``，而这个计数**正是 ``_execute`` 里的
    :func:`_enter_loop_if_rerun` 维护的** —— 绕过它，用例就测不到回环，
    第一遍和第二遍看起来一模一样，用例会假绿。
    """
    cursor = _stub_llm(monkeypatch, [PASS1_WORDING, PASS2_WORDING])
    state = _state()
    harness = Harness.start(state, client="test", username="pytest")
    try:
        await graph._execute("ReviewNode", review_node, state, harness)
        first = [rp["clause_no"] for rp in state["risk_points"]]
        assert sorted(first) == sorted(PASS1_WORDING), f"首轮清单就不对：{first}"

        cursor["i"] = 1                      # 回环重扫：同一批条款，换了措辞
        await graph._execute("ReviewNode", review_node, state, harness)
    finally:
        harness.finish()

    clauses = [rp["clause_no"] for rp in state["risk_points"]]
    assert len(clauses) == len(set(clauses)), f"同一条款出现两次：{clauses}"
    assert len(state["risk_points"]) == len(PASS2_WORDING) + len(CARRY_OVER_CLAUSES), \
        "第二遍报到的条款应以第二遍为准，第二遍没报到的条款应保留"

    by_clause = {rp["clause_no"]: rp["risk"] for rp in state["risk_points"]}
    # 报到过的条款：是**第二遍**的措辞（第一遍的被撤下）
    assert by_clause["第八条"] == PASS2_WORDING["第八条"]
    assert PASS1_WORDING["第八条"] not in [rp["risk"] for rp in state["risk_points"]]
    # 第二遍没报到的条款：第一遍的结论保留
    for no in CARRY_OVER_CLAUSES:
        assert by_clause[no] == PASS1_WORDING[no]
    # 第二遍新报的条款：进得来
    assert by_clause[NEW_IN_PASS2] == PASS2_WORDING[NEW_IN_PASS2]

    # 对照稿是按最终清单展开的三列表（原文/修改后/依据，**不含风险描述**），
    # 所以这里只能按条款行数核：一条风险一行，两遍的条目不许一起铺进表里。
    draft = state["revision_draft"]
    rows = [ln for ln in draft.splitlines() if ln.startswith("| ") and "---" not in ln]
    assert len(rows) == len(state["risk_points"]) + 1, f"对照稿行数对不上：\n{draft}"
    assert sum(1 for ln in rows if CARRY_OVER_CLAUSES[0] in ln) == 1


async def test_failed_rescan_keeps_the_previous_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """重扫一条都没报出来（超时/降级）时，上一轮的清单**一条都不许少**。

    这是"去重"最容易做过头的地方：判据写成"本轮为准"，一轮空结果就会把
    已经审出来的 11 条风险整份抹掉 —— 屏幕上是"零风险"，法律上是最危险的假阴性。
    """
    cursor = _stub_llm(monkeypatch, [PASS1_WORDING, {}])
    state = _state()
    harness = Harness.start(state, client="test", username="pytest")
    try:
        await graph._execute("ReviewNode", review_node, state, harness)
        cursor["i"] = 1
        await graph._execute("ReviewNode", review_node, state, harness)
    finally:
        harness.finish()

    assert sorted(rp["clause_no"] for rp in state["risk_points"]) == sorted(PASS1_WORDING)


async def test_second_pass_alone_does_not_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    """**变异对照**：把回环判据改回"总是追加"，上面那条用例必须变红。

    这个用例断言的是"没有回环时旧行为不变"：单轮扫描的条数就是清单条数。
    """
    _stub_llm(monkeypatch, [PASS1_WORDING])
    state = _state()
    harness = Harness.start(state, client="test", username="pytest")
    try:
        await graph._execute("ReviewNode", review_node, state, harness)
    finally:
        harness.finish()
    assert len(state["risk_points"]) == len(PASS1_WORDING)
