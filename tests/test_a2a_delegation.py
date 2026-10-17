"""A2A 自主委派层（``protocol/delegation.py``）回归测试。

与 ``test_a2a.py`` 的分工：那边钉的是**裸通道**（``A2AClient``）的契约，
这里钉的是**委派层**在一层包装之上必须自己兜住的东西 ——
每一条都对应一个真实存在、且不会自己暴露的故障模式：

1. **委派会改写 ``state["route"]``。** 节点第一件事是 ``harness.step()``，
   它会 ``mark_route``；被调方不在 route 里时会被 **append** 进去，
   于是 ``graph.next_node`` 把它当成还没跑的节点，在 ReportNode 之后再调度一遍 ——
   报告已经生成，新证据根本进不去，还白烧一次检索配额。
   这是本层最该挡住的一条，所以用例用桩节点**真实复现** ``harness.step()``
   （而不是伪造一个不写 state 的假节点），否则测了个寂寞。

2. **两条通道的写回方式不同。** 进程内共用 State，HTTP 只回 JSON ——
   不显式并回去，HTTP 模式下委派就是"静默地什么都没拿到"。

3. **失败必须显式、且不能登记假缺口。** 判据写错（没给检索式）是调用方的
   bug，不是证据链的缺陷，混进证据缺口清单会让律师整份清单都不信。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from backend.agents.harness import Harness
from backend.agents.harness.planner import TaskPlan
from backend.agents.nodes import NODE_FUNCS
from backend.agents.protocol.delegation import (
    FOCUS_KEY,
    Delegation,
    delegate,
)
from backend.app.config import settings
from backend.agents.protocol.message import A2AAction, A2AArtifact, A2ATransport
from backend.agents.state import new_state


# --------------------------------------------------------------------------- 夹具
STUB_CASES = [{"case_no": "(2023)京01民终1号", "title": "测试类案", "court": "北京一中院"}]


async def _stub_case_node(state: dict[str, Any], harness: Harness) -> dict[str, Any]:
    """桩 CaseNode：**刻意照抄真实节点的第一步** ``harness.step()``。

    真实节点每个都是 ``harness.step("XxxNode")`` 开头，而 ``step`` 会
    ``mark_route`` —— 这正是"委派污染 route"的成因。桩节点不这么做的话，
    下面那条 route 用例永远测不出问题。

    必须是 ``async``：``A2AClient._in_process`` 是 ``await fn(...)``。
    """
    harness.step("CaseNode")
    state["cases"] = list(STUB_CASES)
    return state


@pytest.fixture()
def harness_with_trimmed_case(monkeypatch: pytest.MonkeyPatch):
    """一个"CaseNode 被步数预算裁掉、但 AnalysisNode 仍在跑"的运行时。"""
    monkeypatch.setitem(NODE_FUNCS, "CaseNode", _stub_case_node)
    state = new_state("公司拖欠工资并把我辞退了，还不给经济补偿", doc_type="contract")
    # route 是**裁剪后**的序列（intent_node 写的就是 plan.nodes()），
    # 所以被裁掉的 CaseNode 不在里面 —— 这正是真实情形。
    state["route"] = ["IntentNode", "StatuteNode", "AnalysisNode", "ReportNode"]
    # 意图必须显式写：真实的 ``intent_node`` 第一步就是 ``state["intent"] = result.intent``，
    # 而委派层要靠它取静态路由来划"起手范围"。缺了它，起手范围会静默退化成
    # 只认 state["route"]，测出来的边界就不是线上那条。
    state["intent"] = "consult"  # type: ignore[typeddict-item]
    harness = Harness.start(state, client="test", username="pytest")
    plan = TaskPlan(intent="consult", label="劳动咨询", simple=False,
                    trimmed=["CaseNode"])
    harness.last_plan = plan
    try:
        yield harness, state
    finally:
        harness.finish()


def _call(harness: Harness, **kw: Any) -> A2AArtifact | None:
    kw.setdefault("from_node", "AnalysisNode")
    return asyncio.run(delegate(harness, Delegation(**kw)))


# ------------------------------------------------------------------- 头号风险：route
def test_delegation_does_not_mutate_route(harness_with_trimmed_case) -> None:
    """委派之后 ``state["route"]`` 必须**逐字不变**。

    被调方（CaseNode）执行时 ``harness.step()`` 会把自己 append 进 route；
    本层若不恢复，``graph.next_node`` 会把它当成待执行节点再调度一次。
    """
    harness, state = harness_with_trimmed_case
    before = list(state["route"])

    art = _call(harness, target="CaseNode", focus="拖欠工资 经济补偿",
                reason="分析需要类案支撑，池中为空")

    assert art is not None and art.ok, (art.error if art else "被守卫拦下")
    assert list(state["route"]) == before, (
        "委派改写了 state['route']：CaseNode 被 mark_route 追加进去了，"
        "graph.next_node 会把它当成没跑的节点再调度一遍")


def test_delegation_actually_ran_the_target(harness_with_trimmed_case) -> None:
    """反面保证：route 没变，不等于"其实什么都没发生"。

    只断言 route 不变的话，一个"直接 return None"的空实现也能过 ——
    所以必须同时钉住产出确实落地了。
    """
    harness, state = harness_with_trimmed_case
    assert not state.get("cases"), "前置条件：池子本应为空"

    art = _call(harness, target="CaseNode", focus="拖欠工资 经济补偿", reason="缺类案")

    assert art is not None and art.ok
    assert state["cases"], "桩节点写进 State 的类案没有落地"


def test_target_reached_via_agent_name(harness_with_trimmed_case) -> None:
    """``target`` 写成 Agent 名（"case"）与节点名（"CaseNode"）都要认。"""
    harness, state = harness_with_trimmed_case
    art = _call(harness, target="case", focus="拖欠工资", reason="缺类案")
    assert art is not None and art.ok, (art.error if art else "被守卫拦下")
    assert list(state["route"]) == ["IntentNode", "StatuteNode", "AnalysisNode", "ReportNode"]


# ------------------------------------------------------------------------- focus
def test_empty_focus_is_rejected_without_registering_gap(harness_with_trimmed_case) -> None:
    """没有检索式的委派直接拒绝，且**不登记证据缺口**。

    这是调用方判据写错，不是证据链的缺陷。混进缺口清单会拉低置信度，
    而置信度又卡在缓存准入门槛上 —— 一个笔误能污染整条链路。
    """
    harness, state = harness_with_trimmed_case
    gaps_before = list(state.get("evidence_gap") or [])

    art = _call(harness, target="CaseNode", focus="   ", reason="缺类案")

    assert art is None
    assert list(state.get("evidence_gap") or []) == gaps_before
    assert not state.get("cases"), "被拒绝的委派不应产生任何产出"


def test_empty_focus_is_allowed_for_a_target_that_never_reads_it(monkeypatch) -> None:
    """**不读 focus 的被调方**允许留空（Draft / Review / Validator / Report …）。

    原先一律要求填 focus，等于把"检索类节点必须有专项检索式"这条误推广到了全体。
    对不读它的节点，那个要求只会逼调用方现编一句没有去处的话 ——
    而那句话照样进 Trace 的 detail 与 HTTP payload，看着像"说清楚了要什么"，
    实际被调方一个字没用上。

    这里连兜底一起钉住：留空时用 ``state["query"]`` 顶上（律师原话），
    不让 payload 与 Trace 里是一段空白 —— Trace 的去重键含 detail，
    空白会让同一对 Agent 的两次不同委派看起来一模一样。
    """
    seen: dict[str, Any] = {}

    async def _stub_report(state: dict[str, Any], harness: Harness) -> dict[str, Any]:
        harness.step("ReportNode")
        seen["focus"] = state.get(FOCUS_KEY)
        return state

    monkeypatch.setitem(NODE_FUNCS, "ReportNode", _stub_report)
    state = new_state("公司拖欠工资并违法解除，我能主张什么")
    state["route"] = ["IntentNode", "ReportNode"]
    harness = Harness.start(state, client="test", username="pytest")
    try:
        art = _call(harness, target="ReportNode", focus="", reason="让收尾按最新证据汇总")
    finally:
        harness.finish()

    assert art is not None and art.ok, "不读 focus 的被调方被 focus 校验挡住了"
    assert seen.get("focus") == "公司拖欠工资并违法解除，我能主张什么", (
        "留空时没有用 state['query'] 兜底 —— 被调方拿到的是空串，"
        "Trace 与 payload 里也是空白"
    )


def test_step_budget_must_fit_the_queued_nodes(harness_with_trimmed_case) -> None:
    """步数要装得下"被调方那一步 **+ 后面还排着队的节点**"，装不下就现在停手。

    只查 ``harness.exhausted()`` 是不够的：它只说"已经用光了"，答不了
    "被调方跑完之后，排队的节点还有没有步数"。差一步的后果不是少跑一个节点，
    而是流程**停在中途** —— 跑到 Validator 就没步数、Report 不生成，
    交付物比不委派更差。与 ``graph._should_loop`` 的预算闸同一算式、同一理由。

    这里 ``executed`` 刻意设成"AnalysisNode 正在跑"的真实样子：
    委派发生在节点**执行途中**，而 ``graph._execute`` 在调节点函数**之前**
    就把当前节点记进游标了 —— 所以排队数只算它后面的。
    """
    harness, state = harness_with_trimmed_case
    harness.executed = ["IntentNode", "StatuteNode", "AnalysisNode"]   # route 里只剩 ReportNode
    max_steps = int(settings.MAX_STEPS)

    # ---- 差一步：委派那一步跑完，排队的 ReportNode 就没步数了
    state["steps"] = max_steps - 1  # type: ignore[typeddict-item]
    assert _call(harness, target="CaseNode", focus="拖欠工资", reason="缺类案") is None, \
        "步数装不下后续排程却仍然发起了委派"
    assert not state.get("cases"), "被拦下的委派不该产生任何产出"
    notes = " ".join(str(e.get("detail") or "") for e in harness.tracer.to_list())
    assert "剩余步数不足" in notes, f"预算不足没有留下说明：{notes[:300]}"

    # ---- 反面：刚好装得下时必须放行（否则这道闸会误伤正常链路）
    state["steps"] = max_steps - 2  # type: ignore[typeddict-item]
    art = _call(harness, target="CaseNode", focus="拖欠工资", reason="缺类案")
    assert art is not None and art.ok, "步数够却仍被拒 —— 这道闸算得太紧，会误伤正常委派"


def test_focus_does_not_leak_into_state(harness_with_trimmed_case) -> None:
    """委派结束后 State 里不该残留 ``a2a_focus_query``。

    残留会让**下一个**节点误以为自己也是被委派来的，拿着别人的检索式去检索。
    """
    harness, state = harness_with_trimmed_case
    _call(harness, target="CaseNode", focus="拖欠工资 经济补偿", reason="缺类案")
    assert FOCUS_KEY not in state, "委派用的检索式残留在了 State 里"


def test_existing_focus_is_restored(harness_with_trimmed_case) -> None:
    """调用方原本就有 focus 时，委派结束后要原样还回去（而不是删掉）。"""
    harness, state = harness_with_trimmed_case
    state[FOCUS_KEY] = "调用方自己的检索式"

    _call(harness, target="CaseNode", focus="拖欠工资", reason="缺类案")

    assert state[FOCUS_KEY] == "调用方自己的检索式"


# ------------------------------------------------------------------------- 去重
def test_same_target_and_focus_is_sent_once(harness_with_trimmed_case) -> None:
    """同一个 (目标, 检索式) 只委派一次 —— 重复委派是纯浪费配额。"""
    harness, state = harness_with_trimmed_case
    first = _call(harness, target="CaseNode", focus="拖欠工资", reason="缺类案")
    state["cases"] = []          # 清空，确保第二次不是"因为已有产出才跳过"
    second = _call(harness, target="CaseNode", focus="拖欠工资", reason="缺类案")

    assert first is not None and second is not None
    assert second.message_id == first.message_id, "第二次应命中缓存、复用首次的回执"
    assert not state.get("cases"), "第二次委派不应真的再跑一遍节点"


# ------------------------------------------------------------------------- 两档边界
def test_out_of_scope_without_expansion_is_refused(harness_with_trimmed_case) -> None:
    """起手范围外的目标，不显式声明 ``expansion`` 就拒绝。

    ``statute_lookup`` 的静态路由只有法规检索 + 终审 + 收尾 —— 它**天生**不含类案，
    所以"给它拉个 CaseNode"是货真价实的越界，而不是被模型裁掉的那种。
    """
    harness, state = harness_with_trimmed_case
    harness.last_plan = TaskPlan(intent="statute_lookup", label="x", simple=False)  # 无 trimmed
    state["intent"] = "statute_lookup"  # type: ignore[typeddict-item]

    art = _call(harness, target="CaseNode", focus="拖欠工资", reason="想拉个没在计划里的")

    assert art is None
    assert not state.get("cases")


def test_expansion_is_allowed_but_counted_separately(harness_with_trimmed_case) -> None:
    """显式声明 ``expansion`` 后放行，但走单独的、更严的计数。"""
    harness, state = harness_with_trimmed_case
    harness.last_plan = TaskPlan(intent="statute_lookup", label="x", simple=False)
    state["intent"] = "statute_lookup"  # type: ignore[typeddict-item]

    art = _call(harness, target="CaseNode", focus="拖欠工资", reason="扩容补类案",
                expansion=True)

    assert art is not None and art.ok
    assert harness.a2a_budget.expansion == 1
    assert harness.a2a_budget.in_route == 0, "扩容委派不该占用起手内配额"


def test_expansion_has_a_harder_cap(harness_with_trimmed_case) -> None:
    """扩容委派超过上限后必须停手（它的代价是"跑偏"，比档一更该被质疑）。

    上限**读 settings 而不是模块常量**（2026-10-07 提上去的）：写死一个数的话，
    把配置调小再跑这条用例照样绿 —— 而"调了配置不生效"正是这次提上去要治的病。
    """
    harness, state = harness_with_trimmed_case
    harness.last_plan = TaskPlan(intent="statute_lookup", label="x", simple=False)
    state["intent"] = "statute_lookup"  # type: ignore[typeddict-item]
    cap = int(settings.DELEGATION_MAX_EXPANSION)

    for i in range(cap):
        art = _call(harness, target="CaseNode", focus=f"扩容检索式{i}", reason="扩容",
                    expansion=True)
        assert art is not None and art.ok
        state["cases"] = []      # 清空，避免被"已产出"干扰

    over = _call(harness, target="CaseNode", focus="再来一次", reason="扩容", expansion=True)
    assert over is None, f"超过 {cap} 次扩容委派应被拒绝"


# ------------------------------------------------------------------------- 深度
def test_delegation_depth_cap() -> None:
    """委派链深度上限 —— 防 A 委派 B、B 又委派回 A 这类环路。

    去重挡得住"同一件事重复委派"，挡不住"互相委派不同的事"。
    """
    budget = Harness.start(new_state("测试"), client="test", username="pytest")
    try:
        budget.a2a_budget.depth = int(settings.DELEGATION_MAX_DEPTH)
        state = budget.state
        state["route"] = ["IntentNode", "CaseNode"]
        art = _call(budget, target="CaseNode", focus="任意", reason="测深度")
        assert art is None, "深度已达上限时不该再发起委派"
    finally:
        budget.finish()


# ------------------------------------------------------------------------- HTTP 写回
def test_http_fields_are_merged_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 通道下，回执里的 ``fields`` 必须并回调用方 State。

    进程内共用 State，不写回也"看起来正常"；HTTP 只回 JSON ——
    不显式并回去，委派就是**静默地什么都没拿到**。
    """
    remote_cases = [{"case_no": "(2022)沪01民终9号", "title": "远程类案"}]

    class _FakeClient:
        async def call(self, agent, *, action=A2AAction.RUN, payload=None):
            return A2AArtifact(
                message_id="msg_remote", from_agent="case", ok=True,
                summary="远端返回类案 1 条",
                fields={"cases": remote_cases},
                gaps=[{"kind": "tool", "reason": "远端未覆盖 2019 年前类案", "impact": ""}],
                transport=A2ATransport.HTTP.value, elapsed_ms=12,
            )

    monkeypatch.setattr("backend.agents.protocol.delegation.client",
                        lambda harness=None: _FakeClient())

    state = new_state("测试远程委派")
    state["route"] = ["IntentNode", "CaseNode"]
    harness = Harness.start(state, client="test", username="pytest")
    try:
        art = asyncio.run(delegate(harness, Delegation(
            target="CaseNode", from_node="AnalysisNode",
            focus="劳动合同 违法解除", reason="本机池空，问远端")))
        assert art is not None and art.ok
        assert art.transport == A2ATransport.HTTP.value
        assert state["cases"] == remote_cases, "远端回执的 fields 没有并回本地 State"
        reasons = [g.get("reason") for g in (state.get("evidence_gap") or [])]
        assert "远端未覆盖 2019 年前类案" in reasons, "远端缺口没有登记进证据缺口"
    finally:
        harness.finish()


