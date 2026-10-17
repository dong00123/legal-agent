"""RAG 统一门面：入库（indexing）与检索（retrieval）。

上层（MCP Tool / Agent 节点）只依赖这一层，不直接碰 Milvus / BM25 / 分词细节。
检索结果统一转换为带出处的 Evidence 实体：
- 法条 → 法规名 + 条文号 + 效力状态
- 判例 → 案号/案例名称 + 法院 + 裁判日期
- 知识 → 指引版本
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from backend.agents.state import CaseHit, KnowledgeHit, StatuteHit
from backend.llm.evidence_sanitize import sanitize_display_title, sanitize_evidence_text
from backend.rag.chunker import Chunk
from backend.rag.embedding import embedding_client
from backend.rag.retriever.bm25 import bm25_status, get_bm25
from backend.rag.retriever.hybrid import (
    RetrievalTrace,
    hybrid_search,
    search_cases,
    search_knowledge,
    search_materials,
    search_memory,
    search_statutes,
)
from backend.rag.retriever.rerank import get_reranker
from backend.rag.retriever.vector import get_vector_store, vector_status

logger = logging.getLogger("legal_agent.rag.pipeline")


# --------------------------------------------------------------------------- 入库
@dataclass
class IndexResult:
    corpus: str
    chunks: int
    vector_written: int = 0
    bm25_written: int = 0
    vector_backend: str = ""
    bm25_engine: str = ""
    degraded: bool = False
    errors: list[str] = None  # type: ignore[assignment]

    def to_dict(self) -> dict[str, Any]:
        return {
            "corpus": self.corpus,
            "chunks": self.chunks,
            "vector_written": self.vector_written,
            "bm25_written": self.bm25_written,
            "vector_backend": self.vector_backend,
            "bm25_engine": self.bm25_engine,
            "degraded": self.degraded,
            "errors": self.errors or [],
        }


async def index_chunks(chunks: list[Chunk], corpus: str | None = None) -> IndexResult:
    """把一个文档切出的 chunks 同时写入向量库与 BM25。"""
    if not chunks:
        return IndexResult(corpus=corpus or "", chunks=0, errors=[])

    corpus = corpus or chunks[0].corpus
    res = IndexResult(corpus=corpus, chunks=len(chunks), errors=[])

    items = [
        {"chunk_id": c.chunk_id, "text": c.text, "corpus": c.corpus, "metadata": c.metadata}
        for c in chunks
    ]

    # 向量
    try:
        vectors = await embedding_client.encode([c.text for c in chunks], is_query=False)
        store = get_vector_store()
        payload = [
            {"chunk_id": it["chunk_id"], "vector": vec, "text": it["text"],
             "corpus": it["corpus"], "metadata": it["metadata"]}
            for it, vec in zip(items, vectors)
        ]
        res.vector_written = await _to_thread(store.upsert, corpus, payload)
        res.vector_backend = store.name
    except Exception as exc:  # noqa: BLE001
        res.degraded = True
        res.errors.append(f"向量入库失败：{exc}")
        logger.warning("向量入库失败（%s）：%s", corpus, exc)

    # BM25
    try:
        idx = get_bm25(corpus)
        res.bm25_written = await _to_thread(idx.add, items, save=True)
        res.bm25_engine = type(idx).__name__
    except Exception as exc:  # noqa: BLE001
        res.degraded = True
        res.errors.append(f"BM25 入库失败：{exc}")
        logger.warning("BM25 入库失败（%s）：%s", corpus, exc)

    return res


async def _to_thread(fn, *args, **kwargs):
    import asyncio

    return await asyncio.to_thread(fn, *args, **kwargs)


async def remove_source(corpus: str, source_path: str) -> dict[str, int]:
    """按来源文件删除（离线更新包回滚时使用）。"""
    out = {"vector": 0, "bm25": 0}
    try:
        store = get_vector_store()
        if hasattr(store, "delete_by_source"):
            out["vector"] = await _to_thread(store.delete_by_source, corpus, source_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("向量删除失败：%s", exc)
    try:
        idx = get_bm25(corpus)
        out["bm25"] = await _to_thread(idx.remove_by_source, source_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("BM25 删除失败：%s", exc)
    return out


# --------------------------------------------------------------------------- 检索 → Evidence
def _hit_to_statute(hit: dict[str, Any]) -> StatuteHit:
    meta = hit.get("metadata") or {}
    from backend.parser.structure import check_statute_status, StatuteMeta

    status = meta.get("status") or ""
    if not status:
        # 库里没标状态时按日期推算，仍标注为推算结果
        meta_obj = StatuteMeta(
            effective_date=_parse_date(meta.get("effective_date")),
            repealed_date=_parse_date(meta.get("repealed_date")),
        )
        status = check_statute_status(meta_obj)
    return StatuteHit(
        doc_name=sanitize_display_title(meta.get("doc_name") or ""),
        article_no=meta.get("article_no") or "",
        text=sanitize_evidence_text(hit.get("text") or ""),
        status=status,  # type: ignore[arg-type]
        effective_date=_parse_date(meta.get("effective_date")),
        repealed_date=_parse_date(meta.get("repealed_date")),
        version=meta.get("version") or "",
        publisher=meta.get("publisher") or "",
        chunk_id=hit.get("chunk_id") or "",
        source_path=meta.get("source_path") or "",
        score=float(hit.get("rerank_score", hit.get("fusion_score", 0.0))),
        retriever=hit.get("retriever") or "hybrid",
    )


def hit_to_statute(hit: dict[str, Any]) -> StatuteHit:
    """公开包装：把**原始检索命中**（BM25/向量返回的 dict）转成证据对象。

    条文精确定位（``statute_tools._exact_article``）拿到的是原始命中，
    但它走的是与混合检索不同的通道，必须先经过同一套「检索结果 → 证据」转换
    才能与其它法条并列 —— 否则效力状态推算、字段补齐这些逻辑会在两条通道上
    各写一遍，最终表现为"精确命中的法条没有效力状态"。
    """
    return _hit_to_statute(hit)


def _hit_to_case(hit: dict[str, Any]) -> CaseHit:
    meta = hit.get("metadata") or {}
    return CaseHit(
        case_no=meta.get("case_no") or "",
        case_name=sanitize_display_title(meta.get("case_name") or meta.get("title") or ""),
        court=meta.get("court") or "",
        judge_date=_parse_date(meta.get("judge_date")),
        cause_of_action=meta.get("cause_of_action") or "",
        keywords=meta.get("keywords") or [],
        gist=sanitize_evidence_text(meta.get("gist") or ""),
        result=sanitize_evidence_text(meta.get("result") or ""),
        reasoning=sanitize_evidence_text(meta.get("reasoning") or ""),
        issues=meta.get("issues") or [],
        related_statutes=meta.get("related_statutes") or [],
        source=meta.get("source") or "",
        full_text=sanitize_evidence_text(hit.get("text") or ""),
        chunk_id=hit.get("chunk_id") or "",
        source_path=meta.get("source_path") or "",
        score=float(hit.get("rerank_score", hit.get("fusion_score", 0.0))),
        retriever=hit.get("retriever") or "hybrid",
    )


def _hit_to_knowledge(hit: dict[str, Any]) -> KnowledgeHit:
    meta = hit.get("metadata") or {}
    return KnowledgeHit(
        title=sanitize_display_title(meta.get("title") or ""),
        doc_type=meta.get("doc_type") or "",
        version=meta.get("version") or "",
        effective_date=_parse_date(meta.get("effective_date")),
        snippet=sanitize_evidence_text((hit.get("text") or "")[:800]),
        chunk_id=hit.get("chunk_id") or "",
        source_path=meta.get("source_path") or "",
        score=float(hit.get("rerank_score", hit.get("fusion_score", 0.0))),
    )


def _parse_date(value: Any):
    from datetime import date

    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


async def retrieve_statutes(query: str, top_k: int = 6) -> tuple[list[StatuteHit], RetrievalTrace]:
    hits, trace = await search_statutes(query, top_k=top_k)
    return [_hit_to_statute(h) for h in hits], trace


async def retrieve_cases(query: str, top_k: int = 5) -> tuple[list[CaseHit], RetrievalTrace]:
    hits, trace = await search_cases(query, top_k=top_k)
    return [_hit_to_case(h) for h in hits], trace


async def retrieve_knowledge(query: str, top_k: int = 5) -> tuple[list[KnowledgeHit], RetrievalTrace]:
    hits, trace = await search_knowledge(query, top_k=top_k)
    return [_hit_to_knowledge(h) for h in hits], trace


async def retrieve_materials(query: str, case_id: str = "", top_k: int = 8) -> tuple[list[dict[str, Any]], RetrievalTrace]:
    return await search_materials(query, top_k=top_k, case_id=case_id)


async def retrieve_memory(query: str, top_k: int = 5) -> tuple[list[dict[str, Any]], RetrievalTrace]:
    return await search_memory(query, top_k=top_k)


# --------------------------------------------------------------------------- 状态
def retrieval_status() -> dict[str, Any]:
    return {
        "vector": vector_status(),
        "bm25": bm25_status(),
        "reranker": get_reranker().name,
    }


async def embedding_status() -> dict[str, Any]:
    return await embedding_client.status()
