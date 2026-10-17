"""Agent 自主裁决（Controller）的回归测试。

背景：2026-10-07 之前，4 个产品入口里只有 AI 助手会"思考"，另外 3 个是
"钉死意图 → 静态路线 → 顺序跑完"，全项目只有 ``AnalysisNode`` 一处运行时自主委派。
这个文件钉的是**为那 4 个入口补上的自主能力**，以及它的四条硬约束。

这里每一条坏了都不一定报错，只会静默地少一层保障：

1. **控制器被当成第 12 个节点** —— ``graph.NODES`` / ``NODE_FUNCS`` / ``NODE_SPECS`` /
   ``CARDS`` 四条表互相断言，多一条会让 A2A 能力卡、``product_health()``、
   ``next_node`` 的调度一起漂。
2. **直调工具绕过了 ToolExecutor** —— 配额、审计、``tool_calls`` 记账一起失效，
   而那次检索拿到的法条照样会进交付物。
3. **补进来的证据不再过终审** —— 这是最危险的一条：模型写进报告的法条
   没有任何人核过效力状态，而界面显示一切正常。
4. **降级路径上多出模型流量** —— LLM 挂掉时行为必须与改造前逐字一致。
"""

from __future__ import annotations

import asyncio

import pytest

from backend.agents import graph
from backend.agents.harness import Harness
from backend.agents.harness.router import INTENT_ROUTES
from backend.agents.state import new_state
from backend.tools.base import ToolResult

STATUTE = {"doc_name": "中华人民共和国劳动合同法", "article_no": "第八十七条",
           "content": "用人单位违法解除劳动合同的，应当支付赔偿金。",
           "status": "现行有效"}


# --------------------------------------------------------------------------- 脚手架
def _ready(intent: str = "case_search", **kw) -> tuple[dict, Harness]:
    """造一个"这一轮刚好跑完"的现场：route 上每个节点都已执行。

    控制器只在"最后一个待跑节点之后"裁决，所以 ``executed`` 必须与 ``route`` 齐平 ——
    不齐的话 ``_controller_review`` 第一行就 return，用例会变成永远测不到东西的绿灯。
    """
    state = new_state("公司违法解除劳动合同，法院一般怎么判赔偿", **kw)
    state["intent"] = intent          # type: ignore[typeddict-item]
    state["route"] = list(INTENT_ROUTES[intent]["nodes"])  # type: ignore[typeddict-item]
    harness = Harness.start(state, client="test", username="pytest")
    harness.executed = ["IntentNode"] + list(state["route"])
    return state, harness


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch):
    """把一个假的 ``chat_json`` 装进 llm_client，并让调用方可改它的回复。"""
    from backend.llm.client import LLMResult, llm_client

    box = {"reply": {"action": "finish", "why": "证据够用"},
           "provider": "openai", "calls": 0, "raise": None}

    async def _fake(messages, *, task="general", payload=None, default=None):
        box["calls"] += 1
        if box["raise"] is not None:
            raise box["raise"]
        return dict(box["reply"]), LLMResult(text="{}", provider=box["provider"], model="fake")

    monkeypatch.setattr(llm_client, "chat_json", _fake)
    # 绕开"已知钉在规则引擎上"那道闸：默认状态下 _selected 是"未探测"，
    # 本来就放行；这里显式钉死成非 rule，避免用例受先前调用留下的状态影响。
    monkeypatch.setattr(llm_client, "status",
                        lambda: {"provider": box["provider"], "degraded": False})
    return box


@pytest.fixture
def fake_tool(monkeypatch: pytest.MonkeyPatch):
    """替换 ``Harness.tool``。记录 (工具名, agent, 参数)，返回预设结果。"""
    box = {"calls": [], "result": ToolResult(ok=True, tool="statute_search", elapsed_ms=0,
                                             data={"statutes": [dict(STATUTE)]})}

    async def _fake(self, name, agent, **kwargs):  # noqa: ANN001, ANN003
        box["calls"].append((name, agent, kwargs))
        return box["result"]

    monkeypatch.setattr(Harness, "tool", _fake)
    return box