def test_remote_scalar_does_not_clobber_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """远端回执里的**整体性结论**不能冲掉本地已有的（写回是保守的）。

    只有"新增的证据条目"才该并进来；报告/草稿/置信度这类是"被调方的最终话"，
    拿它覆盖调用方自己的结论，等于让一次辅助调用篡改了主体产出。
    """

    class _FakeClient:
        async def call(self, agent, *, action=A2AAction.RUN, payload=None):
            return A2AArtifact(message_id="m", from_agent="report", ok=True,
                               fields={"report": "远端版报告", "confidence": 0.99},
                               transport=A2ATransport.HTTP.value)

    monkeypatch.setattr("backend.agents.protocol.delegation.client",
                        lambda harness=None: _FakeClient())

    state = new_state("测试")
    state["route"] = ["IntentNode", "ReportNode"]
    state["report"] = "本地版报告"
    harness = Harness.start(state, client="test", username="pytest")
    try:
        asyncio.run(delegate(harness, Delegation(
            target="ReportNode", from_node="AnalysisNode", focus="汇总", reason="测试")))
        assert state["report"] == "本地版报告", "远端回执覆盖了调用方自己的报告"
        assert "confidence" not in state or state["confidence"] != 0.99
    finally:
        harness.finish()


# ------------------------------------------------------------------------- 记账
def test_budget_is_per_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """``begin_task`` 要清空委派记账，否则同进程的下一个任务会被上一个的上限拦死。"""
    monkeypatch.setitem(NODE_FUNCS, "CaseNode", _stub_case_node)
    state = new_state("测试")
    state["route"] = ["IntentNode", "CaseNode"]
    harness = Harness.start(state, client="test", username="pytest")
    try:
        harness.a2a_budget.in_route = 99
        harness.begin_task()
        assert harness.a2a_budget.in_route == 0
    finally:
        harness.finish()


