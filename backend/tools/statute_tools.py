"""法规类 MCP Tool：法条检索 / 时效性校验 / 版本比对。

这三个工具是「时效性校验」红线的执行体：检索结果必须带效力状态，
校验不通过（已废止）的法条会在 Validator 阶段被拦截。
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date
from typing import Any

from backend.app.db.models import StatuteDoc
from backend.app.db.session import session_scope
from backend.rag.domain import filter_statutes_by_domain, statute_domain_keywords
from backend.rag.pipeline import hit_to_statute, retrieve_statutes
from backend.tools.base import ToolResult

logger = logging.getLogger("legal_agent.tools.statute")

ARTICLE_NORM_RE = re.compile(r"第\s*([一二三四五六七八九十百零两\d]+)\s*条")

# 检索式里的精确条文要求：《劳动合同法》第二十四条 / 劳动合同法第24条
# 名称部分刻意要求以法律体裁词收尾 —— 否则「因本合同发生争议，适用第八条」
# 这类正文片段会被当成法规名，凭空构造出一个不存在的精确检索要求。
#
# 名称中段用 {1,28}? 而不是 {2,30}?：两字体裁后缀（「法典」）需要给前缀留出
# 至少 1 个字符的位置，写成 {2,} 会让「民法典」整体至少 4 字而永远匹配不上 ——
# 表现是《民法典》第五百八十五条被当成"没有条文要求"，退回语义召回碰运气。
EXACT_CITE_RE = re.compile(
    r"《?([\u4e00-\u9fa5A-Za-z]{1,28}?"
    r"(?:法|条例|规定|解释|规则|办法|细则|法典))》?\s*"
    r"第\s*([一二三四五六七八九十百零两\d]+)\s*条"
)

# 法规名前面粘上的介词/连词（「根据劳动合同法第八十七条」→「劳动合同法」）。
# 不剥的话，逐字匹配会得到「根据劳动合同法」这个不存在的法规名，
# 精确检索必然落空 —— 而落空是静默的：退回语义检索，上层看不出差别。
# 单字与双字的介词/连词混在一起时，**必须按长度倒序**再剥：
# 正序遇到「依」会先把「依据」剥成「据」，之后再也剥不掉，
# 于是「依据劳动合同法」算出法规名「据劳动合同法」—— 精确检索必然落空，
# 而落空是静默的（退回语义检索，上层看不出差别）。
_LAW_LEAD_WORDS = ("根据", "依据", "依照", "按照", "参照", "遵照", "关于", "适用",
                   "以及", "并且", "对于", "和", "与", "及", "或", "的", "该", "本",
                   "在", "依", "据", "按", "以", "对")
_LAW_LEAD_SORTED = tuple(sorted(_LAW_LEAD_WORDS, key=len, reverse=True))

# 以回指词开头的"法规名"：指不出具体是哪一部法，精确检索必然落空。
# 这类片段会被 ``_strip_law_lead`` 加工得更像样 ——「依据该法」剥成「该法」，
# 长度刚好够 2 字，末尾又正好是体裁词「法」，光看结构一律放行。
# 于是「按照上述规定第5条」会凭空造出一个不存在的精确检索要求《上述规定》第5条，
# 检索整条跑偏，而上层只看到"条文没定位到"。
_VAGUE_LAW_PREFIX = ("上述", "前述", "该", "本", "此", "相关", "有关", "以上", "如下",
                     "其他", "其它", "涉案", "双方", "当事人", "约定", "所涉", "相应")


def _strip_law_lead(name: str) -> str:
    """剥掉法规名前面粘上的介词/连词；剥完不足 2 字则原样返回。"""
    out = (name or "").strip()
    for _ in range(4):
        hit = next((w for w in _LAW_LEAD_SORTED
                    if out.startswith(w) and len(out) > len(w) + 1), "")
        if not hit:
            break
        out = out[len(hit):]
    return out if len(out) >= 2 else (name or "").strip()


def _looks_like_law_name(name: str) -> bool:
    """剥掉介词之后的名字是否真的是法规名（而不是行文回指）。"""
    text = (name or "").strip()
    if len(text) < 2:
        return False
    return not any(text.startswith(p) for p in _VAGUE_LAW_PREFIX)


def parse_exact_cites(query: str) -> list[tuple[str, str]]:
    """从检索式里解析「《法规名》第X条」形态的精确条文要求，去重保序。

    这是 docstring 里承诺过、此前没有实现的能力：检索式写成条文号时应当
    **精确定位**，而不是把它当成普通关键词丢进语义检索碰运气。
    对合同审查尤其关键 —— 规则表声明的依据是确定的条文，
    靠相似度召回"最像的条文"会让每条风险点的依据都落空。
    """
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for m in EXACT_CITE_RE.finditer(query or ""):
        law = _strip_law_lead(m.group(1))
        if not _looks_like_law_name(law):
            logger.debug("忽略疑似回指的法规名：%r", law)
            continue
        art = normalize_article(m.group(2))
        if not art or (law, art) in seen:
            continue
        seen.add((law, art))
        out.append((law, art))
    return out


def _law_name_eq(a: str, b: str) -> bool:
    """法规名**严格相等**（剥书名号、去「中华人民共和国」前缀后比）。

    刻意不做子串匹配：「劳动合同法」与「劳动法」是两部不同的法律，
    而「劳动法」是「劳动合同法」的子串。放行子串匹配会让
    「《劳动法》第二十四条」（关于工作时间的条款）冒充
    「《劳动合同法》第二十四条」（竞业限制条款）钉进证据池，
    并顶着 ``source=exact`` 的高可信标记被规则引擎采信 —— 是最糟的一类错误：
    错得毫无痕迹。宁可精确匹配不上就退回普通检索。
    """
    def _plain(name: str) -> str:
        cleaned = re.sub(r"[《》\s　]", "", name or "")
        for prefix in ("中华人民共和国", "最高人民法院"):
            if cleaned.startswith(prefix):
                return cleaned[len(prefix):]
        return cleaned

    pa, pb = _plain(a), _plain(b)
    return bool(pa) and pa == pb


def _probe_forms(law: str, article_no: str) -> list[str]:
    """精确条文定位的检索式候选，按优先级排列。

    **两种写法都要试**：BM25 是字面检索，而法条正文里的条文号一律是中文数字
    （「第五百八十五条」）。库里不存在 ``585`` 这个 token，拿阿拉伯形去搜会返回
    **0 条** —— 不是"排得靠后"，是彻底空手而归，于是精确条文定位静默落空，
    风险点依据只能退回语义召回碰运气。这是"看着在工作、实际从未生效"的典型。
    """
    from backend.parser.structure import article_no_cn

    forms: list[str] = []
    # **中文数字形态排在最前**：法规正文里条文号一律是中文数字，先试这个形态
    # 能一次打中；排在后面时每次都要先白跑一次必然落空的检索。
    for art in (article_no_cn(article_no), article_no):
        if not art:
            continue
        q = f"{law} {art}".strip()
        if q not in forms:
            forms.append(q)
    # 兜底：只给法规名。正文多以「《法规名》第X条」开头，靠名字也能召回一批候选，
    # 再在 Python 侧按归一键复核 —— 用一点算力换"宁可慢也别落空"。
    if law and law not in forms:
        forms.append(law)
    return forms


def _exact_article(law: str, article_no: str) -> dict[str, Any] | None:
    """在法规库里精确定位一条条文；找不到返回 None。

    走 BM25 而不是向量：精确条文要求的是**字面命中**，
    向量相似度在这里只会引入"名字很像的另一条"。
    取回后在 Python 侧用归一后的键复核，确保检索器无论换了哪个后端都一致。
    """
    from backend.parser.structure import article_key
    from backend.rag.retriever.bm25 import get_bm25

    want = article_key(article_no)
    if not want:
        return None
    try:
        idx = get_bm25("statute")
    except Exception as exc:  # noqa: BLE001
        logger.warning("条文精确定位不可用（%s %s）：%s", law, article_no, exc)
        return None

    for probe in _probe_forms(law, article_no):
        try:
            rows = idx.search(probe, 400)
        except Exception as exc:  # noqa: BLE001
            logger.warning("条文精确定位检索失败（%s）：%s", probe, exc)
            continue
        if not rows:
            continue
        for row in rows:
            md = row.get("metadata") or {}
            if article_key(md.get("article_no") or "") != want:
                continue
            if not _law_name_eq(law, md.get("doc_name") or ""):
                continue
            out = dict(row)
            out["score"] = 1.0
            out["rerank_score"] = 1.0      # hit_to_statute 取的是这个键
            out["retriever"] = "exact"     # 出处可辨识：精确命中 vs 语义召回
            out["exact"] = True
            return out
    return None


def _merge_exact(pinned: list[dict[str, Any]], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把精确命中的条文钉到结果最前，并按 chunk 去重。

    钉到最前而不是"参与排序"：后续 Rerank 与融合排序都可能把它挤下去，
    而它是**明确要求**的条文，不是候选之一。
    """
    out: list[dict[str, Any]] = list(pinned)
    seen = {p.get("chunk_id") for p in pinned}
    for it in items:
        if it.get("chunk_id") in seen:
            continue
        seen.add(it.get("chunk_id"))
        out.append(it)
    return out