def _review(state: dict, harness: Harness):
    return asyncio.run(graph._controller_review(state, harness))


def _candidates(state: dict, harness: Harness) -> list[str]:
    """直接问控制器"现在有哪些可选动作"（不经过模型）。"""
    from backend.agents.harness.controller import _budget
    from backend.app.config import settings

    return harness.controller._candidates(state, harness, _budget(settings))


# --------------------------------------------------------------------------- 口径守卫
def test_controller_is_not_a_node() -> None:
    """控制器**不许**出现在任何一条节点表里。有人把它登记成节点时，这条先红。

    四条表是互相断言的（``test_a2a.py`` 断言 ``set(CARDS) == set(NODE_FUNCS)``），
    一旦控制器进了其中之一，它会出现在 A2A 能力卡、管理后台的节点清单、
    ``product_health()`` 的对账里 —— 而"4 个 agent + 11 个节点"这个口径就破了。
    """
    from backend.agents.nodes import NODE_FUNCS
    from backend.agents.harness.router import NODE_SPECS
    from backend.agents.protocol.registry import CARDS

    for name, table in (("NODES", graph.NODES), ("NODE_FUNCS", NODE_FUNCS),
                        ("NODE_SPECS", NODE_SPECS), ("CARDS", CARDS)):
        # graph.NODES 额外含 2 个并行批调度节点（BATCH__StatuteNode,CaseNode,KnowledgeNode
        # 与 BATCH__ReviewNode,AnalysisNode），它们是编排壳不是业务节点；其余表仍 11 条。
        expected = 13 if table is graph.NODES else 11
        assert len(table) == expected, f"{name} 不再是 {expected} 条：{len(table)}"
        assert "Controller" not in table, f"控制器混进了 {name}"


# --------------------------------------------------------------------------- 降级
def test_no_model_no_intervention(fake_llm, fake_tool) -> None:
    """模型不可用（``provider == "rule"``）时**一次调用都不发**，直接不介入。

    这是"LLM 挂掉时行为与改造前逐字一致"的落点：CI 与降级演示都恒为 rule。
    ``chat_json`` 在规则引擎兜底时也回得出 JSON，但那是**拼装**不是"agent 在想"，
    不能算数 —— 否则降级路径上会凭空多出检索动作。
    """
    state, harness = _ready()
    fake_llm["provider"] = "rule"
    fake_llm["reply"] = {"action": "call_tool:statute_search", "why": "想补"}
    try:
        assert _review(state, harness) is None
    finally:
        harness.finish()
    assert fake_tool["calls"] == [], "规则引擎兜底时控制器仍发起了工具调用"
    assert state.get("statutes") in (None, []), "降级路径上证据池被改了"
    assert harness.controller.state.rounds == 0


def test_known_rule_provider_skips_before_calling(fake_llm, fake_tool, monkeypatch) -> None:
    """已经确定钉在规则引擎上时，**连 chat_json 都不该发**。

    与上一条的区别：上一条是"发了才发现是 rule"，这条是"知道是 rule 就根本不发"。
    少了这一道，每次裁决都要白等一轮 provider 重探（``LLM_REPROBE_INTERVAL``
    到点时是真 HTTP，最长 8 秒）。
    """
    from backend.llm.client import llm_client

    state, harness = _ready()
    monkeypatch.setattr(llm_client, "status",
                        lambda: {"provider": "rule", "degraded": True})
    try:
        assert _review(state, harness) is None
    finally:
        harness.finish()
    assert fake_llm["calls"] == 0, "已知是规则引擎，仍然发了模型调用"
    assert harness.controller.state.llm_calls == 0


def test_llm_exception_does_not_break_the_flow(fake_llm, fake_tool) -> None:
    """裁决抛异常绝不能带崩已经跑完的流程 —— 它是可选增强，不是交付链路。"""
    state, harness = _ready()
    fake_llm["raise"] = RuntimeError("网关 500")
    try:
        assert _review(state, harness) is None       # 不抛出去
    finally:
        harness.finish()
    assert harness.controller.state.rounds == 1      # 确实尝试过
    assert fake_tool["calls"] == []