# =========================================================== 接线：Analysis → Case
# 上面钉的是委派层的契约，下面钉的是**调用方的判据**：AnalysisNode 在什么条件下
# 才该去把被预算裁掉的 Case/Knowledge Agent 叫回来，以及叫回来之后缺口语义不变。
def _collab_harness(monkeypatch: pytest.MonkeyPatch, *, route: list[str],
                    trimmed: list[str], intent: str = "consult",
                    query: str = "公司拖欠工资并违法解除"):
    """搭一个 AnalysisNode 视角的运行时。与 ``harness_with_trimmed_case`` 的差别：
    那边测委派层（只关心 route/写回），这边测**判据**，所以 state 要像真跑过
    StatuteNode 一样带着争议焦点。"""
    monkeypatch.setitem(NODE_FUNCS, "CaseNode", _stub_case_node)
    monkeypatch.setitem(NODE_FUNCS, "KnowledgeNode", _stub_knowledge_node)
    state = new_state(query, doc_type="contract")
    state["route"] = list(route)
    state["intent"] = intent  # type: ignore[typeddict-item]
    harness = Harness.start(state, client="test", username="pytest")
    harness.last_plan = TaskPlan(intent=intent, label="劳动咨询", simple=False,
                                 trimmed=list(trimmed))
    return harness, state


