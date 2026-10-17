"""类案类 MCP Tool：类案检索 / 判例结构化摘要 / 案号真实性校验。

「案号真实性校验」是 Citation Validator 的核心依赖，也是本项目的生命线：
**编造判例是法律 AI 最大的事故**。

现实约束：公开典型案例常常只有案例名称、没有案号（例如最高法/人社部发布的典型案例），
所以校验采用两条并行通路——
1. 有案号 → 在类案库中按案号精确匹配；
2. 无案号 → 按「案例名称」精确/近似匹配（去掉"某"字脱敏、当事人代称差异）。
两条都不通过 → 判定为不可信引用，Validator 拦截。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from backend.rag.pipeline import retrieve_cases
from backend.rag.retriever.bm25 import get_bm25
from backend.rag.retriever.hybrid import search_cases
from backend.tools.base import ToolResult

logger = logging.getLogger("legal_agent.tools.case")

CASE_NO_RE = re.compile(r"[（(]\s*\d{4}\s*[）)][\u4e00-\u9fa5A-Za-z0-9]{2,30}号")


async def case_search(
    query: str,
    top_k: int = 5,
    *,
    cause_of_action: str = "",
    court_level: str = "",
    keywords: list[str] | None = None,
    domain: str = "",
) -> ToolResult:
    """类案检索：按案由、争议焦点、法院层级。

    Args:
        query: 争议焦点或事实描述。
        top_k: 返回条数。
        cause_of_action: 案由过滤。
        court_level: 法院层级过滤（指导性案例/最高法/高院/中院/基层）。
        keywords: 关键词补强检索。
        domain: 法律领域过滤（跨领域错配治理：如民族事务问题不得召回劳动争议类案）。
    """
    q = query
    if keywords:
        q = q + " " + " ".join(keywords)
    if cause_of_action:
        q = q + " " + cause_of_action

    hits, trace = await search_cases(q, top_k=top_k * 3)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for h in hits:
        meta = h.get("metadata") or {}
        key = meta.get("case_name") or meta.get("case_no") or h.get("chunk_id")
        if key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "case_no": meta.get("case_no") or "",
                "case_name": meta.get("case_name") or "",
                "court": meta.get("court") or "",
                "judge_date": meta.get("judge_date") or "",
                "cause_of_action": meta.get("cause_of_action") or "",
                "keywords": meta.get("keywords") or [],
                "gist": meta.get("gist") or "",
                "result": meta.get("result") or "",
                "reasoning": meta.get("reasoning") or "",
                "related_statutes": meta.get("related_statutes") or [],
                "source": meta.get("source") or "",
                "section": meta.get("section") or "",
                "score": h.get("rerank_score", h.get("fusion_score", 0.0)),
                "source_path": meta.get("source_path") or "",
            }
        )
        if len(items) >= top_k:
            break

    if court_level:
        filtered = [i for i in items if court_level in (i.get("source") or "") or court_level in (i.get("court") or "")]
        items = filtered or items
        if not filtered:
            trace.notes.append(f"法院层级过滤「{court_level}」后无命中，已忽略该过滤条件")

    if domain and domain != "unknown":
        from backend.rag.domain import filter_cases_by_domain

        filtered = filter_cases_by_domain(items, domain)
        if filtered:
            items = filtered
        else:
            # 领域内确实没有判例：不能退回全量（那正是跨领域错配的来源），
            # 如实返回空并登记缺口，让 ReportNode 按「类案证据缺口」降级说明。
            items = []
            trace.notes.append(f"领域过滤「{domain}」后无命中，已按该领域清空类案结果")

    return ToolResult(
        ok=True,
        tool="case_search",
        data={
            "query": query,
            "count": len(items),
            "cases": items,
            "trace": {
                "vector_hits": trace.vector_hits, "bm25_hits": trace.bm25_hits,
                "fused": trace.fused, "elapsed_ms": trace.elapsed_ms, "notes": trace.notes,
            },
        },
        degraded=trace.degraded,
        fallback_note="；".join(trace.notes) if trace.notes else "",
    )


async def case_summarize(text: str = "", *, case_no: str = "", case_name: str = "") -> ToolResult:
    """判例结构化摘要：裁判要旨 / 争议焦点 / 裁判结果。

    先在类案库中定位该判例，再抽取结构化字段；定位不到则从传入文本做规则抽取，
    并标注为「未在类案库中命中」。
    """
    target = ""
    if case_no or case_name:
        found = await case_no_verify(case_no=case_no, case_name=case_name)
        data = found.data or {}
        if data.get("exists"):
            target = data.get("matched_text") or ""
            meta = data.get("matched_metadata") or {}
            return ToolResult(
                ok=True, tool="case_summarize",
                data={
                    "found": True,
                    "case_no": meta.get("case_no") or case_no,
                    "case_name": meta.get("case_name") or case_name,
                    "gist": meta.get("gist") or "",
                    "result": meta.get("result") or "",
                    "reasoning": meta.get("reasoning") or "",
                    "keywords": meta.get("keywords") or [],
                    "related_statutes": meta.get("related_statutes") or [],
                    "source": meta.get("source") or "",
                },
                degraded=found.degraded, fallback_note=found.fallback_note,
            )

    source_text = text or target
    if not source_text:
        return ToolResult(
            ok=False, tool="case_summarize",
            error="未提供判例文本，且类案库中未命中该判例",
            degraded=True, fallback_note="判例摘要不可用",
        )

    extracted = _rule_summarize(source_text)
    extracted["found"] = False
    return ToolResult(
        ok=True, tool="case_summarize", data=extracted,
        degraded=True,
        fallback_note="未在类案库中命中该判例，摘要为从传入文本的规则抽取结果",
    )


def _rule_summarize(text: str) -> dict[str, Any]:
    def grab(labels: list[str], limit: int = 400) -> str:
        for label in labels:
            m = re.search(rf"{label}[：:]\s*(.{{2,{limit}}}?)(?=\n\S{{0,8}}[：:]|\Z)", text, re.S)
            if m:
                return re.sub(r"\s+", " ", m.group(1)).strip()
        return ""

    gist = grab(["裁判要点", "裁判要旨", "本院认为"])
    result = grab(["裁判结果", "判决如下", "裁定如下"])
    issues = re.findall(r"(?:争议焦点|焦点)[：:]\s*([^\n]{4,80})", text)
    return {
        "gist": gist[:600],
        "result": result[:400],
        "reasoning": grab(["裁判理由"])[:600],
        "issues": issues[:6],
        "keywords": re.findall(r"(?:关键词)[：:]\s*([^\n]{2,60})", text)[:1],
    }


async def case_no_verify(case_no: str = "", *, case_name: str = "") -> ToolResult:
    """案号 / 案例名称存在性校验。

    这是「无来源不出引用」的落地实现：
    - 在类案库中按案号精确匹配；
    - 无案号时按案例名称匹配（容忍"某"字脱敏与常见前后缀差异）；
    - 均未命中 → ``exists=False``，Validator 会拦截该引用。
    """
    idx = get_bm25("case")
    if len(idx) == 0:
        return ToolResult(
            ok=False, tool="case_no_verify",
            error="类案库为空，无法校验案号真实性",
            degraded=True,
            fallback_note="类案库未入库，案号存在性校验不可用——按引用纪律，此时不得输出任何案号",
        )

    key = (case_no or case_name or "").strip()
    if not key:
        return ToolResult(ok=False, tool="case_no_verify", error="未提供案号或案例名称")

    # 1) 精确匹配（BM25 精确 token 命中）
    hits = idx.search(key, top_k=20)
    exact: dict[str, Any] | None = None
    norm_key = _norm_case_name(key)
    for h in hits:
        meta = h.get("metadata") or {}
        stored_no = (meta.get("case_no") or "").strip()
        stored_name = (meta.get("case_name") or "").strip()
        if case_no and stored_no and _norm_case_no(stored_no) == _norm_case_no(case_no):
            exact = {"meta": meta, "text": h.get("text", ""), "how": "案号精确匹配"}
            break
        if not case_no and stored_name and _norm_case_name(stored_name) == norm_key:
            exact = {"meta": meta, "text": h.get("text", ""), "how": "案例名称精确匹配"}
            break

    # 2) 近似匹配（名称相似度）
    if exact is None and case_name:
        for h in hits:
            meta = h.get("metadata") or {}
            stored_name = (meta.get("case_name") or "").strip()
            if stored_name and _similar(stored_name, case_name) >= 0.72:
                exact = {"meta": meta, "text": h.get("text", ""), "how": f"案例名称近似匹配（相似度≥0.72）"}
                break

    if exact is None:
        return ToolResult(
            ok=True, tool="case_no_verify",
            data={
                "exists": False, "key": key,
                "reason": "类案库中未找到该案号/案例名称，判定为不可信引用",
                "candidates": [
                    {
                        "case_no": (h.get("metadata") or {}).get("case_no") or "",
                        "case_name": (h.get("metadata") or {}).get("case_name") or "",
                    }
                    for h in hits[:3]
                ],
            },
            degraded=True,
            fallback_note="案号/案例名称未通过存在性校验",
        )

    meta = exact["meta"]
    return ToolResult(
        ok=True, tool="case_no_verify",
        data={
            "exists": True,
            "key": key,
            "how": exact["how"],
            "case_no": meta.get("case_no") or "",
            "case_name": meta.get("case_name") or "",
            "court": meta.get("court") or "",
            "judge_date": meta.get("judge_date") or "",
            "source": meta.get("source") or "",
            "source_path": meta.get("source_path") or "",
            "matched_text": exact["text"],
            "matched_metadata": meta,
        },
    )


def _norm_case_no(value: str) -> str:
    return re.sub(r"[\s（）()]", "", value or "")


def _norm_case_name(value: str) -> str:
    """名称归一：去空格/标点、「某」字（脱敏代称）、案字后缀差异。"""
    v = re.sub(r"[\s，,。、（）()《》]", "", value or "")
    v = v.replace("某", "")
    v = re.sub(r"(纠纷|争议)?案$", "", v)
    return v


def _similar(a: str, b: str) -> float:
    from difflib import SequenceMatcher

    return SequenceMatcher(None, _norm_case_name(a), _norm_case_name(b)).ratio()