def normalize_article(article_no: str) -> str:
    """条文号规范化：统一「第X条」形式并把中文数字转成阿拉伯数字。

    委托 ``parser.structure.normalize_article_no``，保证入库、检索、终审
    三处用的是同一套比较键 —— 分开实现过一次，代价是
    「第八十七条」与「第87条」被当成两个不同条文，正确的引用被拦。
    """
    from backend.parser.structure import normalize_article_no

    return normalize_article_no(article_no) or article_no.strip()


async def statute_search(
    query: str,
    top_k: int = 6,
    *,
    category: str = "",
    level: str = "",
    only_valid: bool = False,
    domain: str = "",
) -> ToolResult:
    """法条检索：按关键词 / 条文号 / 效力层级 / 法律领域。

    两种检索意图走**两条**通道，结果合并：

    - 检索式里写了条文号（「《劳动合同法》第八十七条」）→ 先做**精确条文定位**，
      钉在结果最前。这是 docstring 一直承诺、此前未实现的能力；
    - 其余部分交给 Hybrid 检索（向量 + BM25 + RRF + Rerank）做语义召回。

    为什么必须分两条：精确条文要求的是"就是要这一条"，语义召回给的是
    "看起来最像的几条"。混成一条通道时，规则表声明要引第五百八十五条，
    召回来的却是第五百九十二条，于是每条风险点的依据都落空 ——
    这不是检索质量差一点的问题，是"无来源不出引用"红线整条空转。

    Args:
        query: 检索式。支持关键词（"违法解除劳动合同"）与条文号（"《劳动合同法》第八十七条"）。
        top_k: 返回条数。
        category: 业务领域过滤（劳动法 / 知识产权法 / 民法典）。
        level: 效力层级过滤（法律 / 行政法规 / 司法解释 / 地方性法规）。
        only_valid: 仅返回现行有效（过滤已废止/已修订）。
        domain: 法律领域过滤（labor/tax/env/civil/unknown）。
            **跨领域错配治理**：语义召回的命中按领域词表（doc_name/source_path）
            过滤，劳动问题不再召回环保税法/民法典；``exact`` 精确命中的条文是
            用户点名要求，**不参与领域过滤**，避免点名条文被误滤。
            unknown/空值不做过滤（退回改造前行为）。
    """
    exact = parse_exact_cites(query)
    widened = bool(category or level or only_valid or domain)
    _t_start = time.perf_counter()
    hits, trace = await retrieve_statutes(query, top_k=top_k * 3 if widened else top_k)
    _t_retrieve = time.perf_counter()
    logger.info("statute_search 混合检索耗时 %dms（top_k=%d, widened=%s）",
                int((_t_retrieve - _t_start) * 1000), top_k, widened)
    items = [h.model_dump(mode="json") for h in hits]

    pinned: list[dict[str, Any]] = []
    for law, art in exact:
        row = _exact_article(law, art)
        if row is None:
            # 明确要求了某条文却定位不到：必须声张。静默退回语义召回的结果，
            # 会让上层以为"要的那条已经拿到了"。
            trace.notes.append(f"法规库中未定位到《{law}》{art}，该条文未纳入证据")
            trace.degraded = True
            continue
        pinned.append(hit_to_statute(row).model_dump(mode="json"))
    if exact:
        trace.notes.append(f"精确条文要求 {len(exact)} 条，命中 {len(pinned)} 条")
        logger.info("法规检索精确条文：要求 %s，命中 %s", exact, len(pinned))

    items = _merge_exact(pinned, items)

    if category:
        items = [i for i in items if category in (i.get("source_path") or "") or category in (i.get("doc_name") or "")]
    if domain:
        # 精确命中的条文是用户点名要求，不参与领域过滤；
        # 语义召回部分按领域词表滤除跨领域错配（如劳动问题召回环保税法）。
        kept_pinned = [i for i in items if i.get("retriever") == "exact"]
        rest = filter_statutes_by_domain(
            [i for i in items if i.get("retriever") != "exact"], domain)
        before = len(items)
        items = kept_pinned + rest
        semantic = before - len(kept_pinned)
        trace.notes.append(
            f"按领域「{domain}」过滤：语义召回 {semantic} 条中保留 {len(rest)} 条，"
            f"滤除 {semantic - len(rest)} 条跨领域错配")
    if level:
        items = [i for i in items if _level_of(i.get("doc_name") or "") == level]
    if only_valid:
        items = [i for i in items if (i.get("status") or "") == "现行有效"]

    items = items[:top_k]
    for i in items:
        i["article_no"] = normalize_article(i.get("article_no") or "")

    return ToolResult(
        ok=True,
        tool="statute_search",
        data={
            "query": query,
            "count": len(items),
            "statutes": items,
            "exact_requests": [f"{law}{art}" for law, art in exact],
            "exact_hits": [f"{i.get('doc_name')}{i.get('article_no')}" for i in items
                           if i.get("retriever") == "exact"],
            "domain": domain or "unknown",
            "domain_keywords": list(statute_domain_keywords(domain)),
            "trace": {
                "vector_hits": trace.vector_hits,
                "bm25_hits": trace.bm25_hits,
                "fused": trace.fused,
                "elapsed_ms": trace.elapsed_ms,
                "degraded": trace.degraded,
                "notes": trace.notes,
            },
        },
        degraded=trace.degraded,
        fallback_note="；".join(trace.notes),
    )