async def _stub_knowledge_node(state: dict[str, Any], harness: Harness) -> dict[str, Any]:
    harness.step("KnowledgeNode")
    state["knowledges"] = [{"title": "本所劳动争议办案指引", "chunk_id": "k1"}]
    return state


def _collab(harness: Harness, state: dict[str, Any]) -> None:
    from backend.agents.nodes.analysis_node import _collaborate

    asyncio.run(_collaborate(state, harness))


def test_collaborate_calls_back_trimmed_case_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """**本次改造的主戏**：类案池为空 + CaseNode 被预算裁掉 → Analysis 把它叫回来。

    改造前这条路只有一种结局：planner 砍掉类案检索 → 报告里登记
    「缺少同类案件的裁判倾向参考」，然后由律师自己认栽。现在判据落在证据本身 ——
    "我正要下的判断需要类案，手上一条都没有" —— 于是自己发起委派。
    """
    harness, state = _collab_harness(
        monkeypatch,
        route=["IntentNode", "StatuteNode", "AnalysisNode", "ReportNode"],
        trimmed=["CaseNode"])
    try:
        assert not state.get("cases"), "前置条件：类案池本应为空"

        _collab(harness, state)

        assert state.get("cases"), (
            "被裁掉的 CaseNode 没有被叫回来 —— 起手范围若只认 state['route']，"
            "就会把这个最该救的委派挡在门外（trimmed 也是起手范围的一部分）")
        # 两条都发：consult 的静态路由本就同时含 Case 与 Knowledge，而两个池子都空。
        # **不是**"顺手多捞一个" —— 类案与办案指引是分析下判断的两条独立支撑，
        # 缺哪条补哪条。上限由 MAX_IN_ROUTE 与各自配额兜住。
        assert state.get("knowledges")
        assert harness.a2a_budget.in_route == 2
        assert [t for t, _ in harness.a2a_budget.sent] == ["CaseNode", "KnowledgeNode"], \
            "委派顺序应与 _COLLABORATIONS 的优先级一致（类案优先）"
    finally:
        harness.finish()