# --------------------------------------------------------------------------- 候选集
def test_empty_pool_offers_only_that_tool(fake_llm, fake_tool) -> None:
    """候选集**事前**裁剪：只有"池子为空"的工具才交给模型选。

    事后靠 ``can_call`` 拦是另一回事 —— 那样模型会反复挑一个它拿不到的工具，
    每次都白花一次调用。这里钉的是"它压根看不到那个选项"。
    """
    state, harness = _ready()
    state["statutes"] = [dict(STATUTE)]      # 法条池非空 ⇒ 不该再提供 statute_search
    state["cases"] = []
    try:
        cands = _candidates(state, harness)
    finally:
        harness.finish()
    assert "call_tool:statute_search" not in cands, "法条池非空却仍提供补检选项"
    assert "call_tool:case_search" in cands, "类案池为空却没提供补检选项"
    assert "finish" in cands


def test_nothing_to_do_means_no_model_call(fake_llm, fake_tool) -> None:
    """三个池子都有货 ⇒ 候选集为空 ⇒ **根本不问模型**（省掉一次 chat_json）。

    这是省调用最有效的一刀，而且比"问了再让它选 finish"更确定：没得选的时候，
    模型也可能硬选一个。
    """
    state, harness = _ready()
    state["statutes"] = [dict(STATUTE)]
    state["cases"] = [{"case_name": "某案"}]
    state["knowledges"] = [{"title": "某指引"}]
    try:
        assert _review(state, harness) is None
    finally:
        harness.finish()
    assert fake_llm["calls"] == 0, "没有可选动作时仍然问了模型"


def test_direct_intent_is_left_alone(fake_llm, fake_tool) -> None:
    """与法律无关的直答（off_topic）不补证据 —— 那条路线本来就不检索、不终审。"""
    state, harness = _ready(intent="off_topic")
    try:
        assert _review(state, harness) is None
    finally:
        harness.finish()
    assert fake_llm["calls"] == 0


def test_action_outside_candidates_is_ignored(fake_llm, fake_tool) -> None:
    """模型给出候选集外的动作（含越权工具）时忽略 —— 白名单是硬的，不是建议。"""
    state, harness = _ready()
    fake_llm["reply"] = {"action": "call_tool:draft_generate", "why": "偷偷起草"}
    try:
        assert _review(state, harness) is None
    finally:
        harness.finish()
    assert fake_tool["calls"] == [], "候选集外的工具被真的调用了"


# --------------------------------------------------------------------------- 约束 1：不得旁路
def test_tool_goes_through_harness_tool(fake_llm, fake_tool) -> None:
    """直调工具必须走 ``Harness.tool`` —— 配额/审计/Trace 一套语义，不得旁路。"""
    state, harness = _ready()
    fake_llm["reply"] = {"action": "call_tool:statute_search", "why": "法条池为空"}
    try:
        _review(state, harness)
    finally:
        harness.finish()
    assert len(fake_tool["calls"]) == 1
    name, agent, _args = fake_tool["calls"][0]
    assert name == "statute_search"
    # agent 记成 Controller：审计与 tool_calls 里要能看出"这次检索是 agent 自己发起的"，
    # 记成某个节点名会让排障时分不清是谁调的。
    assert agent == "Controller"
    assert harness.controller.state.tool_calls == 1


def test_case_search_forces_state_top_k(fake_llm, fake_tool) -> None:
    """控制器直调 ``case_search`` 时 ``top_k`` 必须取律师在面板上选的那个。

    用工具默认值（5）而律师选的是 3，接口层的
    ``len(done["cases"]) <= top_k`` 断言会直接被撑爆 ——
    那条断言在 ``test_api.py`` 里，是类案检索页「返回条数」面板的真机守门人。
    """
    state, harness = _ready(top_k=3)
    fake_llm["reply"] = {"action": "call_tool:case_search", "why": "类案池为空"}
    fake_tool["result"] = ToolResult(ok=True, tool="case_search", elapsed_ms=0,
                                     data={"cases": [{"case_name": "某案"}]})
    try:
        _review(state, harness)
    finally:
        harness.finish()
    assert fake_tool["calls"][0][2].get("top_k") == 3


