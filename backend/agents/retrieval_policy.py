"""专家检索策略：把专家声明域 / 检索增强词从 prompt 软指引提升为节点级硬约束。

背景
----
此前"定向检索指引"只写在 system prompt 里，但检索节点是代码直调
``harness.tool("statute_search" / "case_search" / "knowledge_search", ...)``，
模型并不负责调检索工具，因此专家声明域实际上从未进入检索参数——
专家版与普通版的检索口径完全相同，这是"差别看不出来"的机制根因。

本模块提供两个纯函数，由三个检索节点统一调用：
- :func:`effective_domain`：专家声明域优先；未选专家 / 专家未配置 domains 时回退意图域。
- :func:`enrich_query`：在检索词上追加专家检索增强词，聚焦专业召回；未配置时原样返回。

未选专家（``expert_domains`` 为空）时两者都逐字保持原行为，不影响普通助手。
"""

from __future__ import annotations

from typing import Any


def effective_domain(state: dict[str, Any]) -> str:
    """专家声明域优先，无专家域时回退意图域（``state.domain``）。

    ``expert_domains`` 取第一个生效；意图域来自 ``intent_node`` 推断，与专家无关。
    未选专家或专家未配置 domains 时返回意图域，行为与改造前一致。
    """
    domains = [str(d) for d in (state.get("expert_domains") or []) if str(d)]
    if domains:
        return domains[0]
    return str(state.get("domain") or "")


def enrich_query(state: dict[str, Any], query: str) -> str:
    """在检索词上追加专家检索增强词（``expert_hints`` 至多前 3 个）。

    保持检索式数量不变（不突破各工具配额），只把专业线索并入 query 提升召回聚焦。
    未选专家 / 未配置 hints / query 为空时原样返回。
    """
    q = str(query or "").strip()
    if not q:
        return q
    hints = [str(h) for h in (state.get("expert_hints") or []) if str(h)]
    if not hints:
        return q
    return f"{q} {' '.join(hints[:3])}".strip()