def test_collaborate_skips_when_pool_already_filled(monkeypatch: pytest.MonkeyPatch) -> None:
    """池子非空就不委派 —— **不做无条件预取**。

    ``case_search`` 的配额（默认 2 次）是**全任务共享**的：本节点多用一次，
    后面排程的节点就少一次。已有证据时再检一遍纯属浪费。
    """
    harness, state = _collab_harness(
        monkeypatch,
        route=["IntakeNode", "StatuteNode", "CaseNode", "AnalysisNode"],
        trimmed=[])
    try:
        state["cases"] = [{"case_no": "(2023)京01民终1号"}]
        state["knowledges"] = [{"title": "本所劳动争议办案指引"}]

        _collab(harness, state)

        assert harness.a2a_budget.in_route == 0, "池子已有证据却仍然发起了委派"
        assert harness.a2a_budget.sent == {}
    finally:
        harness.finish()


def test_collaborate_calls_back_model_trimmed_node(monkeypatch: pytest.MonkeyPatch) -> None:
    """**模型**裁掉的节点同样要能叫回来（route 与 trimmed 里都没有它）。

    这条是跑真机才发现的：``case_analysis`` 的静态路由本来含 KnowledgeNode，
    但动态规划让模型复核时，它以"多跑一个检索节点就是多等几十秒"为由把它裁了 ——
    ``plan.trimmed`` 只记**步数预算**砍掉的节点，模型裁的不留痕。
    若起手范围写成 ``route ∪ trimmed``，这种"模型嫌弃过、但干活时发现真需要"的
    Agent 就永远拉不回来，而它恰恰是自主委派最该覆盖的一类。
    """
    harness, state = _collab_harness(
        monkeypatch,
        intent="case_analysis",
        route=["IntakeNode", "StatuteNode", "CaseNode", "AnalysisNode",
               "ValidatorNode", "ReportNode"],   # 静态路由里的 KnowledgeNode 不在
        trimmed=[])                              # 也不是被预算裁的
    try:
        assert not state.get("knowledges"), "前置条件：本所指引池本应为空"

        _collab(harness, state)

        assert state.get("knowledges"), (
            "KnowledgeNode 在本次意图的静态路由里，却因为不在 route/trimmed 中被跳过 —— "
            "起手范围漏了这个来源，判据就变成了「看模型这轮裁没裁」")
    finally:
        harness.finish()


