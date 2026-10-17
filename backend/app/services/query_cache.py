"""相似问题缓存：同一个法律问题问第二遍时，直接复用已核验过的结论。

为什么是"语义"缓存
------------------
按 query 字符串做精确缓存命中率极低（"公司不签合同怎么办" 与
"用人单位未签书面劳动合同，劳动者可以主张哪些权利"是同一个问题，
字符串却完全不同）。所以这里存的是**问题向量**，命中判据是余弦相似度。

三条纪律（决定这个功能是不是"另一个造轮子的坑"）
------------------------------------------------
1. **只缓存值得复用的结论。** 引用终审没过（``citation_check != passed``）
   或置信度低于阈值的任务不入库 —— 缓存一条没核验过的结论，等于让错误
   结论在下一次问到时**自动重发**。
2. **带材料的提问必须材料一致才命中。** 合同审查的答案取决于那份合同，
   只看提问相似度会把 A 合同的结论套到 B 合同上。所以额外比对材料指纹，
   指纹不同直接不命中（宁可重跑）。
3. **缓存的是指针，不是副本。** 向量库里只存 ``task_id`` + 元数据，
   结论本体仍从 ``la_task`` 取 —— 结论只有一份真相，不会出现
   "缓存里的旧版本和任务表里的新版本不一致"。任务被删=缓存自动失效。

命中后**必须声明**：结果里带 ``cache_hit``（原任务 id / 相似度 / 原始提问），
Trace 里也留一条，让律师知道这份结论是哪来的、多久之前的。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from typing import Any

from backend.app.config import settings

logger = logging.getLogger("legal_agent.services.query_cache")

# 独立集合：与 la_memory（办案经验）、la_statute（法规）分开。
# 混在一起的后果是"检索历史问答"会把缓存条目当证据召回，
# 而它是**上一轮的结论**，不是可引用的依据。
COLLECTION = "qcache"

_WS = re.compile(r"\s+")
_PUNCT = re.compile("[，。！？、；：“”‘’（）()\\[\\]《》\\s]+")


def _norm(text: str) -> str:
    return _WS.sub("", _PUNCT.sub("", text or "")).lower()


def fingerprint(text: str) -> str:
    """文本指纹（去标点空白后哈希）：判断"是不是同一份材料"。"""
    return hashlib.blake2b(_norm(text).encode("utf-8"), digest_size=8).hexdigest()


def _chunk_id(query: str, task_id: str) -> str:
    return "qc_" + hashlib.blake2b(f"{_norm(query)}|{task_id}".encode("utf-8"),
                                   digest_size=10).hexdigest()


def enabled() -> bool:
    return bool(getattr(settings, "QUERY_CACHE_ENABLED", False))


async def _usable() -> bool:
    """embedding 是否真的可用（**hash 兜底不算**）。

    hash 向量的"相似度"与语义无关（实测同义改写只有 0.27），
    拿它做缓存命中判据会把无关问题当成同一个问题 ——
    这比不缓存更危险：律师会拿到一份**别人的、错误的**结论，
    而界面上写的是"命中缓存"。所以检测到降级时直接停用缓存。
    """
    if not enabled():
        return False
    try:
        from backend.rag.embedding import embedding_client

        st = await embedding_client.status()
    except Exception:  # noqa: BLE001
        return False
    if st.get("degraded") or st.get("backend") == "hash":
        logger.warning("embedding 已降级为 hash，语义缓存停用（相似度不可信）")
        return False
    return True


# --------------------------------------------------------------------------- 写
async def store(query: str, *, task_id: str, intent: str = "", material_text: str = "",
                confidence: float = 0.0, citation_check: str = "") -> bool:
    """把一个已完成的结论登记进缓存。调用方负责筛选"值不值得缓存"。"""
    if not query.strip() or not task_id or not await _usable():
        return False
    try:
        from backend.rag.embedding import embedding_client
        from backend.rag.retriever.vector import get_vector_store

        vec = await embedding_client.encode_one(query[:2000], is_query=True)
        if not vec:
            return False
        payload = {
            "task_id": task_id,
            "query": query[:500],
            "intent": intent,
            "created_at": time.time(),
            "material_fp": fingerprint(material_text) if material_text else "",
            "confidence": round(float(confidence or 0.0), 3),
            "citation_check": citation_check or "",
        }
        item = {
            "chunk_id": _chunk_id(query, task_id),
            "vector": vec,
            "text": query[:2000],
            "corpus": COLLECTION,
            "metadata": payload,
        }
        # upsert 是同步（pymilvus），放进线程避免拖住事件循环
        await asyncio.to_thread(get_vector_store().upsert, COLLECTION, [item])
        logger.info("已缓存问题结论：task=%s intent=%s 相似度阈值=%.2f",
                    task_id, intent, float(settings.QUERY_CACHE_MIN_SIM))
        return True
    except Exception as exc:  # noqa: BLE001
        # 缓存写失败绝不能影响交付物：它只是"下次更快"的优化
        logger.warning("写入问题缓存失败（不影响本次结果）：%s", exc)
        return False


# --------------------------------------------------------------------------- 读
async def lookup(query: str, *, intent: str = "", material_text: str = "",
                 top_k: int | None = None) -> dict[str, Any] | None:
    """找有没有**足够相似**的历史问题可以复用。

    返回 ``None`` 表示没命中（调用方照常跑完整链路）；命中时返回
    ``{"task_id", "similarity", "cached_query", "created_at", "result", "intent"}``。
    """
    if not query.strip() or not await _usable():
        return None
    try:
        from backend.rag.embedding import embedding_client
        from backend.rag.retriever.vector import get_vector_store

        vec = await embedding_client.encode_one(query[:2000], is_query=True)
        if not vec:
            return None
        hits = await asyncio.to_thread(
            get_vector_store().search, COLLECTION, vec, int(top_k or settings.QUERY_CACHE_TOP_K)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("查询问题缓存失败（按未命中处理）：%s", exc)
        return None

    threshold = float(getattr(settings, "QUERY_CACHE_MIN_SIM", 0.90))
    ttl = float(getattr(settings, "QUERY_CACHE_TTL_DAYS", 30)) * 86400
    fp = fingerprint(material_text) if material_text else ""

    candidates: list[dict[str, Any]] = []
    for h in hits or []:
        meta = h.get("metadata") or {}
        task_id = str(meta.get("task_id") or "")
        score = float(h.get("score") or 0.0)
        if not task_id or score < threshold:
            continue
        if ttl and time.time() - float(meta.get("created_at") or 0) > ttl:
            logger.info("缓存条目已过期，跳过：task=%s", task_id)
            continue
        # 材料不一致 ⇒ 不是同一个问题（合同审查尤其致命）
        if fp != (meta.get("material_fp") or ""):
            continue
        cached_intent = str(meta.get("intent") or "")
        if cached_intent and intent and cached_intent != intent:
            # 意图不同的相似提问，结论大概率不可套用（例："合同有效性" vs "违约责任"）
            logger.info("相似但意图不同，不复用：%s(%s) vs 当前 %s", task_id, cached_intent, intent)
            continue
        candidates.append({
            "task_id": task_id,
            "similarity": round(score, 4),
            "cached_query": str(meta.get("query") or ""),
            "cached_intent": cached_intent,
            "created_at": float(meta.get("created_at") or 0),
        })

    # **同类里取最新的一条**，而不是分数最高的那条。
    #
    # 走到这里的候选相似度都在阈值之上（0.90），彼此已经是"同一个问题"，
    # 再按分数排名没有意义；而按分数排会出现这个死角：律师对复用的结论有疑问、
    # 用 no_cache 强制重跑得到新结论并入库后，下次提问仍可能命中**被推翻的旧条目**
    # —— 强制重跑就白做了，而且他再也绕不过去。
    # 缓存存的是"上一次的结论"，那最新一次才该是它。
    candidates.sort(key=lambda c: c["created_at"], reverse=True)

    for c in candidates:
        row = _load_task(c["task_id"])
        if row is None:
            continue
        result = row.get("result") or {}
        if not (result.get("report") or result.get("draft")):
            continue
        return {**c, "result": result}
    return None


# 可以复用的任务状态。**``degraded`` 必须在列**：
# 它的含义是"降级完成"——交付物是完整的，只是带着证据缺口声明
# （``evals/metrics.py`` 单独统计它、并明写"降级完成是可用交付物，不是失败"）。
# 而 ``harness._status_of`` 的判据是"有证据缺口就记 degraded"，
# 也就是说**任何一次有缺口的任务都会被记成 degraded** ——
# 漏掉它，缓存就会写成却永远读不出来：入库日志一切正常，
# 换个说法再问还是重跑一遍，而"相似问题直接命中缓存"正是这么失效的。
REUSABLE_STATUS = ("succeeded", "finished", "done", "degraded")


def _load_task(task_id: str) -> dict[str, Any] | None:
    """取回原任务。失败或还在跑的任务没有可交付结论，复用它会把半成品当答案发出去。"""
    try:
        from backend.app.services.task_service import task_service

        row = task_service.get(task_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取缓存任务失败：%s", exc)
        return None
    if not row:
        return None
    if str(row.get("status") or "") not in REUSABLE_STATUS:
        return None
    return row


# --------------------------------------------------------------------------- 运维
async def stats() -> dict[str, Any]:
    try:
        from backend.rag.retriever.vector import get_vector_store

        store = get_vector_store()
        count = await asyncio.to_thread(store.count, COLLECTION)
        return {"enabled": enabled(), "collection": COLLECTION, "entries": count,
                "min_similarity": float(getattr(settings, "QUERY_CACHE_MIN_SIM", 0.90)),
                "ttl_days": int(getattr(settings, "QUERY_CACHE_TTL_DAYS", 30))}
    except Exception as exc:  # noqa: BLE001
        return {"enabled": enabled(), "error": str(exc)}


async def purge() -> dict[str, Any]:
    """清空缓存（换 embedding 模型后向量维度变了必须清，否则检索报维度不匹配）。"""
    try:
        from backend.rag.retriever.vector import get_vector_store

        store = get_vector_store()
        await asyncio.to_thread(store.drop, COLLECTION)
        return {"purged": True}
    except Exception as exc:  # noqa: BLE001
        return {"purged": False, "error": str(exc)}
