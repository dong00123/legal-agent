"""Rerank：对融合召回结果做精排。

法律场景的精排不是纯粹的语义相似度问题，还要考虑：
- 命中是否落在**同一部法规的同一个条文**（条文号精确命中应显著加分）；
- 法条是否**现行有效**（过时依据比没有依据更危险，必须降权）；
- 案号是否**精确命中**（类案检索里案号命中是强信号）。

所以默认使用领域启发式精排（无需额外模型、可解释），
若本地存在 CrossEncoder 重排模型则优先使用（``bge-reranker``）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.app.config import PROJECT_ROOT, settings

logger = logging.getLogger("legal_agent.rag.rerank")

ARTICLE_RE = re.compile(r"第\s*[一二三四五六七八九十百零两\d]+\s*条")
CASE_NO_RE = re.compile(r"[（(]\s*\d{4}\s*[）)][\u4e00-\u9fa5A-Za-z0-9]{2,30}号")
LAW_NAME_RE = re.compile(r"《[^》]{2,40}》")

STATUS_WEIGHT = {"现行有效": 1.0, "尚未生效": 0.7, "已修订": 0.55, "已废止": 0.15, "未知": 0.5}

DEFAULT_RERANKER_MODEL_DIRS = [
    PROJECT_ROOT / "data" / "models" / "bge-reranker-base",
    PROJECT_ROOT / "data" / "models" / "bge-reranker-v2-m3",
]


@dataclass
class RerankSignals:
    semantic: float = 0.0
    lexical: float = 0.0
    exact: float = 0.0
    status: float = 0.0
    detail: dict[str, Any] = None  # type: ignore[assignment]


class BaseReranker:
    name = "base"

    def rerank(self, query: str, docs: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:  # pragma: no cover
        raise NotImplementedError


class HeuristicReranker(BaseReranker):
    """领域启发式精排：融合分 + 精确命中 + 效力状态。"""

    name = "heuristic"

    def rerank(self, query: str, docs: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        if not docs:
            return []
        q_articles = {a.replace(" ", "") for a in ARTICLE_RE.findall(query)}
        q_cases = {c.replace(" ", "") for c in CASE_NO_RE.findall(query)}
        q_laws = {n.strip("《》") for n in LAW_NAME_RE.findall(query)}

        max_fusion = max((d.get("fusion_score", 0.0) for d in docs), default=1.0) or 1.0
        max_lexical = max((d.get("bm25_score", 0.0) for d in docs), default=1.0) or 1.0

        for d in docs:
            meta = d.get("metadata") or {}
            signals = RerankSignals()

            signals.semantic = float(d.get("vector_score", 0.0))
            signals.lexical = float(d.get("bm25_score", 0.0)) / max_lexical
            signals.detail = {}

            # 精确命中：条文号 / 案号 / 法规名
            exact = 0.0
            if q_articles:
                doc_article = (meta.get("article_no") or "").replace(" ", "")
                if doc_article and doc_article in q_articles:
                    exact += 0.6
                    signals.detail["article_match"] = doc_article
                elif any(a in d.get("text", "") for a in q_articles):
                    exact += 0.2
            if q_cases:
                case_no = (meta.get("case_no") or "").replace(" ", "")
                if case_no and case_no in q_cases:
                    exact += 0.8
                    signals.detail["case_match"] = case_no
                elif any(c in d.get("text", "") for c in q_cases):
                    exact += 0.25
            if q_laws:
                doc_name = (meta.get("doc_name") or "").strip("《》")
                if doc_name and any(law in doc_name or doc_name in law for law in q_laws):
                    exact += 0.35
                    signals.detail["law_match"] = doc_name
            signals.exact = min(exact, 1.0)

            # 效力状态：过时依据降权
            status = meta.get("status") or ""
            signals.status = STATUS_WEIGHT.get(status, 0.5) if status else 0.5

            # 融合分归一化
            fusion = float(d.get("fusion_score", 0.0)) / max_fusion

            score = (
                0.40 * fusion
                + 0.20 * signals.lexical
                + 0.25 * signals.exact
                + 0.15 * signals.status
            )
            d["rerank_score"] = round(score, 6)
            d["rerank_detail"] = {
                "fusion": round(fusion, 4),
                "lexical": round(signals.lexical, 4),
                "exact": round(signals.exact, 4),
                "status": round(signals.status, 4),
                **signals.detail,
            }
            d["retriever"] = "hybrid+rerank"

        docs.sort(key=lambda x: -x.get("rerank_score", 0.0))
        return docs[:top_k]


class CrossEncoderReranker(BaseReranker):
    """CrossEncoder 精排（本地存在 bge-reranker 时启用）。"""

    name = "cross-encoder"

    def __init__(self, model_path: str) -> None:
        from sentence_transformers import CrossEncoder

        self.model = CrossEncoder(model_path, max_length=512)
        self.model_path = model_path

    def rerank(self, query: str, docs: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        if not docs:
            return []
        pairs = [(query, d.get("text", "")) for d in docs]
        scores = self.model.predict(pairs)
        for d, s in zip(docs, scores):
            meta = d.get("metadata") or {}
            status = meta.get("status") or ""
            status_w = STATUS_WEIGHT.get(status, 0.5) if status else 0.5
            # CrossEncoder 原始分与效力状态加权
            d["rerank_score"] = float(s) * (0.85 + 0.15 * status_w)
            d["rerank_detail"] = {"cross_encoder": round(float(s), 4), "status": status_w}
            d["retriever"] = "hybrid+rerank(ce)"
        docs.sort(key=lambda x: -x.get("rerank_score", 0.0))
        return docs[:top_k]


_reranker: BaseReranker | None = None


def get_reranker() -> BaseReranker:
    global _reranker
    if _reranker is not None:
        return _reranker
    configured_path = settings.RERANKER_LOCAL_MODEL_PATH.strip()
    model_dirs = [Path(configured_path)] if configured_path else DEFAULT_RERANKER_MODEL_DIRS
    for d in model_dirs:
        if d.exists():
            try:
                _reranker = CrossEncoderReranker(str(d))
                logger.info("Reranker 已启用：CrossEncoder（%s）", d.name)
                return _reranker
            except Exception as exc:  # noqa: BLE001
                logger.info("CrossEncoder 加载失败（%s）：%s", d, exc)
    if configured_path and not Path(configured_path).exists():
        logger.warning("未找到本地 reranker 模型目录：%s", configured_path)
    _reranker = HeuristicReranker()
    logger.info("Reranker 已启用：领域启发式精排")
    return _reranker