# --------------------------------------------------------------------------- 约束 2：终审不可绕过
def test_call_tool_reruns_validator_and_report(fake_llm, fake_tool) -> None:
    """**本文件最要紧的一条**：补进来的证据必须重新过引用终审。

    控制器直调检索拿到的法条，光并进 ``state["statutes"]`` 是不够的 ——
    终审必须再跑一遍，否则那批法条是"没经过 ``ValidatorNode`` 就进了交付物"，
    而报告是重跑前的那一份、压根没提它们。这里钉三件事：

    1. 新证据真的进了池子（``merge`` 生效）；
    2. 终审段的执行游标被撤回（**而不是**改 route —— route 是计划）；
    3. ``next_node`` 下一步真的会调度回 ``ValidatorNode``（这才是端到端证明）。
    """
    state, harness = _ready()
    fake_llm["reply"] = {"action": "call_tool:statute_search", "why": "法条池为空"}
    try:
        _review(state, harness)
        assert any(s.get("article_no") == "第八十七条" for s in state["statutes"]), \
            "补检到的法条没有并进证据池 —— 终审读的就是这个池子"
        for node in graph.LOOP_RERUN_TAIL:
            assert node not in harness.executed, f"{node} 没被撤回，不会重跑"
        assert graph.next_node(state, harness) == "ValidatorNode", \
            "补检之后没有调度回终审 —— 新证据绕过了引用终审"
    finally:
        harness.finish()


def test_no_gain_does_not_rerun(fake_llm, fake_tool) -> None:
    """补检没拿到新证据时**不重跑终审段**：白跑两三个节点、耗掉步数，结论一个字不变。

    与 ``graph._should_loop`` 的第三层（"没有新证据就不要白转一圈"）同一条纪律。
    """
    state, harness = _ready()
    fake_llm["reply"] = {"action": "call_tool:statute_search", "why": "想补"}
    fake_tool["result"] = ToolResult(ok=True, tool="statute_search", elapsed_ms=0,
                                     data={"statutes": []})
    before = list(harness.executed)
    try:
        _review(state, harness)
    finally:
        harness.finish()
    assert harness.executed == before, "无新增却撤回了终审段"
    assert graph.next_node(state, harness) == graph.END
    # 没补到东西就**不是**关键决策：别推给律师（他看到的会是"补检无新增，
    # 不再重跑终审"这种排障话，而这趟交付物一个字都没变）。
    assert harness.controller.visible_decisions() == [], "无新增却当成关键决策推给律师了"


def test_failed_tool_does_not_rerun(fake_llm, fake_tool) -> None:
    """工具调用失败时不重跑 —— 没有新证据，重跑只会让报告重写一遍同样的内容。"""
    state, harness = _ready()
    fake_llm["reply"] = {"action": "call_tool:statute_search", "why": "想补"}
    fake_tool["result"] = ToolResult(ok=False, tool="statute_search", error="服务不可用")
    before = list(harness.executed)
    try:
        _review(state, harness)
    finally:
        harness.finish()
    assert harness.executed == before


def test_budget_exhausted_blocks_the_rerun(fake_llm, fake_tool, monkeypatch) -> None:
    """剩余步数装不下终审段时**明确报告并带缺口交付**，不启动注定卡住的重跑。

    与 ``_should_loop`` 的预算闸同一算式、同一理由：跑到 Validator 就没步数了、
    Report 不生成，交付物比不重跑更差。
    """
    state, harness = _ready()
    fake_llm["reply"] = {"action": "call_tool:statute_search", "why": "法条池为空"}
    from backend.app.config import settings

    monkeypatch.setattr(settings, "MAX_STEPS", int(state.get("steps") or 0) + 1, raising=False)
    before = list(harness.executed)
    try:
        _review(state, harness)
    finally:
        harness.finish()
    assert harness.executed == before, "步数不够却仍撤回了终审段"
    notes = " ".join(str(e.get("detail") or "") for e in harness.tracer.to_list())
    assert "剩余步数不足" in notes, f"预算不足没有留下说明：{notes[:300]}"