_LEVEL_CACHE: dict[str, str] = {}


def _level_of(doc_name: str) -> str:
    if doc_name in _LEVEL_CACHE:
        return _LEVEL_CACHE[doc_name]
    level = ""
    with session_scope() as db:
        row = db.query(StatuteDoc).filter(StatuteDoc.doc_name == doc_name).one_or_none()
        if row:
            level = row.level or ""
    # DB 没记录时按名称推断，保证过滤不至于静默失效
    if not level:
        plain = doc_name.strip("《》")
        if "司法解释" in plain or "解释" in plain:
            level = "司法解释"
        elif any(k in plain for k in ("条例", "规定", "办法", "细则")):
            level = "行政法规"
        elif "法" in plain:
            level = "法律"
    _LEVEL_CACHE[doc_name] = level
    return level


async def statute_validity_check(items: list[dict[str, Any]] | None = None, *, doc_name: str = "", article_no: str = "") -> ToolResult:
    """法规时效性校验：现行有效 / 已修订 / 已废止 / 尚未生效。

    两种调用方式：
    - 传 ``items``（检索结果），批量校验；
    - 传 ``doc_name`` + ``article_no``，单条校验。
    """
    results: list[dict[str, Any]] = []

    if doc_name:
        results.append(_check_one({"doc_name": doc_name, "article_no": normalize_article(article_no)}))
    for it in items or []:
        results.append(_check_one(it))

    ok_count = sum(1 for r in results if r["status"] == "现行有效")
    invalid = [r for r in results if r["status"] in ("已废止", "已修订")]
    unknown = [r for r in results if r["status"] == "未知"]

    return ToolResult(
        ok=True,
        tool="statute_validity_check",
        data={
            "checked": len(results),
            "valid": ok_count,
            "invalid": len(invalid),
            "unknown": len(unknown),
            "results": results,
            "conclusion": _validity_conclusion(len(results), ok_count, len(invalid), len(unknown)),
        },
        degraded=len(unknown) > 0,
        fallback_note=f"{len(unknown)} 条法条效力状态未知（法规库未收录元数据）" if unknown else "",
    )


