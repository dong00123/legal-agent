"""A2A 协同层回归测试。

这里的用例价值集中在两件事：

1. **契约一致性**。路由表声明的节点必须都有注册函数。这类错位不会在启动时暴露 ——
   流程跑到那一步才算不出下一步，然后 ``next_node`` 返回 END **静默结束**，
   产出物看起来只是"少了一段"。启动即报错，比事后从报告里倒查便宜得多。

2. **失败必须是显式的**。委派不到就返回 ``ok=False`` 并带原因，
   不能静默退回进程内、也不能抛异常把整条流水线打断 ——
   一个 Agent 挂了，正确行为通常是"记一条缺口、继续跑别的"。

HTTP 通道（跨进程委派）的**收发两端**分别由 ``A2AClient._http`` 与
``app/api/v1/agents.py`` 的 ``/a2a/remote/{agent}`` 实现；这里只覆盖
"未配置远端地址时给出明确原因"与契约一致性 —— 真实网络往返属于集成测试，
放到需要起两个实例的场合再测，单元测试里 mock 掉的 httpx 断言价值很低。
"""

from __future__ import annotations

import asyncio

import pytest

from backend.agents.harness import Harness
from backend.agents.harness.router import INTENT_ROUTES
from backend.agents.nodes import NODE_FUNCS
from backend.agents.protocol import (
    A2AAction,
    A2AClient,
    A2ATransport,
    CARDS,
    card_for,
    cards_for_intent,
    directory,
    health,
    node_of,
)
from backend.agents.state import new_state


# --------------------------------------------------------------------------- 一致性
def test_every_routed_node_is_registered() -> None:
    declared: set[str] = set()
    for route in INTENT_ROUTES.values():
        declared |= set(route.get("nodes") or [])
    declared.add("IntentNode")
    missing = declared - set(NODE_FUNCS)
    assert not missing, f"以下节点在路由表里但未注册函数，流程会中途静默结束：{missing}"


def test_health_reports_no_missing() -> None:
    assert health()["missing_nodes"] == []


def test_cards_cover_all_nodes() -> None:
    assert set(CARDS) == set(NODE_FUNCS)


def test_cards_produces_come_from_planner_table() -> None:
    """卡片产出与 Planner 的预期表同源，避免两边分叉。"""
    from backend.agents.harness.planner import NODE_PRODUCES

    for node, card in CARDS.items():
        assert list(card.produces) == list(NODE_PRODUCES.get(node, []))


def test_cards_intents_derived_from_router() -> None:
    for node, card in CARDS.items():
        expected = tuple(sorted(k for k, v in INTENT_ROUTES.items()
                                if node in (v.get("nodes") or [])))
        assert card.intents == expected


def test_card_lookup_by_agent_name_and_node_name() -> None:
    """调用方写 Agent 名（statute）或节点名（StatuteNode）都应能解析。"""
    assert card_for("StatuteNode") is not None
    assert card_for("statute") is not None
    assert node_of("statute") == "StatuteNode"
    assert node_of("StatuteNode") == "StatuteNode"
    assert node_of("nope") == ""


def test_cards_for_intent_follows_route_order() -> None:
    nodes = [c.node for c in cards_for_intent("contract_review")]
    assert nodes == list(INTENT_ROUTES["contract_review"]["nodes"])


def test_directory_serializable() -> None:
    rows = directory()
    assert len(rows) == len(CARDS)
    assert all({"id", "node", "produces"} <= set(r) for r in rows)


# --------------------------------------------------------------------------- 失败路径
def _client(**kw) -> A2AClient:
    return A2AClient(None, **kw)


def test_ping_unknown_agent_fails_explicitly() -> None:
    art = asyncio.run(_client().call("nope", action=A2AAction.PING))
    assert not art.ok and "未注册" in art.error


def test_ping_known_agent_returns_card() -> None:
    art = asyncio.run(_client().call("case", action=A2AAction.PING))
    assert art.ok and art.fields["card"]["id"] == "case"


def test_run_without_harness_fails_explicitly() -> None:
    """没有运行时就没有 State 可操作 —— 直接说清楚，别自己造空 State 跑。"""
    art = asyncio.run(_client().call("StatuteNode"))
    assert not art.ok and "无运行时" in art.error


def test_verify_rejected_for_non_verifier() -> None:
    """VERIFY 是有真实约束的动作，不是装饰 —— 只有核对 Agent 提供该能力。"""
    art = asyncio.run(_client().call("case", action=A2AAction.VERIFY))
    assert not art.ok and "不提供核对能力" in art.error


def test_http_transport_without_remote_fails_explicitly() -> None:
    """未配置远端时必须报错，不能静默退回进程内 ——
    静默降级会让运维以为"已经拆出去了"，排查方向全错。"""
    cli = A2AClient(None, transport=A2ATransport.HTTP)
    art = asyncio.run(cli.call("case", action=A2AAction.PING))
    assert art.ok          # PING 不走通道
    art = asyncio.run(cli.call("StatuteNode"))
    assert not art.ok and "HTTP 通道未配置" in art.error


# --------------------------------------------------------------------------- 真实委派
def test_in_process_delegation_runs_node() -> None:
    """真跑一次：委派 IntentNode，返回物里应带上它写进 State 的字段。"""
    state = new_state("公司把我辞退了，不给经济补偿", doc_type="contract")
    harness = Harness.start(state, client="test", username="pytest")
    try:
        art = asyncio.run(A2AClient(harness).call("IntentNode"))
        assert art.ok, art.error
        assert art.transport == A2ATransport.IN_PROCESS.value
        assert art.fields.get("intent"), "IntentNode 未产出 intent"
        assert art.fields["intent"] == state.get("intent")
        assert art.summary
    finally:
        harness.finish()


def test_delegation_payload_overrides_state() -> None:
    """payload 只覆盖显式给出的键，不动已有证据。"""
    state = new_state("原始提问")
    state["parties"] = [{"name": "甲公司", "role": "原告"}]
    harness = Harness.start(state, client="test", username="pytest")
    try:
        art = asyncio.run(A2AClient(harness).call(
            "IntentNode", payload={"query": "换成这个检索式"}))
        assert art.ok, art.error
        assert state["query"] == "换成这个检索式"
        assert state["parties"], "payload 不应清掉已有证据"
    finally:
        harness.finish()


def test_delegation_reports_only_new_gaps() -> None:
    """返回物的 gaps 只含**本次委派新增**的缺口，不含调用方原有的。"""
    state = new_state("测试", doc_type="statute")
    state["evidence_gap"] = [{"kind": "x", "reason": "调用方原有的缺口", "impact": ""}]
    harness = Harness.start(state, client="test", username="pytest")
    try:
        art = asyncio.run(A2AClient(harness).call("IntentNode"))
        assert art.ok, art.error
        reasons = [g.get("reason") for g in art.gaps]
        assert "调用方原有的缺口" not in reasons
    finally:
        harness.finish()