# --------------------------------------------------------------------------- 决策可见
def test_decision_is_recorded_for_the_trace(fake_llm, fake_tool) -> None:
    """关键决策要留痕（约束 4 的"过程中"层）：一句话 + 落 Trace。

    后端**全量落 trace**，不该因为前端只展示一句就丢掉审计。
    """
    state, harness = _ready()
    fake_llm["reply"] = {"action": "call_tool:statute_search", "why": "法条池为空"}
    try:
        _review(state, harness)
    finally:
        harness.finish()
    events = [e for e in harness.tracer.to_list()
              if e.get("node") == "Controller" and e.get("event") == "decision"]
    assert events, "关键决策没有落 Trace"
    assert "法条" in str(events[0].get("detail") or "")
    kinds = [d["kind"] for d in harness.controller.state.decisions]
    assert "call_tool" in kinds


def test_visible_decisions_match_the_decision_trace(fake_llm, fake_tool) -> None:
    """**两份"关键决策"必须是同一个集合**（本文件最容易悄悄分叉的一处）。

    同一句决策走两条投递路径：SSE 那三条链路从 Trace 里挑出 ``Controller/decision``
    推成帧，而 ``/doc/compose`` 不是 SSE，只能随最终 body 一起回
    （``Controller.visible_decisions`` → ``RunOutcome.decisions``）。
    两边各有一套"什么算关键决策"的判断，一旦分叉，表现是**某个入口比别的入口
    多说一句或少说一句** —— 不报错、控制台干干净净，只有对着两个页面比才看得出来。
    """
    state, harness = _ready()
    fake_llm["reply"] = {"action": "call_tool:statute_search", "why": "法条池为空"}
    # 按 chat 那边的**常量**过滤，而不是再抄一遍字面量：分帧条件是
    # ``node == CONTROLLER_NODE and event == CONTROLLER_EVENT``，两边同时改才不破。
    # 各抄一份字面量的话，改了写方（controller）而没改读方（chat）也能全绿。
    from backend.app.api.v1.chat import CONTROLLER_EVENT, CONTROLLER_NODE

    try:
        _review(state, harness)
    finally:
        harness.finish()
    frames = [e["detail"] for e in harness.tracer.to_list()
              if e.get("node") == CONTROLLER_NODE and e.get("event") == CONTROLLER_EVENT]
    assert frames, "一条决策事件都没有，这条用例会变成空转的绿灯"
    assert [d["text"] for d in harness.controller.visible_decisions()] == frames


def test_frames_split_decisions_from_node_traces() -> None:
    """分帧：控制器那句决策单独走 ``event=decision``，**节点级 trace 一个字都不推**。

    这是约束 4「过程中」层在服务端唯一的落点，两条坏法都不报错：
    分流条件写错 → 决策推不出去，界面上什么都不显示，与"这次压根没裁决"一模一样；
    反过来把 trace 也当决策推 → 前端渲染出来就是"StatuteNode · 法规检索"，
    等于把 2026-09-29 摘掉的执行轨迹面板请回来。
    """
    from backend.app.api.v1.chat import frame_of

    got = frame_of({"node": "Controller", "event": "decision", "detail": "补检法条：新增 2 条"})
    assert got == {"event": "decision", "data": {"text": "补检法条：新增 2 条"}}

    # 控制器自己的**其他**事件不是决策：那是排障信息（预算不足、补检无新增、
    # 被哪一层预算闸止住）。它们照旧进 Trace 给排障用，但一个字都不上律师界面。
    for name in ("no_gain", "no_budget", "stop"):
        assert frame_of({"node": "Controller", "event": name, "detail": "x"})["event"] == "trace", \
            f"{name} 被当成关键决策推给律师了"

    # 节点级事件一律原样走 trace，不加工、不丢
    node_ev = {"node": "StatuteNode", "event": "statute_search", "detail": "法规检索 3 条"}
    assert frame_of(node_ev) == {"event": "trace", "data": node_ev}
    # 缺字段（detail 为空）时也要给得出一个能渲染的决策帧，而不是抛 KeyError
    assert frame_of({"node": "Controller", "event": "decision"}) == \
        {"event": "decision", "data": {"text": ""}}


