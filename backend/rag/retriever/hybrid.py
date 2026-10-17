"""Hybrid 检索：向量 + BM25 → RRF 融合 → Rerank → Top-K → Evidence。

对应方案 8.2 检索链路：

    Query
     ↓ Hybrid Retrieval（Milvus 向量 + BM25）
     ↓ 融合召回（RRF）
     ↓ Rerank
     ↓ Top-K
     ↓ Evidence（强制携带出处：法规名+条文号+效力状态 / 案号+法院+裁判日期 / 指引版本）
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from backend.app.config import settings
from backend.rag.embedding import embedding_client
from backend.rag.retriever.bm25 import get_bm25
from backend.rag.retriever.rerank import get_reranker
from backend.rag.retriever.vector import get_vector_store

logger = logging.getLogger("legal_agent.rag.hybrid")


@dataclass
class RetrievalTrace:
    query: str = ""
    corpus: str = ""
    vector_hits: int = 0
    bm25_hits: int = 0
    fused: int = 0
    #: 实际送进精排的候选数。与 ``fused`` 分开记，是因为截断后两者不等 ——
    #: 排查"精排为什么慢"时要看的是这个数，不是融合出来的总数。
    rerank_pool: int = 0
    returned: int = 0
    elapsed_ms: int = 0
    degraded: bool = False
    notes: list[str] = field(default_factory=list)


async def hybrid_search(
    query: str,
    corpus: str,
    *,
    top_k: int | None = None,
    retrieve_k: int | None = None,
    rrf_k: int | None = None,
    filters: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], RetrievalTrace]:
    """混合检索。返回 (结果列表, 检索轨迹)。

    单路失败不致命：向量挂掉只用 BM25，BM25 挂掉只用向量；
    两路都空则返回空列表并由上层写入 ``evidence_gap``。
    """
    started = time.perf_counter()
    top_k = top_k or settings.RERANK_TOP_K
    retrieve_k = retrieve_k or settings.RETRIEVE_TOP_K
    rrf_k = rrf_k or settings.RRF_K

    trace = RetrievalTrace(query=query, corpus=corpus)

    async def run_vector() -> list[dict[str, Any]]:
        t0 = time.perf_counter()
        try:
            vec = await embedding_client.encode_one(query, is_query=True)
            store = get_vector_store()
            hits = await asyncio.to_thread(store.search, corpus, vec, retrieve_k * 2)
            trace.notes.append(f"向量检索耗时 {int((time.perf_counter() - t0) * 1000)}ms")
            return hits
        except Exception as exc:  # noqa: BLE001
            trace.notes.append(
                f"向量检索不可用（{int((time.perf_counter() - t0) * 1000)}ms）：{type(exc).__name__}")
            trace.degraded = True
            return []

    async def run_bm25() -> list[dict[str, Any]]:
        t0 = time.perf_counter()
        try:
            idx = get_bm25(corpus)
            hits = await asyncio.to_thread(idx.search, query, retrieve_k * 2)
            trace.notes.append(f"BM25 检索耗时 {int((time.perf_counter() - t0) * 1000)}ms")
            return hits
        except Exception as exc:  # noqa: BLE001
            trace.notes.append(
                f"BM25 检索不可用（{int((time.perf_counter() - t0) * 1000)}ms）：{type(exc).__name__}")
            trace.degraded = True
            return []

    vec_hits, kw_hits = await asyncio.gather(run_vector(), run_bm25())
    trace.vector_hits = len(vec_hits)
    trace.bm25_hits = len(kw_hits)

    # ---- RRF 融合：score = Σ 1/(k + rank)
    fused: dict[str, dict[str, Any]] = {}
    for source, hits in (("vector", vec_hits), ("bm25", kw_hits)):
        for rank, h in enumerate(hits, start=1):
            cid = h["chunk_id"]
            entry = fused.setdefault(
                cid,
                {
                    "chunk_id": cid,
                    "text": h.get("text", ""),
                    "corpus": h.get("corpus", corpus),
                    "metadata": h.get("metadata") or {},
                    "fusion_score": 0.0,
                    "vector_score": 0.0,
                    "bm25_score": 0.0,
                    "sources": [],
                },
            )
            entry["fusion_score"] += 1.0 / (rrf_k + rank)
            if source == "vector":
                entry["vector_score"] = float(h.get("score", 0.0))
            else:
                entry["bm25_score"] = float(h.get("score", 0.0))
            entry["sources"].append(source)
            if not entry["text"]:
                entry["text"] = h.get("text", "")

    candidates = list(fused.values())
    trace.fused = len(candidates)

    if filters:
        candidates = [
            c for c in candidates
            if all((c.get("metadata") or {}).get(k) == v for k, v in filters.items())
        ]

    candidates.sort(key=lambda x: -x["fusion_score"])
    # 精排池上限。``max(top_k * 4, retrieve_k)`` 在合同审查那条链上会算出 60
    # （knowledge_search 传 top_k*3=15 → 15*4=60），而 CrossEncoder 在 CPU 上
    # 是**线性于条数**的重活：实测（20 核 / 无 GPU）20 对 0.57s、40 对 0.96s、
    # 60 对 1.49s、80 对 2.28s —— 一条合同审查光精排就吃掉 6~9 秒。
    #
    # 为什么下限取 30 而不是更小：实测把池从 60 砍到 24 后，**第 1~5 名完全
    # 不变**，差异从第 6 名才开始（该处候选分已跌到 0.05/0.04 量级，两条
    # 文档本身就几乎同分）。30 是"覆盖到 RERANK_TOP_K=6 且留出排序余量"，
    # 实测 top6 与全量精排完全一致，耗时减半。
    #
    # RRF 融合分已把两路排序归一化，排在这个上限之后的候选翻盘概率极低，
    # 送进 CrossEncoder 基本是白烧 CPU —— 但不设下限，否则召回会塌。
    pool_limit = max(30, int(settings.RERANK_TOP_K) * 5)
    pool = candidates[: min(len(candidates), pool_limit)]
    trace.fused = len(candidates)
    trace.rerank_pool = len(pool)

    # ---- Rerank
    reranker = get_reranker()
    t_rerank = time.perf_counter()
    try:
        ranked = await asyncio.to_thread(reranker.rerank, query, pool, top_k)
        trace.notes.append(
            f"精排耗时 {int((time.perf_counter() - t_rerank) * 1000)}ms（池 {len(pool)} 条）")
    except Exception as exc:  # noqa: BLE001
        # ⚠ 必须置 degraded：ToolResult.degraded 取的就是它，
        # 不置的话精排失败会被当成"检索完全正常"，工具层不会登记证据缺口，
        # 律师只看到结果排序变差，却看不到任何说明。
        trace.degraded = True
        trace.notes.append(
            f"精排失败（{int((time.perf_counter() - t_rerank) * 1000)}ms），退回融合排序：{exc}")
        pool.sort(key=lambda x: -x["fusion_score"])
        ranked = pool[:top_k]

    trace.returned = len(ranked)
    trace.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return ranked, trace


# --------------------------------------------------------------------------- 按语料封装
async def search_statutes(query: str, top_k: int = 6, **kw: Any) -> tuple[list[dict[str, Any]], RetrievalTrace]:
    return await hybrid_search(query, "statute", top_k=top_k, **kw)


async def search_cases(query: str, top_k: int = 6, **kw: Any) -> tuple[list[dict[str, Any]], RetrievalTrace]:
    return await hybrid_search(query, "case", top_k=top_k, **kw)


async def search_knowledge(query: str, top_k: int = 6, **kw: Any) -> tuple[list[dict[str, Any]], RetrievalTrace]:
    return await hybrid_search(query, "knowledge", top_k=top_k, **kw)


async def search_materials(query: str, top_k: int = 8, case_id: str = "", **kw: Any) -> tuple[list[dict[str, Any]], RetrievalTrace]:
    filters = {"case_id": case_id} if case_id else None
    return await hybrid_search(query, "material", top_k=top_k, filters=filters, **kw)


async def search_memory(query: str, top_k: int = 5, **kw: Any) -> tuple[list[dict[str, Any]], RetrievalTrace]:
    return await hybrid_search(query, "memory", top_k=top_k, **kw)
