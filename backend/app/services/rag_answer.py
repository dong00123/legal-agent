"""纯 RAG 问答：检索 → 生成 → 引用回溯。**不经过任何 Agent 节点。**

为什么单独有一条链路
--------------------
AI 助手页要的是"对话"（核心结论 / 具体分析 / 通俗化输出 / 额外思考 + 行内法条标签 +
末尾参考法规卡片），而 ``orchestrator`` 的交付物是**报告模板**——它一定会拼出
「引用清单 / 证据缺口与不确定性 / 综合置信度」这三节。改动报告模板会同时改变
合同审查、文书起草、类案检索三个入口的产出，那三个入口要的恰恰是报告。
所以不是"改报告"，而是**另开一条只做检索与生成的轻链路**。

这条链路里没有 VerificationNode，所以有一个必须守住的口径：
**行内标签只能表达"该法条在检索结果里的效力状态"，不能表达"这句话的引用已核验"。**
终审在编排图里排在报告生成之前，报告正文从未被它扫过；这条链路连终审都没有。
界面上写"已核验"就是编。

引用回溯（本模块的核心）
------------------------
模型被要求写 ``[S1]`` 这样的行内标记，但它会犯错，而**法律场景里引用的错误不能靠模型自觉**。
:func:`extract_cites` 用确定性规则把正文与证据池对齐，三种情况分别处置：

- 标记指向池内条目 → 保留，产出可点击的引用卡；
- 标记越界（``[S9]`` 池里只有 5 条）→ **从正文里删掉**，记入 ``unsupported``；
- 正文写了《劳动合同法》第四十条却漏了标记 → 按 ``法规名|条号`` 对齐后**回填**标记；
- 正文写了池外的条文号（模型凭记忆编的）→ **把条号换成「相应条款」**。
  最后这条是红线「无来源不出引用」的落地：宁可示弱，也不在正文里留一个
  看起来有出处、实际查无此条的条文号。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from backend.app.config import settings
from backend.llm import prompts
from backend.llm.client import llm_client
from backend.rag.domain import domain_label, filter_statutes_by_domain, infer_domain

logger = logging.getLogger("legal_agent.services.rag_answer")

# 行内标记：``[S1]`` / ``[C2]`` / ``[K3]``。与 ``prompts.LEGAL_SYSTEM_PROMPT``
# 要求模型输出的格式、以及 ``evidence_blocks`` 发出的证据编号规则必须一致。
MARKER_RE = re.compile(r"\[([SCK])(\d{1,2})\]")

# 允许外泄的字段白名单。**不是"排除 source_path"，而是"只放行这些"** ——
# 检索命中里可能带任何东西（内部路径、向量分数、调试字段），
# 用黑名单迟早漏一个出去。``source_path`` 会暴露服务器目录结构。
_STATUTE_FIELDS = ("doc_name", "article_no", "text", "status", "publisher",
                   "version", "effective_date", "repealed_date", "chunk_id")
# 案例字段要够界面把详情弹窗填满 —— 只给到 ``gist`` 的后果是
# 「类案」标签点开后看不到最值钱的裁判理由。``score`` 供详情弹窗显示相似度。
# **``full_text`` 不在白名单里**：库里的 ``text`` 是按 section 切出来的片段
# （"案例名称/关键词/裁判要旨"各是一条），把它当"案例全文"展示是编。
_CASE_FIELDS = ("case_no", "case_name", "court", "judge_date", "cause_of_action",
                "gist", "result", "reasoning", "related_statutes", "keywords",
                "source", "score", "chunk_id")

_KIND_FIELDS = {"S": _STATUTE_FIELDS, "C": _CASE_FIELDS, "K": ("title", "doc_type", "version", "chunk_id")}

# 追问改写：出现这些**句首**连接词，说明本轮问题省略了主语/宾语
_FOLLOWUP_HEAD = ("那", "那么", "还有", "另外", "再问", "再问一下", "如果", "要是", "呢")
# 出现这些**回指词**同理
_FOLLOWUP_WORDS = ("它", "这个", "该", "此", "上述", "上面", "这种情况", "那笔")

# 追问改写后检索式的长度上限：拼太长会让检索式被无关词淹没
_MAX_STANDALONE_CHARS = 120


# --------------------------------------------------------------------------- 数据结构
@dataclass
class RagAnswer:
    """纯 RAG 问答的交付物。"""

    query: str = ""
    retrieval_query: str = ""          # 追问改写后**实际用于检索**的式子
    rewritten: bool = False            # 是否发生过追问改写
    answer: str = ""                   # 正文（``[Sn]`` 标记已按池校验过）
    citations: list[dict[str, Any]] = field(default_factory=list)
    ref_check: dict[str, Any] = field(default_factory=dict)
    retrieval: dict[str, Any] = field(default_factory=dict)
    disclaimer: str = ""
    provider: str = ""
    degraded: bool = False
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "retrieval_query": self.retrieval_query,
            "rewritten": self.rewritten,
            "answer": self.answer,
            "citations": self.citations,
            "ref_check": self.ref_check,
            "retrieval": self.retrieval,
            "disclaimer": self.disclaimer,
            "provider": self.provider,
            "degraded": self.degraded,
            "elapsed_ms": self.elapsed_ms,
        }


# --------------------------------------------------------------------------- 纯函数
def _key(name: str, article: str) -> str:
    """``法规名|条号`` 比较键。

    **必须与 ``harness.validator._statute_key`` 逐字一致**（同一套 ``_plain`` /
    ``_norm_article``）。两边各写一份的后果是「中华人民共和国劳动合同法 第八十七条」
    与库里的「劳动合同法 第87条」对不上，正确引用被当成编造剥掉。
    """
    from backend.agents.harness.validator import _norm_article, _plain

    return f"{_plain(name)}|{_norm_article(article)}"


def _looks_like_statute(name: str) -> bool:
    from backend.agents.harness.validator import _looks_like_statute as f

    return f(name)


def slim(items: list[Any], kind: str) -> list[dict[str, Any]]:
    """白名单序列化：只留下界面要用的字段。

    接受 pydantic 命中对象或 dict。``source_path`` 一定在内网路径里，**必须丢掉**。
    """
    fields = _KIND_FIELDS.get(kind, ())
    out: list[dict[str, Any]] = []
    for it in items or []:
        if isinstance(it, dict):
            src = it
        elif hasattr(it, "model_dump"):          # pydantic v2（StatuteHit 等）
            src = it.model_dump()
        else:
            src = getattr(it, "__dict__", {})
        row: dict[str, Any] = {}
        for f in fields:
            v = src.get(f)
            if v is None:
                continue
            row[f] = str(v) if f.endswith("_date") else v
        if row:
            out.append(row)
    return out


def pool_of(statutes: list[dict[str, Any]], cases: list[dict[str, Any]],
            knowledges: list[dict[str, Any]] | None = None) -> dict[str, list[dict[str, Any]]]:
    """证据池。编号规则 S1..Sn / C1..Cn / K1..Kn，与 ``evidence_blocks`` 一致。"""
    return {"statutes": list(statutes or []), "cases": list(cases or []),
            "knowledges": list(knowledges or [])}


def resolve_followup(question: str, history: list[dict[str, str]] | None) -> tuple[str, bool]:
    """把省略主语的追问补成可独立检索的式子。

    **历史只用于改写检索式，永不作为证据。** 把上一轮的结论当证据并进本轮，
    会让"本次结论的依据来自上一个问题"——报告可以追溯到上一轮，
    而上一轮的回答本身没有出处。追问一律**重新检索**。

    判据刻意保守：只认明确的回指词/句首连接词。判错的方向性后果不对称 ——
    把新问题误当追问，会拿一个不相关的旧问题去检索，**召回错误的法条**；
    把追问误当新问题，只是检索式短一点、召回弱一点。所以宁可漏判。
    """
    q = (question or "").strip()
    turns = [t for t in (history or []) if isinstance(t, dict) and str(t.get("q") or "").strip()]
    if not q or not turns:
        return q, False
    if len(q) > 40:                       # 长问题自己就是完整的，不需要补
        return q, False
    if not (q.startswith(_FOLLOWUP_HEAD) or any(w in q for w in _FOLLOWUP_WORDS)):
        return q, False

    prev = str(turns[-1].get("q") or "").strip()
    if not prev or prev == q:
        return q, False
    return f"{prev}；{q}"[:_MAX_STANDALONE_CHARS], True


def extract_cites(answer: str, pool: dict[str, list[dict[str, Any]]]) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """把正文里的引用标记与证据池对齐。

    返回 ``(正文, 引用卡列表, 回溯报告)``。正文可能被改写（删越界标记、补漏标记、
    剥编造的条号），所以**调用方必须用返回的正文**，不能用原来那份。
    """
    text = answer or ""
    statutes = pool.get("statutes") or []
    cases = pool.get("cases") or []
    knowledges = pool.get("knowledges") or []
    by_kind = {"S": statutes, "C": cases, "K": knowledges}

    unsupported: list[dict[str, Any]] = []
    order: list[str] = []                 # 引用卡的顺序 = 首次出现顺序
    cards: dict[str, dict[str, Any]] = {}

    def _register(kind: str, idx: int) -> str:
        """登记一张引用卡（已存在则只加出现次数），返回该标记的文本。

        **回填路径也必须走这里。** 只往正文里插 ``[S1]`` 而不建卡，
        前端就会渲染出一个点不开的标签——正文说有这条依据，末尾的参考法规里却找不到。
        """
        key = f"{kind}{idx}"
        if key not in cards:
            order.append(key)
            cards[key] = _card(kind, idx, by_kind[kind][idx - 1])
        cards[key]["occurrences"] += 1
        return f"[{key}]"

    # ---- 1. 标记：池内保留，越界删掉
    def _marker(m: re.Match[str]) -> str:
        kind, idx = m.group(1), int(m.group(2))
        items = by_kind.get(kind) or []
        if not (1 <= idx <= len(items)):
            unsupported.append({"marker": m.group(0), "reason": "标记超出检索结果范围"})
            return ""
        return _register(kind, idx)

    text = MARKER_RE.sub(_marker, text)

    # ---- 2. 正文里出现但没带标记的条文：对得上就回填，对不上就剥条号
    from backend.agents.harness.validator import STATUTE_CITE_RE

    index_of = {_key(s.get("doc_name") or "", s.get("article_no") or ""): i + 1
                for i, s in enumerate(statutes)}
    backfilled = 0

    def _statute(m: re.Match[str]) -> str:
        nonlocal backfilled
        raw = m.group(0)
        name, article = m.group(1), m.group(2) or ""
        plain = name.strip().strip("《》")
        if not _looks_like_statute(plain):
            return raw                       # 《房屋租赁合同》这类不是法规引用
        tail = text[m.end():m.end() + 8]
        if re.match(r"\s*\[[SCK]\d{1,2}\]", tail):
            return raw                       # 后面已经跟着标记，不再补
        idx = index_of.get(_key(plain, article))
        if idx:
            backfilled += 1
            return f"{raw}{_register('S', idx)}"
        if article:
            # 池里没有这一条：模型凭记忆写的条文号。**换成不带条号的说法**，
            # 而不是留着 —— 留一个查无此条的编号，等于给了它出处。
            unsupported.append({
                "marker": f"《{plain}》第{article}条",
                "reason": "检索结果中没有该条文",
                "action": "stripped_article_no",
            })
            return f"《{plain}》相应条款"
        return raw

    text = STATUTE_CITE_RE.sub(_statute, text)
    # 删掉越界标记后可能留下连着的空格/空行，清一下（上面几处 `return ""` 的副产物）
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)

    ref_check = {
        "total": len(cards),
        "backfilled": backfilled,
        "unsupported": unsupported,
        # 没有引用不是错误：检索结果确实覆盖不到时，模型应当直说。
        "ok": not unsupported,
    }
    return text, [cards[k] for k in order], ref_check


def _card(kind: str, idx: int, item: dict[str, Any]) -> dict[str, Any]:
    """一张引用卡。字段与前端 ``formatRagAnswer`` 的取用一一对应。"""
    marker = f"{kind}{idx}"
    base = {"marker": marker, "type": {"S": "statute", "C": "case", "K": "knowledge"}[kind],
            "occurrences": 0}
    for f in _KIND_FIELDS.get(kind, ()):
        v = item.get(f)
        if v:
            base[f] = v
    return base


def rag_system_prompt() -> str:
    """本链路的系统提示词 = 通用法务纪律 + 对话式输出契约。

    **两段都必须发出去。** 只发 ``LEGAL_SYSTEM_PROMPT`` 会丢掉
    「四节结构、不许写引用清单/证据缺口/置信度」这一整套约束 ——
    而模型会顺着通用纪律里的"结构清晰、条目化输出"自己长出一份报告，
    正是这个页面要去掉的东西。这个漏发实测发生过一次（prompt 常量写好了却没人引用），
    所以另有一条测试钉住"它真的进了消息序列"。
    """
    return f"{prompts.LEGAL_SYSTEM_PROMPT}\n\n{prompts.RAG_ANSWER_PROMPT.format(disclaimer=settings.DISCLAIMER)}"


def ensure_disclaimer(answer: str) -> str:
    """模型偶尔会漏掉免责声明。**这条不能靠模型自觉**，缺了就补。"""
    note = settings.DISCLAIMER
    if not note:
        return answer
    if note in (answer or ""):
        return answer
    return f"{(answer or '').rstrip()}\n\n{note}"


# --------------------------------------------------------------------------- 主流程
async def answer(
    query: str,
    *,
    history: list[dict[str, str]] | None = None,
    top_k_statutes: int = 8,
    top_k_cases: int = 4,
    on_stage: Any = None,
) -> RagAnswer:
    """检索 → 生成 → 引用回溯。

    ``on_stage(name, detail)`` 可选回调，用来向 SSE 推**真实阶段**
    （向量召回 / BM25 / 融合 / 生成），而不是编排图里的节点名 ——
    这条链路上没有节点，界面显示 ``StatuteNode`` 就是编。
    """
    from backend.rag.pipeline import retrieve_cases, retrieve_statutes

    started = time.perf_counter()
    out = RagAnswer(query=query.strip())
    out.disclaimer = settings.DISCLAIMER

    def stage(name: str, detail: str = "") -> None:
        if on_stage is None:
            return
        try:
            on_stage(name, detail)
        except Exception:  # noqa: BLE001 - 进度回调绝不能影响出答案
            logger.debug("stage 回调失败（忽略）", exc_info=True)

    out.retrieval_query, out.rewritten = resolve_followup(out.query, history)

    # ---- 检索（混合：向量 + BM25 + RRF + 重排，全在 hybrid_search 里）
    stage("混合检索 · 法条")
    statutes, st_trace = await retrieve_statutes(out.retrieval_query, top_k=top_k_statutes)
    # 领域过滤：与 statute_search 同一套词表，在证据池建立之前滤除跨领域错配
    # （如劳动问题召回环保税法），保证（五）行动建议按领域给路径时证据是同域的。
    domain = infer_domain(out.retrieval_query)
    if domain and domain != "unknown":
        # 与 slim 同一处理：兼容 pydantic 命中对象与 dict（测试/mock 常用 dict）
        _statute_dicts = [
            s if isinstance(s, dict)
            else s.model_dump(mode="json") if hasattr(s, "model_dump")
            else getattr(s, "__dict__", {})
            for s in statutes
        ]
        _kept = filter_statutes_by_domain(_statute_dicts, domain)
        if len(_kept) < len(_statute_dicts):
            if _kept:
                st_trace.notes.append(
                    f"按领域「{domain}」过滤：法条 {len(_statute_dicts)} 条中保留 {len(_kept)} 条，"
                    f"滤除 {len(_statute_dicts) - len(_kept)} 条跨领域错配")
                statutes = _kept   # list[dict]，slim 直接接受
            else:
                # 全滤空 ⇒ 领域判定与召回证据域不一致（弱信号误判可能），
                # 回退保留全量：宁可不过滤，也不能把证据清零造成"无据结论"。
                st_trace.notes.append(
                    f"按领域「{domain}」过滤后法条为 0，判定可能与证据域不符，已保留原召回")
    stage("混合检索 · 类案")
    cases, cs_trace = await retrieve_cases(out.retrieval_query, top_k=top_k_cases)

    pool = pool_of(slim(statutes, "S"), slim(cases, "C"))
    out.retrieval = {
        "statutes": len(pool["statutes"]),
        "cases": len(pool["cases"]),
        "vector_hits": int(getattr(st_trace, "vector_hits", 0) or 0)
                        + int(getattr(cs_trace, "vector_hits", 0) or 0),
        "bm25_hits": int(getattr(st_trace, "bm25_hits", 0) or 0)
                     + int(getattr(cs_trace, "bm25_hits", 0) or 0),
        "fused": int(getattr(st_trace, "fused", 0) or 0) + int(getattr(cs_trace, "fused", 0) or 0),
        "degraded": bool(getattr(st_trace, "degraded", False) or getattr(cs_trace, "degraded", False)),
        # 降级说明**必须原样透出**：降级了却看起来像正常结果，是法律场景最不该发生的事。
        "notes": list(getattr(st_trace, "notes", None) or []) + list(getattr(cs_trace, "notes", None) or []),
    }
    stage("重排完成", f"法条 {out.retrieval['statutes']} 条 · 类案 {out.retrieval['cases']} 篇")

    # ---- 生成。证据块复用 ``evidence_blocks``：法条带效力状态、判例带案号
    # 这两条硬约束只有一处实现，另写一份迟早会漏。
    blocks = prompts.evidence_blocks(pool)
    if not blocks:
        # 检索为空时**明确告诉模型没有资料**，否则它会用自己的记忆作答并写出条文号，
        # 而那些条文号既没有出处、也无法回溯 —— 正是红线要拦的东西。
        blocks = [("无", "本次检索未命中任何法条或类案。"
                         "只能基于一般法律常识谨慎作答，**不得写出任何条文号**，"
                         "并明确告知用户需要补充检索条件或材料。")]
        out.degraded = True

    stage("生成回答")
    system = rag_system_prompt()
    if domain and domain != "unknown":
        # 领域判定注入：RAG_ANSWER_PROMPT 的（五）行动建议按领域给救济路径，
        # 模型必须知道本次问题被判成哪个领域（unknown 时不注入，按通用路径作答）。
        system += (f"\n\n【领域判定】本次问题的法律领域识别为：{domain_label(domain)}。"
                   f"第（五）节「行动建议」请按该领域的救济路径给出（可执行的步骤，"
                   f"不是法条罗列）。")
    msgs = prompts.build_rag_messages(
        system,
        out.query,
        blocks,
        history,
    )
    result = await llm_client.chat(
        msgs,
        task="rag_answer",
        # 降级到规则引擎时，``payload`` 是**唯一的证据通道** —— ``RuleProvider``
        # 拿不到 messages 里的结构化字段。漏传的后果实测发生过：检索命中 8 法条
        # + 4 类案，兜底回答却写成「针对问题「」，未检索到相关来源」—— 问题与来源
        # 一起丢，还谎报了检索结论。检索为空与证据没传到是两回事，不能混。
        # 编号即 ``pool`` 下标：``extract_cites`` 按同一个顺序建引用卡。
        payload={
            "query": out.query,
            "retrieval_query": out.retrieval_query,
            "rewritten": out.rewritten,
            "statutes": pool["statutes"],
            "cases": pool["cases"],
            "knowledges": pool["knowledges"],
            "domain": domain or "unknown",
            "disclaimer": out.disclaimer,
        },
    )

    # ---- 引用回溯
    out.answer, out.citations, out.ref_check = extract_cites(result.text or "", pool)
    out.answer = ensure_disclaimer(out.answer)
    out.provider = result.provider
    out.degraded = bool(out.degraded or result.degraded or out.retrieval["degraded"])
    out.elapsed_ms = int((time.perf_counter() - started) * 1000)

    if not out.citations:
        logger.info("RAG 问答未产出引用：query=%s 检索法条=%d 类案=%d",
                    out.query[:40], out.retrieval["statutes"], out.retrieval["cases"])
    return out