def test_rounds_are_capped(fake_llm, fake_tool, monkeypatch) -> None:
    """轮数上限是硬的：想再试一次也不行（"预算内的自由"）。"""
    from backend.app.config import settings

    state, harness = _ready()
    monkeypatch.setattr(settings, "CONTROLLER_MAX_ROUNDS", 1, raising=False)
    fake_llm["reply"] = {"action": "call_tool:case_search", "why": "想补"}
    fake_tool["result"] = ToolResult(ok=True, tool="case_search", elapsed_ms=0,
                                     data={"cases": [{"case_name": "某案"}]})
    try:
        _review(state, harness)          # 第 1 轮：允许
        assert harness.controller.state.rounds == 1
        state["route"] = list(INTENT_ROUTES["case_search"]["nodes"])  # type: ignore[typeddict-item]
        harness.executed = ["IntentNode"] + list(state["route"])
        _review(state, harness)          # 第 2 轮：应被闸住
    finally:
        harness.finish()
    assert harness.controller.state.rounds == 1, "轮数上限没有拦住第二轮"
    assert harness.controller.state.stopped_by == "rounds"
    assert len(fake_tool["calls"]) == 1
    # 止于哪一层必须**在 Trace 里点得出名**（不是只留在内存的 stopped_by）：
    # 否则"这次为什么没补证据"只能靠日志，而日志与某一次任务对不上号。
    stops = [e for e in harness.tracer.to_list()
             if e.get("node") == "Controller" and e.get("event") == "stop"]
    assert stops, "被闸住却没写 Trace"
    assert "轮数上限" in str(stops[-1].get("detail")), stops[-1]
    # 但它**不是**决策事件：律师界面上不该多出这一行
    # （第 1 轮那次成功的补检仍在可见集合里 —— 这里判的是"撞闸"这一句不在）
    assert not any("轮数上限" in str(d.get("text") or "")
                   for d in harness.controller.visible_decisions())


def test_ruling_events_land_in_state_trace(fake_llm, fake_tool, monkeypatch) -> None:
    """裁决写的事件要落进 ``state["trace"]``，**哪怕裁完就直接收尾**。

    为什么这条值得单独钉：``flush_trace`` 一直是在**节点收尾时**调的，而控制器
    活在节点之外。裁决之后最常见的一条路恰恰是"不补检、直接交付"——
    那时最后一个节点已经收过尾了，这些事件就只活在 Tracer 的内存里：
    实时 SSE 推过一句，``GET /tasks/{id}/trace`` 里却查无此条。
    两条路径对不上，而两边都不报错。
    """
    from backend.app.config import settings

    state, harness = _ready()
    # route 只留一个节点：跑完它就轮到裁决，裁完直接 END（正是要覆盖的那条路）
    state["route"] = ["StatuteNode"]  # type: ignore[typeddict-item]
    harness.executed = []
    monkeypatch.setattr(settings, "CONTROLLER_MAX_ROUNDS", 0, raising=False)  # 必然撞闸

    async def _stub(state_, harness_):  # noqa: ANN001
        return state_

    monkeypatch.setitem(graph.NODES, "StatuteNode", _stub)  # type: ignore[arg-type]
    asyncio.run(graph._run_sequential(state, harness))

    stored = [e for e in (state.get("trace") or []) if e.get("node") == "Controller"]
    assert stored, "裁决事件没有合并进 state['trace']（实时推过、落库没有）"
    assert any(e.get("event") == "stop" for e in stored), stored
