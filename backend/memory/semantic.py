"""语义记忆：Milvus 脱敏历史文书 / 办案经验 / 法规片段。

方案 9.1 / 9.3：仅脱敏数据入库。

与法规库/类案库的区别：语义记忆存的是**本所自己的办案沉淀**（历史文书、办案经验），
因此检索时必须与"公开法规/判例"区分开呈现 —— 否则律师分不清哪句是法条、
哪句是"上次类似案子是这么处理的"。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from backend.app.config import settings
from backend.memory.sanitizer import sanitize
from backend.rag.chunker import chunk_memory

logger = logging.getLogger("legal_agent.memory.semantic")

MEMORY_CORPUS = "memory"


class SemanticMemory:
    """脱敏语义记忆的读写。"""

    # ------------------------------------------------------------------ 写
    async def remember(self, text: str, *, title: str = "", meta: dict[str, Any] | None = None,
                       source_id: str = "") -> dict[str, Any]:
        """写入一条办案经验（**强制脱敏**）。"""
        if not text or len(text.strip()) < 10:
            return {"written": 0, "reason": "内容过短，未入库"}

        clean = sanitize(text, label="semantic")
        tags = [str(t) for t in ((meta or {}).get("tags") or [])]
        if meta and meta.get("memory_type"):
            tags.append(str(meta["memory_type"]))

        chunks = chunk_memory(clean, title=title or "办案经验",
                              source_path=source_id, tags=tags)
        if not chunks:
            return {"written": 0, "reason": "切块后为空"}

        from backend.rag.pipeline import index_chunks

        res = await index_chunks(chunks, MEMORY_CORPUS)
        logger.info("语义记忆写入 %d 块（原文 %d 字 → 脱敏后 %d 字）",
                    res.vector_written, len(text), len(clean))
        return {
            "written": res.vector_written,
            "chunks": len(chunks),
            "degraded": res.degraded,
            "errors": res.errors,
            "anonymized": clean != text,
        }

    # ------------------------------------------------------------------ 读
    async def recall(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """按语义召回历史办案经验。"""
        try:
            from backend.rag.pipeline import retrieve_memory

            hits, trace = await retrieve_memory(query, top_k=top_k)
            if trace.degraded:
                logger.warning("语义记忆召回降级：%s", trace.notes)
            return hits
        except Exception as exc:  # noqa: BLE001
            # 记忆召回失败不能中断主流程：它只是"锦上添花"的上下文
            logger.warning("语义记忆召回失败（不影响主流程）：%s", exc)
            return []

    async def recall_context(self, query: str, top_k: int = 3) -> str:
        """召回结果格式化为可注入 Prompt 的上下文文本。"""
        hits = await self.recall(query, top_k=top_k)
        if not hits:
            return ""
        lines = ["【本所历史办案经验（脱敏，仅供参考，不得作为法律依据引用）】"]
        for i, h in enumerate(hits, 1):
            text = (h.get("text") or "").strip().replace("\n", " ")
            lines.append(f"{i}. {text[:300]}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 维护
    async def forget_source(self, source_id: str) -> dict[str, int]:
        """按来源删除记忆（用于个案撤回 / 合规删除请求）。

        注意：只删除**语义记忆**与短期记忆。**审计日志永不删除**（方案第十四章）。
        """
        from backend.rag.pipeline import remove_source

        out = await remove_source(MEMORY_CORPUS, source_id)
        logger.info("语义记忆来源 %s 已删除：%s", source_id, out)
        return out

    def status(self) -> dict[str, Any]:
        from backend.rag.pipeline import retrieval_status

        st = retrieval_status()
        coll = (st.get("vector") or {}).get("collections") or {}
        return {
            "backend": (st.get("vector") or {}).get("backend"),
            "chunks": coll.get(MEMORY_CORPUS, 0),
            "sanitize_required": True,
            "collection": settings.MILVUS_COLLECTION_MEMORY,
        }


semantic_memory = SemanticMemory()