def _check_one(item: dict[str, Any]) -> dict[str, Any]:
    doc = (item.get("doc_name") or "").strip()
    art = normalize_article(item.get("article_no") or "")
    plain = doc.strip("《》")

    with session_scope() as db:
        row = (
            db.query(StatuteDoc)
            .filter(StatuteDoc.doc_name.in_([doc, plain, f"《{plain}》"]))
            .one_or_none()
        )
        if row is None:
            row = db.query(StatuteDoc).filter(StatuteDoc.doc_name.like(f"%{plain}%")).first()

    if row is None:
        status = item.get("status") or "未知"
        return {
            "doc_name": doc, "article_no": art, "status": status,
            "reason": "法规库中未找到该法规的元数据，无法确认效力状态，需人工核实",
            "source": item.get("source_path") or "",
        }

    status = row.status or "未知"
    reason = {
        "现行有效": f"现行有效（施行日期：{row.effective_date or '未标注'}）",
        "已修订": f"已修订（版本：{row.version or '未标注'}），引用时应核对最新文本",
        "已废止": f"已废止（废止日期：{row.repealed_date or '未标注'}），**不得作为裁判依据**",
        "尚未生效": f"尚未生效（生效日期：{row.effective_date or '未标注'}）",
        "未知": "效力状态未标注，需人工核实",
    }.get(status, "效力状态未标注")

    return {
        "doc_name": row.doc_name,
        "article_no": art,
        "status": status,
        "level": row.level,
        "publisher": row.publisher,
        "effective_date": row.effective_date,
        "repealed_date": row.repealed_date,
        "reason": reason,
        "source": row.stored_path,
    }


