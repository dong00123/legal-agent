"""A2A Agent 卡片注册表。

卡片从 ``planner`` 的节点元数据表**派生**，不另写一份：
``NODE_AGENT``（节点→Agent）、``NODE_ACTION``（能力描述）、``NODE_TOOLS``（使用的工具）、
``NODE_PRODUCES``（承诺产出）。另写一份的后果是两边慢慢分叉 ——
卡片说 Case Agent 会产出 cases、而路由表某次改动把它从路径里去掉了，
调用方就一直在等一个永远不来的产出，而且**不会报错**。

``intents`` 由 ``INTENT_ROUTES`` 反推：某个 Agent 出现在哪些意图的路径上，
它就在这些意图下可被委派。这样"新增 Agent 不影响存量"这句话才是可验证的 ——
新 Agent 只要注册进 ``NODE_FUNCS`` 并在路由表里出现，卡片自动就有了。
"""

from __future__ import annotations

from typing import Any

from backend.agents.harness.planner import (
    NODE_ACTION,
    NODE_AGENT,
    NODE_PRODUCES,
    NODE_TOOLS,
)
from backend.agents.harness.router import (
    INTENT_ROUTES,
    PRODUCT_AGENTS,
    product_directory,
)
from backend.agents.nodes import NODE_FUNCS
from backend.agents.protocol.message import AgentCard

# 节点 → 该节点依赖的 State 字段（被调方拿不到这些时无法工作）
NODE_REQUIRES: dict[str, tuple[str, ...]] = {
    "IntakeNode": ("query",),
    "StatuteNode": ("query",),
    "CaseNode": ("query",),
    "KnowledgeNode": ("query",),
    "ReviewNode": ("material_text",),
    "AnalysisNode": ("query",),
    "ValidatorNode": ("citations",),
    "DraftNode": ("query",),
    "ReportNode": (),
    "MemoryNode": ("task_id",),
    "IntentNode": ("query",),
}


def _intents_for(node: str) -> tuple[str, ...]:
    return tuple(sorted(k for k, v in INTENT_ROUTES.items() if node in (v.get("nodes") or [])))


def build_cards() -> dict[str, AgentCard]:
    """构造全部 Agent 卡片。节点名作为键（与路由表同一命名空间）。"""
    cards: dict[str, AgentCard] = {}
    for node, fn in NODE_FUNCS.items():
        agent = NODE_AGENT.get(node, node)
        cards[node] = AgentCard(
            id=agent,
            name=f"{agent} Agent",
            description=NODE_ACTION.get(node, ""),
            node=node,
            intents=_intents_for(node),
            tools=tuple(NODE_TOOLS.get(node, ())),
            produces=tuple(NODE_PRODUCES.get(node, ())),
            requires=NODE_REQUIRES.get(node, ()),
            version="1.0.0",
        )
    return cards


CARDS: dict[str, AgentCard] = build_cards()


def card_for(node_or_agent: str) -> AgentCard | None:
    """按节点名或 Agent 名取卡片（两种写法都支持，调用方不必先对齐命名）。"""
    hit = CARDS.get(node_or_agent)
    if hit is not None:
        return hit
    for card in CARDS.values():
        if card.id == node_or_agent:
            return card
    return None


def node_of(node_or_agent: str) -> str:
    """把 Agent 名解析回节点名；未知返回空串。"""
    card = card_for(node_or_agent)
    return card.node if card else ""


def cards_for_intent(intent: str) -> list[AgentCard]:
    """某个意图下会被委派到的 Agent（按路由顺序）。"""
    route = INTENT_ROUTES.get(intent) or {}
    return [CARDS[n] for n in (route.get("nodes") or []) if n in CARDS]


def directory() -> list[dict[str, Any]]:
    """可对外暴露的 Agent 目录（管理后台 / 调试用）。

    ⚠ 这份是**节点级**的实现清单（11 个），不是律师在侧边栏看到的那 4 个。
    对外的 4 个走 :func:`products`。
    """
    return [c.to_dict() for c in CARDS.values()]


def products() -> list[dict[str, Any]]:
    """对外的 4 个产品 Agent（侧边栏那 4 个入口）。

    与 :func:`directory` 的区别见 ``harness.router.ProductAgent`` 的注释：
    一个是实现、一个是门面。两者都从这里出，是为了让"对外有几个 Agent"
    这个问题只有一个答案来源，而不是前端硬编一份、后端再硬编一份。
    """
    return product_directory()


def product_health() -> dict[str, Any]:
    """产品 Agent 的一致性自检：**钉的意图必须真的登记过，且节点都有实现**。

    这类错位不会在启动时暴露，也不会在请求时暴露成 500 ——
    症状是"这个页面的结果看起来少了点东西"，而日志里一行都没有。
    所以它必须是一条**可断言的**检查，而不是"约定俗成要对应上"。

    ``unpinned`` 单列出来而不是算作错误：``assistant`` 是唯一不钉意图的入口，
    由 IntentNode 判定路径，这是设计如此。列出来是为了让"到底有几个没钉"
    在管理后台一眼可见 —— 有一天多出一个没钉意图的入口，它就会出现在这里。
    """
    problems: list[dict[str, str]] = []
    unpinned: list[str] = []
    for spec in PRODUCT_AGENTS.values():
        if not spec.intent:
            unpinned.append(spec.id)
            continue
        route = INTENT_ROUTES.get(spec.intent)
        if route is None:
            problems.append({"agent": spec.id, "kind": "unknown_intent",
                             "detail": f"意图「{spec.intent}」不在 INTENT_ROUTES 里"})
            continue
        missing = [n for n in (route.get("nodes") or []) if n not in NODE_FUNCS]
        if missing:
            problems.append({"agent": spec.id, "kind": "missing_nodes",
                             "detail": f"意图「{spec.intent}」的路由含未实现节点：{'、'.join(missing)}"})
    return {
        "products": len(PRODUCT_AGENTS),
        "internal_nodes": len(CARDS),
        "unpinned": sorted(unpinned),
        "problems": problems,
        "ok": not problems,
    }


def health() -> dict[str, Any]:
    """一致性自检：**路由表声明但未注册函数的节点**必须为零。

    这类错位不会在启动时暴露：流程跑到那一步才算不出下一步，然后**静默结束**
    （``next_node`` 返回 END），产出物看起来只是"内容少了一段"。
    """
    declared: set[str] = set()
    for route in INTENT_ROUTES.values():
        declared |= set(route.get("nodes") or [])
    declared.add("IntentNode")   # 入口节点不在任何路由列表里
    missing = sorted(declared - set(NODE_FUNCS))
    return {
        "agents": len(CARDS),
        "declared_nodes": len(declared),
        "missing_nodes": missing,
        "orphan_nodes": sorted(set(NODE_FUNCS) - declared),
    }


__all__ = [
    "CARDS",
    "NODE_REQUIRES",
    "build_cards",
    "card_for",
    "cards_for_intent",
    "directory",
    "health",
    "node_of",
    "product_health",
    "products",
]
