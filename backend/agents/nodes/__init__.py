"""Agent 节点导出表。

这里是「节点名 → 节点函数」的**唯一事实来源**。``graph.py`` 与 ``protocol``
都从这里取，避免出现"图上登记了某个节点、实际函数表里没有"这类
只在运行到那一步才暴露的错位。

节点函数签名统一为 ``async def fn(state: LegalState, harness: Harness) -> LegalState``。
这个签名在 LangGraph 下有个容易踩的坑：图的节点包装器**只往函数里传 state**，
多一个必填参数就会抛 ``TypeError``。Harness 因此走 ``ContextVar`` 传递，
具体的包装逻辑见 ``agents.graph._graph_node`` / ``_exec_node``。

节点名（``NodeName``）与 ``router.INTENT_ROUTES`` 里的字符串必须逐字一致 ——
路由表写 "ValidatorNode" 而这里注册 "validator_node" 的话，
``next_node`` 会算不出下一步，流程在第一步之后静默结束。
"""

from __future__ import annotations

from typing import Callable

from backend.agents.nodes.analysis_node import analysis_node
from backend.agents.nodes.case_node import case_node
from backend.agents.nodes.draft_node import draft_node
from backend.agents.nodes.intake_node import intake_node
from backend.agents.nodes.intent_node import intent_node
from backend.agents.nodes.knowledge_node import knowledge_node
from backend.agents.nodes.memory_node import memory_node
from backend.agents.nodes.report_node import report_node
from backend.agents.nodes.review_node import review_node
from backend.agents.nodes.statute_node import statute_node
from backend.agents.nodes.validator_node import validator_node

NodeFn = Callable[..., object]

# 顺序即「执行顺序的推荐值」，实际执行顺序由 Router 的 INTENT_ROUTES 决定。
NODE_FUNCS: dict[str, NodeFn] = {
    "IntentNode": intent_node,
    "IntakeNode": intake_node,
    "StatuteNode": statute_node,
    "CaseNode": case_node,
    "KnowledgeNode": knowledge_node,
    "ReviewNode": review_node,
    "AnalysisNode": analysis_node,
    "ValidatorNode": validator_node,
    "DraftNode": draft_node,
    "ReportNode": report_node,
    # MemoryNode 不在任何 INTENT_ROUTES 里：它是收尾归档节点，由
    # ``app.services`` 在任务结束后单独调用（归档失败不该影响交付物）。
    "MemoryNode": memory_node,
}

__all__ = [
    "NODE_FUNCS",
    "NodeFn",
    "analysis_node",
    "case_node",
    "draft_node",
    "intake_node",
    "intent_node",
    "knowledge_node",
    "memory_node",
    "report_node",
    "review_node",
    "statute_node",
    "validator_node",
]