def test_collaborate_skips_target_outside_start_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """本次意图压根不含 CaseNode（既不在 route 也不在 trimmed，也不在静态路由）→ 不去捞。

    越界不是不能做，但要**显式**声明 ``expansion=True``；判据代码里悄悄越界，
    等于把"允许扩容但单独计数"这条约束架空了。
    """
    harness, state = _collab_harness(
        monkeypatch,
        intent="statute_lookup",  # 静态路由天生不含类案
        route=["IntentNode", "StatuteNode", "AnalysisNode", "ReportNode"],
        trimmed=[])  # 注意：没被裁，是**本次意图不需要**
    try:
        _collab(harness, state)

        assert harness.a2a_budget.in_route == 0
        assert not state.get("cases")
        # 静默跳过：不该在每次正常运行里刷一条没有信息量的告警
        assert not [e for e in harness.tracer.to_list()
                    if e.get("event") == "a2a_delegation_fallback"]
    finally:
        harness.finish()


def test_collaborate_respects_tool_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    """配额已用尽 → 不委派，且留一条 ``collab_skip`` 说明原因。

    不发这条 Trace 的话，界面上的表现是"明明缺类案却没去检"，
    排障时只能靠猜 —— 而配额耗尽是**最可能的**那个原因。
    """
    from backend.app.config import settings

    harness, state = _collab_harness(
        monkeypatch,
        route=["IntentNode", "StatuteNode", "AnalysisNode"],
        trimmed=["CaseNode"])
    try:
        monkeypatch.setattr(
            Harness, "quota_used",
            lambda self, tool: settings.CASE_SEARCH_MAX_CALLS if tool == "case_search" else 0)

        _collab(harness, state)

        assert "CaseNode" not in [t for t, _ in harness.a2a_budget.sent], \
            "类案配额已尽仍然发起了检索"
        assert not state.get("cases")
        assert [e for e in harness.tracer.to_list() if e.get("event") == "collab_skip"], \
            "配额耗尽导致的跳过没有留下任何痕迹"
    finally:
        harness.finish()