def _validity_conclusion(total: int, valid: int, invalid: int, unknown: int) -> str:
    if total == 0:
        return "无可校验条目"
    parts = [f"共校验 {total} 条，现行有效 {valid} 条"]
    if invalid:
        parts.append(f"存在 {invalid} 条已失效/已修订条文，将被引用终审拦截")
    if unknown:
        parts.append(f"{unknown} 条效力状态未知，需人工核实后再引用")
    return "；".join(parts)


async def statute_version_compare(doc_name: str, *, article_no: str = "") -> ToolResult:
    """法规版本比对：新旧条文差异。

    法规库中同一名称存在多个版本时逐条比对；只有一个版本时，
    返回该法规的版本信息与条文清单，并明确说明无对比基准。
    """
    plain = doc_name.strip("《》")
    with session_scope() as db:
        rows = db.query(StatuteDoc).filter(StatuteDoc.doc_name.like(f"%{plain}%")).all()
        versions = [
            {
                "doc_name": r.doc_name, "version": r.version or "", "status": r.status,
                "publish_date": r.publish_date, "effective_date": r.effective_date,
                "repealed_date": r.repealed_date, "article_count": r.article_count,
                "stored_path": r.stored_path,
            }
            for r in rows
        ]

    if not versions:
        return ToolResult(
            ok=False, tool="statute_version_compare",
            error=f"法规库中未找到「{doc_name}」，无法比对",
            degraded=True, fallback_note="法规库未收录该法规",
        )

    if len(versions) == 1:
        return ToolResult(
            ok=True, tool="statute_version_compare",
            data={
                "doc_name": doc_name, "compared": False, "versions": versions,
                "diff": [],
                "conclusion": "法规库中仅有一个版本，无新旧对比基准。若需版本比对，请先导入该法规的历史版本。",
            },
            degraded=True, fallback_note="仅有一个版本，无法比对",
        )

    # 多版本：按生效日期排序，比较条文号集合差异
    versions.sort(key=lambda v: v.get("effective_date") or v.get("publish_date") or "")
    old, new = versions[-2], versions[-1]
    diff = await _article_set_diff(old["stored_path"], new["stored_path"])
    return ToolResult(
        ok=True, tool="statute_version_compare",
        data={
            "doc_name": doc_name, "compared": True,
            "from": old, "to": new, "diff": diff,
            "conclusion": f"对比 {old.get('version') or old.get('publish_date')} → "
                          f"{new.get('version') or new.get('publish_date')}："
                          f"新增 {len(diff['added'])} 条，删除 {len(diff['removed'])} 条，修改 {len(diff['changed'])} 条",
        },
    )


async def _article_set_diff(old_path: str, new_path: str) -> dict[str, list[dict[str, Any]]]:
    from pathlib import Path

    from backend.parser.pipeline import parse_document
    from backend.parser.structure import split_articles

    async def load(path: str) -> dict[str, str]:
        p = Path(path)
        if not p.exists():
            return {}
        parsed = await parse_document(p, do_vision=False)
        return {a.article_no: a.text.strip() for a in split_articles(parsed.text)}

    old_arts = await load(old_path)
    new_arts = await load(new_path)
    added = [{"article_no": k, "text": v[:200]} for k, v in new_arts.items() if k not in old_arts]
    removed = [{"article_no": k, "text": v[:200]} for k, v in old_arts.items() if k not in new_arts]
    changed = [
        {"article_no": k, "old": old_arts[k][:200], "new": new_arts[k][:200]}
        for k in old_arts
        if k in new_arts and old_arts[k].strip() != new_arts[k].strip()
    ]
    return {"added": added, "removed": removed, "changed": changed}


def today() -> date:
    return date.today()
