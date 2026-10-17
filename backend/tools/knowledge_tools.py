"""知识类 MCP Tool：律所知识库检索。

方案第五章的工具表里没有单列"知识库检索"（Knowledge Agent 被描述为直接做 RAG），
这里补一个工具的**理由**：所有证据检索都必须走同一条路径，才能保证
权限隔离、访问审计、Trace 记账、次数配额四件事不会在某个通道上被绕过。
知识库同样属于"本所内部资料"，读它也是材料访问，一样要留痕。

对应方案 4.1 Knowledge Agent 约束：**标注指引版本**。
指引不标版本比不检索更危险 —— 律师会以为那是当前有效的作业标准。
"""

from __future__ import annotations

import logging

from backend.rag.domain import filter_knowledge_by_domain
from backend.rag.pipeline import retrieve_knowledge
from backend.tools.base import ToolResult

logger = logging.getLogger("legal_agent.tools.knowledge")


async def knowledge_search(
    query: str,
    top_k: int = 5,
    *,
    doc_type: str = "",
    domain: str = "",
) -> ToolResult:
    """律所知识库检索：办案指引、审查清单、合同模板、文书规范。

    Args:
        query: 检索式。
        top_k: 返回条数。
        doc_type: 过滤文档类型（审查清单 / 办案指引 / 合同模板 / 文书规范）。
        domain: 法律领域过滤（labor/tax/env/civil/unknown）。
            **跨领域错配治理**：按 title 领域词表滤除无关指引（如劳动问题不再
            召回环保类办案指引）；unknown/空值不做过滤（退回改造前行为）。
    """
    hits, trace = await retrieve_knowledge(query, top_k=top_k * 3 if doc_type or domain else top_k)
    items = [h.model_dump(mode="json") for h in hits]

    if doc_type:
        items = [i for i in items if doc_type in (i.get("doc_type") or "")]

    if domain:
        before = len(items)
        items = filter_knowledge_by_domain(items, domain)
        trace.notes.append(
            f"按领域「{domain}」过滤：知识库命中 {before} 条中保留 {len(items)} 条，"
            f"滤除 {before - len(items)} 条跨领域错配")

    items = items[:top_k]

    # 版本缺失要显式提示：指引必须标版本，否则无法判断作业标准是否已更新
    unversioned = [i.get("title") or "" for i in items if not i.get("version")]
    notes = list(trace.notes)
    if unversioned:
        notes.append(f"{len(unversioned)} 份指引未标注版本，引用前请确认是否为最新版"
                     f"（{'、'.join(unversioned[:3])}）")

    if not items:
        notes.append("知识库中未检索到相关指引，本次结论未参考本所作业标准")

    return ToolResult(
        ok=True,
        tool="knowledge_search",
        data={
            "query": query,
            "count": len(items),
            "knowledges": items,
            "versions": {i.get("title") or "": i.get("version") or "未标注" for i in items},
            "trace": {
                "vector_hits": trace.vector_hits,
                "bm25_hits": trace.bm25_hits,
                "fused": trace.fused,
                "elapsed_ms": trace.elapsed_ms,
                "degraded": trace.degraded,
                "notes": trace.notes,
            },
        },
        degraded=trace.degraded or bool(unversioned),
        fallback_note="；".join(notes),
    )