def test_collaborate_focus_prefers_issues_over_raw_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """检索式优先用**争议焦点**，而不是当事人的口语原话。

    争点已经归纳成法律问题（"未签书面劳动合同的二倍工资差额如何认定"），
    拿它去检索类案，命中率远高于一句需要猜的口语描述。
    """
    harness, state = _collab_harness(
        monkeypatch,
        route=["IntentNode", "StatuteNode", "AnalysisNode"],
        trimmed=["CaseNode"],
        query="公司把我辞了还不给钱")
    try:
        state["issues"] = [{"title": "违法解除劳动合同的经济补偿"},
                           {"title": "未签书面劳动合同的二倍工资差额"}]

        _collab(harness, state)

        by_target = dict(harness.a2a_budget.sent.keys())
        focus = by_target["CaseNode"]
        assert "违法解除劳动合同的经济补偿" in focus
        assert "还不给钱" not in focus, "用了口语原话而不是争议焦点"
        assert by_target["KnowledgeNode"] == focus, "两条委派应共用同一个检索式"
    finally:
        harness.finish()


def test_delegation_shrinks_gap_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """**验收口径**：补上类案之后，``GAP_NO_CASES`` 这条缺口不该再被登记。

    这条钉的是"改造有没有实际收益"：委派成功却照旧登记缺口的话，
    律师看到的清单一点没变，整个改造就只是多烧了几次工具调用。
    """
    from backend.agents.nodes.analysis_node import _collaborate, _gap_checks
    from backend.agents.state import GAP_NO_CASES

    harness, state = _collab_harness(
        monkeypatch,
        route=["IntentNode", "StatuteNode", "AnalysisNode"],
        trimmed=["CaseNode"])
    try:
        state["statutes"] = [{"doc_name": "劳动合同法", "article_no": "第四十七条"}]
        state["facts"] = [{"text": "用人单位于 2024 年 3 月单方解除"}]

        asyncio.run(_collaborate(state, harness))
        _gap_checks(state, harness, {})

        reasons = [g.get("reason") or "" for g in (state.get("evidence_gap") or [])]
        assert not any(GAP_NO_CASES in r for r in reasons), (
            f"委派已把类案补进池子，却仍然登记了「{GAP_NO_CASES}」：{reasons}")
    finally:
        harness.finish()


def test_collaborate_skips_circuit_broken_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """工具已熔断 → 不委派。

    **池子空有两种成因，必须分开**：检索式不好（换一个再检是对的），
    与工具本身不可用（再检多少次都一样）。真机实测过后者：``knowledge_search``
    超时把断路器打开，委派照样发出去，回执 ``ok=True``、池子仍空，
    由 ``landed()`` 兜住 —— 结论是对的，但白等一轮，Trace 里还多一条
    "回退直调"，把"知识库不可用"这件事说成了"委派没成"。
    """
    from backend.agents.nodes.analysis_node import _collaborate

    harness, state = _collab_harness(
        monkeypatch,
        route=["IntentNode", "StatuteNode", "CaseNode", "KnowledgeNode", "AnalysisNode"],
        trimmed=[])
    try:
        monkeypatch.setattr(type(harness.invoker.handler), "should_skip",
                            lambda self, tool: tool == "knowledge_search")

        asyncio.run(_collaborate(state, harness))

        assert [t for t, _ in harness.a2a_budget.sent] == ["CaseNode"], \
            "知识库工具已熔断，不该再委派 KnowledgeNode"
        skips = [e for e in harness.tracer.to_list() if e.get("event") == "collab_skip"]
        assert skips and "熔断" in skips[0].get("detail", ""), \
            "熔断导致的跳过没有说明原因，排障时会被当成判据写错"
    finally:
        harness.finish()
