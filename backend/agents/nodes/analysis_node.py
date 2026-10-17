"""AnalysisNode：案件分析（方案 4.1 Analysis Agent）。

职责：「争议焦点归纳、攻防要点、诉讼策略、时效核查；**类案不可用时明确标注证据缺口**」。

本节点与 ReviewNode 的关键差别：合同审查有明确的"该改哪一条"，而案件分析
产出的是**判断**。判断最容易滑向自由发挥，所以这里有三条约束：

1. **争议焦点必须能从事实推出**。Prompt 里给了 facts，且要求 LLM 只能在此基础上
   归纳；IntakeNode 已经尽力从材料里抽了争点，本节点只在"没抽到"时补，
   而不是推翻重写 —— 材料里的争点比模型猜的准。
2. **诉讼时效单独核查**。时效是"程序上直接输掉"的少数事由之一，方案要求单列
   ``limitation_check``。无法判断时必须写"需核实起算点"，不允许给一个看起来
   确定的结论 —— 时效结论错一次的代价是当事人的实体权利。
3. **类案缺口必须显式传递**。类案检索失败时不能静默，否则律师会把
   "没有类案支撑的意见"当成"有类案支撑"。

不写 ``state["confidence"]``：置信度由 ``harness.finish()`` 统一合成（见
``state_manager.compute_confidence``），单节点覆写会抹掉前序证据的分量。
"""

from __future__ import annotations

import logging
from typing import Any

from backend.agents.harness import Harness
from backend.agents.state import GAP_NO_CASES, GAP_NO_FACTS, GAP_NO_STATUTES, LegalState
from backend.llm.client import llm_client
from backend.llm.prompts import (
    CASE_ANALYSIS_PROMPT,
    ISSUE_PROMPT,
    LEGAL_SYSTEM_PROMPT,
    build_messages,
    evidence_blocks as _evidence_blocks,
    market_skill_context,
    system_with_persona,
)
from backend.skills import skill_context

logger = logging.getLogger("legal_agent.nodes.analysis")

AGENT = "analysis"

# 时效相关关键词：命中则必须给出时效核查结论
LIMITATION_KEYWORDS = ("时效", "期间", "起算", "中断", "中止", "除斥期间", "仲裁时效", "诉讼时效")


async def analysis_node(state: LegalState, harness: Harness) -> LegalState:
    """争议焦点 + 攻防要点 + 时效核查。"""
    harness.step("AnalysisNode")

    blocks = _evidence_blocks(state)
    # 基础系统提示只放角色 + 引用纪律 + 本所作业路径；
    # 各步骤的"输出什么字段"分别由 ISSUE_PROMPT / CASE_ANALYSIS_PROMPT 追加。
    base = f"{system_with_persona(state.get('expert_persona', ''))}\n\n{skill_context('case_analysis')}"
    # 方案 B：按意图路由注入与本次分析相关的已启用市场技能，
    # 只注入适用技能，避免无关技能拼接缝合
    mkt = market_skill_context(
        state.get("user_id", ""),
        intent=state.get("intent", ""),
        query=state.get("query", ""),
    )
    if mkt:
        base = f"{base}\n\n{mkt}"

    # ---- 1. 争议焦点：只在 IntakeNode 没抽到时才让模型归纳
    if not state.get("issues"):
        await _fill_issues(state, harness, blocks, f"{base}\n\n{ISSUE_PROMPT}")

    # ---- 2. 自主委派：现在知道"要分析什么"了，缺哪类专业能力就去找哪个 Agent。
    #      放在 1 之后、3 之前是刻意的 —— 争点定了才有像样的检索式可委托，
    #      而补齐的证据要赶在 _analyze 之前进池子，否则模型看不到。
    await _collaborate(state, harness)

    # ---- 3. 攻防要点 / 策略 / 时效（用补齐后的证据重算）
    data = await _analyze(state, harness, _evidence_blocks(state),
                          f"{base}\n\n{CASE_ANALYSIS_PROMPT}")

    added = harness.merge(state, "issues", data.get("issues") or [])
    harness.tracer.info("AnalysisNode", "issues", f"新增争议焦点 {added} 个")

    strategy = [str(s).strip() for s in (data.get("strategy") or []) if str(s).strip()]
    if strategy:
        existing = list(state.get("strategy") or [])
        for s in strategy:
            if s not in existing:
                existing.append(s)
        state["strategy"] = existing
        harness.tracer.info("AnalysisNode", "strategy", f"攻防要点 {len(strategy)} 条")

    await _store_limitation(state, harness, data.get("limitation_check") or {})
    _gap_checks(state, harness, data)

    harness.snapshot()
    return state


# --------------------------------------------------------------------------- 自主委派
# "我缺哪类专业能力" → 找哪个 Agent。元组为 (目标节点, 判定池, 检索工具, 说法)。
# 顺序即优先级：类案比本所指引更影响判断质量。
_COLLABORATIONS: tuple[tuple[str, str, str, str], ...] = (
    ("CaseNode", "cases", "case_search", "类案"),
    ("KnowledgeNode", "knowledges", "knowledge_search", "本所办案指引"),
)

# 各检索工具的每任务配额（工具名 → settings 上的配置键）。
# ``knowledge_search`` 不在表里 = 未设配额（知识库不是法定证据，不限次）。
_TOOL_QUOTA: dict[str, str] = {"case_search": "CASE_SEARCH_MAX_CALLS"}


def _collaboration_focus(state: LegalState) -> str:
    """拼委派检索式：**用争议焦点，不用当事人原话**。

    ``state["query"]`` 是当事人的口语描述（"公司把我辞了还不给钱，还不签合同"），
    而争议焦点是已经归纳过的**法律问题**。检索类案与办案指引用后者命中率高得多 ——
    被调方拿到的是"未签书面劳动合同的二倍工资差额如何认定"，
    而不是一句要去猜的口语。
    """
    titles = [str(i.get("title") or "").strip()
              for i in (state.get("issues") or [])[:3]]
    focus = " ".join(t for t in titles if t)
    return focus or (state.get("query") or "").strip()


async def _collaborate(state: LegalState, harness: Harness) -> None:
    """分析开始前，把"缺的专业能力"通过 A2A 委派给对应 Agent。

    这是"多 Agent 自主委派"的主要落点。在此之前，缺什么完全由静态路由表决定，
    而 ``planner`` 恰恰**专门准备牺牲 CaseNode 与 KnowledgeNode**
    （``required=node not in ("KnowledgeNode","CaseNode")``）：步数预算一紧就砍掉
    类案检索，然后在报告里登记一条缺口，让律师自己留意。

    但本节点是"下判断"的地方 —— 争议焦点、攻防要点、诉讼策略都要类案支撑，
    所以它比规划器更有资格说"我这次真的需要类案"。判据按顺序是：
    **池子为空 → 目标在起手范围内 → 工具没熔断 → 配额还有余**，
    四条都过才把那个 Agent 叫回来；补不上才声明缺口
    （``_gap_checks`` 照旧登记，缺口语义完全不变）。

    只补**池子为空**的那一类，不做无条件预取：已有证据时再检一遍是纯浪费，
    而 ``case_search`` 的配额（默认 2 次）是**全任务共享**的 ——
    本节点多用一次，后面排程的节点就少一次。

    起手范围见 ``delegation._start_scope``：本次意图的静态路由 ∪ 已执行 ∪ 被预算裁掉的。
    并进静态路由是刻意的 —— 动态规划（含模型复核）砍节点不留痕，
    若只认 route/trimmed，"模型嫌贵裁掉、干活时发现真需要"的 Agent 就永远拉不回来。
    """
    focus = _collaboration_focus(state)
    if not focus:
        return

    from backend.agents.protocol.delegation import (
        Delegation, delegate, in_start_scope,
    )
    from backend.app.config import settings

    for node, field, tool, label in _COLLABORATIONS:
        if state.get(field):
            continue
        if not in_start_scope(harness, node):
            # 不在起手范围（本次意图压根不需要它）—— 那不是缺口，别去捞。
            continue
        if harness.invoker.handler.should_skip(tool):
            # 工具已熔断（连续失败到阈值）：**池子空是因为工具坏了，不是因为检索式不好**。
            # 换个检索式再发一次注定还是失败，只是把"知识库不可用"这件事
            # 拖到多烧一次调用之后才承认。真机实测过这条路：
            # knowledge_search 超时 → 熔断 → 委派回执 ok=True 但池子仍空 →
            # landed() 拦截 → 白等一轮，最后登记的还是同一条工具缺口。
            harness.tracer.info(
                "AnalysisNode", "collab_skip",
                f"{label}检索工具已熔断，不再委派 {node}（池子空是工具不可用，不是检索式问题）")
            continue
        cap = int(getattr(settings, _TOOL_QUOTA[tool], 0) or 0) if tool in _TOOL_QUOTA else 0
        if cap and harness.quota_used(tool) >= cap:
            harness.tracer.info(
                "AnalysisNode", "collab_skip",
                f"{label}检索配额已用尽（{cap} 次），不再委派 {node}")
            continue

        await delegate(
            harness,
            Delegation(target=node, from_node="AnalysisNode", focus=focus,
                       reason=f"分析要下判断，需要{label}支撑，但{label}池为空"),
            landed=lambda pool, f=field: bool(pool.get(f)),
        )


# --------------------------------------------------------------------------- 焦点
async def _fill_issues(state: LegalState, harness: Harness,
                       blocks: list[tuple[str, str]], system: str) -> None:
    """IntakeNode 未抽出争议焦点时，用 LLM 归纳一次。

    为什么不用 LLM 覆盖已有争点：IntakeNode 的争点来自材料里的显式表述
    （"争议焦点：……"）或当事人问句，那是**当事人真实关心的问题**；
    模型归纳出的更"规范"，但可能偏离当事人诉求。以当事人的为准。
    """
    facts = state.get("facts") or []
    if not facts:
        # **这里刻意不登记证据缺口**：本节点末尾的 ``_gap_checks`` 会以规范措辞
        # 登记同一条事实缺口（见 ``agents.state.GAP_NO_FACTS``）。两处都登记时
        # 措辞不同、去重失效，同一件事会在报告里出现两遍。
        harness.tracer.info("AnalysisNode", "issues_skip",
                            "无结构化案件事实，跳过争议焦点归纳")
        return

    res = await llm_client.chat_json(
        build_messages(system, _issue_user(state, facts), blocks),
        task="issues",
        payload={"facts": facts, "statutes": state.get("statutes") or [],
                 "cases": state.get("cases") or []},
        default={"issues": []},
    )
    data, llm_res = res
    issues = data.get("issues") or []
    if llm_res.degraded:
        harness.tracer.warn("AnalysisNode", "issues_rule_based",
                            "争议焦点由规则引擎归纳，维度覆盖有限，需律师核对")
    if issues:
        harness.merge(state, "issues", issues)


def _issue_user(state: LegalState, facts: list[dict[str, Any]]) -> str:
    fact_lines = "\n".join(
        f"{i}. {f.get('text') or ''}（来源：{f.get('source') or '未标注'}）"
        for i, f in enumerate(facts[:15], start=1)
    )
    return (f"当事人诉求：{state.get('query') or '（未提供）'}\n\n"
            f"【已提取的案件事实】\n{fact_lines}\n\n"
            "请归纳争议焦点，只输出 JSON。")


# --------------------------------------------------------------------------- 分析
async def _analyze(state: LegalState, harness: Harness,
                   blocks: list[tuple[str, str]], system: str) -> dict[str, Any]:
    cases = state.get("cases") or []
    has_case_evidence = bool(cases)
    # 类案不可用时在用户消息里直接告诉模型，否则它常会自行"补"出类案倾向
    case_note = ("【类案证据】本次检索到 %d 个类案，裁判倾向可参照。" % len(cases)
                 if has_case_evidence else
                 "【类案证据】本次类案检索未取得有效结果。**不得提及任何裁判倾向或类案，"
                 "并需在证据缺口中明确说明**。")

    user = (
        f"当事人诉求：{state.get('query') or '（未提供）'}\n\n"
        f"【争议焦点】\n" + "\n".join(
            f"- {(i.get('title') or '')}：{(i.get('description') or '')[:200]}"
            for i in (state.get("issues") or [])[:10]
        ) + "\n\n"
        f"【案件事实】\n" + "\n".join(
            f"- {f.get('text') or ''}" for f in (state.get("facts") or [])[:15]
        ) + "\n\n"
        f"{case_note}\n\n"
        "请给出攻防要点、对方可能的抗辩与应对、诉讼/仲裁时效核查。只输出 JSON。"
    )

    data, llm_res = await llm_client.chat_json(
        build_messages(system, user, blocks),
        task="case_analysis",
        payload={"facts": state.get("facts") or [],
                 "statutes": state.get("statutes") or [],
                 "cases": cases},
        default={"issues": [], "strategy": [], "limitation_check": {},
                 "evidence_gap_notes": [], "confidence": 0.4},
    )
    if llm_res.degraded:
        harness.tracer.warn("AnalysisNode", "llm_degraded",
                            "分析由确定性规则引擎完成，覆盖维度有限，需律师复核")
    return data


# --------------------------------------------------------------------------- 时效
def _plain_law_name(name: str) -> str:
    """法规名规范化（与 harness.validator._plain 同一套规则，两边必须一致）。"""
    cleaned = (name or "").strip().strip("《》").strip()
    for prefix in ("中华人民共和国", "最高人民法院"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break
    return cleaned


def _pool_has_statute(state: LegalState, law: str, article: str) -> bool:
    """证据池里是否已有「法规名+条号」都匹配的条文（引用回溯的判据）。"""
    from backend.parser.structure import normalize_article_no

    want = normalize_article_no(article) or article
    for s in state.get("statutes") or []:
        if (_plain_law_name(s.get("doc_name") or "") == _plain_law_name(law)
                and (normalize_article_no(s.get("article_no") or "") or "") == want):
            return True
    return False


async def _fetch_statute_into_pool(state: LegalState, harness: Harness,
                                   law: str, article: str) -> bool:
    """把兜底法条**真正检索入库**，让引用可回溯。

    红线一「无来源不出引用」要求报告里每条《法名》第X条都能回溯到本次检索
    证据池。内置兜底法条（劳动仲裁时效/普通诉讼时效）是确定真实的法条，
    但如果只是写进 ``basis`` 而证据池里没有，引用终审会按"疑似无来源"拦截
    —— 等于消掉"时效缺依据"缺口又换来"引用被拦截"缺口。
    所以兜底时先补一次专项检索，命中就落进 ``state["statutes"]``，引用即可回溯；
    检索不到（库里确实没有/配额耗尽）则返回 False，由调用方降级为
    不带引用格式的表述（终审不会拦，缺口声明照旧如实说明）。

    **检索走 A2A 委派而非直调工具**（A2A 主链接入点）：先把专项检索式
    委派给 Statute Agent（``A2AClient.call("statute", RUN)``，进程内通道），
    由它完成"检索 + 合池去重 + 效力补标"的完整职责；委派未命中/未注册/
    异常时回退到原有 ``harness.tool`` 直调路径，行为兜底完全不变。
    只在执行计划本就包含 StatuteNode 且检索配额还有余额时才委派 ——
    路由、步数、缺口三条主链语义因此与直调时代一致。
    """
    from backend.parser.structure import normalize_article_no

    want = normalize_article_no(article) or article

    # ---- A2A 委派 Statute Agent（主链接入点） ----
    if "StatuteNode" in (state.get("route") or []):
        from backend.app.config import settings
        if harness.quota_used("statute_search") < settings.STATUTE_SEARCH_MAX_CALLS:
            from backend.agents.protocol.delegation import Delegation, delegate

            art = await delegate(
                harness,
                Delegation(target="StatuteNode", from_node="AnalysisNode",
                           focus=f"《{law}》{article}",
                           reason=f"分析要引用《{law}》{article}，证据池里没有"),
                landed=lambda pool: _pool_has_statute(pool, law, article))
            if art is not None:
                return True

    try:
        res = await harness.tool("statute_search", "AnalysisNode",
                                 query=f"《{law}》{article}", top_k=5)
    except Exception as exc:  # noqa: BLE001
        harness.tracer.warn("AnalysisNode", "limitation_fetch_error",
                            f"兜底法条专项检索异常：{exc}")
        return False
    if not getattr(res, "ok", False):
        return False
    data = res.data if isinstance(res.data, dict) else {}
    for s in data.get("statutes") or []:
        if (_plain_law_name(s.get("doc_name") or "") == _plain_law_name(law)
                and (normalize_article_no(s.get("article_no") or "") or "") == want):
            pool = list(state.get("statutes") or [])
            dup = any((s.get("doc_name") or "") == (e.get("doc_name") or "")
                      and (s.get("article_no") or "") == (e.get("article_no") or "")
                      for e in pool)
            if not dup:
                pool.append(s)
                state["statutes"] = pool
                harness.tracer.info("AnalysisNode", "limitation_fetched",
                                    f"兜底法条已专项检索入库：《{law}》{article}")
            return True
    return False


async def _store_limitation(state: LegalState, harness: Harness, check: dict[str, Any]) -> None:
    """诉讼/仲裁时效核查（方案 4.1 Analysis Agent 要求项）。

    无论 LLM 给没给，都必须落一个 ``limitation_check``：**缺这一项比给一个
    粗糙的结论更危险** —— 律师会以为案件不存在时效问题。
    所以给不出结论时写 "需核实起算点"，把不确定性显式留在交付物里。
    """
    result: dict[str, Any] = {
        "period": (check.get("period") or "").strip(),
        "start_point": (check.get("start_point") or "").strip(),
        "conclusion": (check.get("conclusion") or "").strip(),
        "basis": [b for b in (check.get("basis") or []) if b],
    }

    if not result["period"]:
        # 规则兜底：劳动案件走仲裁时效，其余按普通诉讼时效
        text = " ".join([
            state.get("query") or "",
            " ".join((i.get("title") or "") for i in (state.get("issues") or [])),
            " ".join((f.get("text") or "") for f in (state.get("facts") or [])),
            " ".join((s.get("doc_name") or "") for s in (state.get("statutes") or [])),
        ])
        if "劳动" in text:
            result["period"] = "劳动仲裁时效 1 年"
            result["start_point"] = "自当事人知道或应当知道其权利被侵害之日起计算"
        else:
            result["period"] = "诉讼时效 3 年"
            result["start_point"] = "自权利人知道或应当知道权利受到损害以及义务人之日起计算"
        result["conclusion"] = result["conclusion"] or "需核实起算点与是否存在中断/中止事由"

    if not result["basis"]:
        # 检索证据里没拿到时效法条（降级/无检索时常见）：用内置通用法条兜底，
        # 避免"时效结论完全无依据"的硬缺口——无模型时这条尤其必要。
        # 兜底法条与上面 period 的规则兜底保持一致（劳动→1年，其他→3年）。
        # 注意：兜底法条**必须可回溯**（红线一），所以先看证据池有没有、
        # 没有就专项检索入库；两头都落空时不能写成《法名》第X条格式，
        # 否则引用终审会按"疑似无来源"拦截（本次检索证据里没有这条法规）。
        lim_text = " ".join([
            state.get("query") or "",
            " ".join((i.get("title") or "") for i in (state.get("issues") or [])),
            " ".join((f.get("text") or "") for f in (state.get("facts") or [])),
        ])
        is_labor = "劳动" in lim_text
        law = "劳动争议调解仲裁法" if is_labor else "中华人民共和国民法典"
        article = "第二十七条" if is_labor else "第一百八十八条"
        explain = ("劳动争议申请仲裁的时效期间为一年" if is_labor
                   else "向人民法院请求保护民事权利的诉讼时效期间为三年")

        if _pool_has_statute(state, law, article) or \
                await _fetch_statute_into_pool(state, harness, law, article):
            # 已在/已补进检索证据池：按标准引用格式写，引用终审可回溯、可核验
            result["basis"] = [f"《{law}》{article}（{explain}）"]
            result["conclusion"] = (result["conclusion"] or "").rstrip("。") + \
                "（时效依据已专项检索入库，须律师核实具体起算点与中断/中止事由）"
        else:
            # 专项检索也拿不到：退为**不带引用格式**的通用表述，
            # 把"未命中检索条文"如实说出来，不给终审留拦截目标
            label = "劳动仲裁时效为一年" if is_labor else "诉讼时效为三年"
            result["basis"] = [f"{label}（通用规则兜底，本次检索证据未命中对应条文）"]
            result["conclusion"] = (result["conclusion"] or "").rstrip("。") + \
                "（依据通用规则兜底，未做专项检索命中，须律师核实具体起算点与中断/中止事由）"

    state["limitation_check"] = result
    harness.tracer.info("AnalysisNode", "limitation",
                        f"{result['period']}｜{result['conclusion'][:60]}")

    # 案情里明确提到时效但结论仍是"需核实"时，单独提示：律师必须自己算一次
    blob = " ".join([state.get("query") or ""] + [f.get("text") or "" for f in (state.get("facts") or [])])
    if any(k in blob for k in LIMITATION_KEYWORDS) and "需核实" in result["conclusion"]:
        harness.gaps("analysis", "案件涉及时效问题，但本次未能给出确定的时效结论",
                     "时效届满将直接影响实体权利，须律师按起算点逐项核算")


# --------------------------------------------------------------------------- 缺口
def _gap_checks(state: LegalState, harness: Harness, data: dict[str, Any]) -> None:
    """把分析阶段发现的证据缺口写进 State，并传递 LLM 自己声明的缺口。

    **顺序是刻意的：先登记本节点自己的缺口，再传递模型声明的缺口。**
    两者可能描述同一件事（模型也会说"没有检索到类案"），而去重保留**首个**登记的
    那一条 —— 本节点登记时带的是针对本次交付的具体影响，比模型那句中性的
    "该缺口会影响结论的确定性"更有用。反过来先放模型缺口，具体影响就被挤掉了。
    """
    if not state.get("statutes"):
        harness.gaps("analysis", GAP_NO_STATUTES,
                     "结论仅能作为问题梳理，不得作为正式法律意见")
    if not state.get("cases"):
        harness.gaps("analysis", GAP_NO_CASES,
                     "缺少同类案件的裁判倾向参考，结论的说服力下降")
    if not state.get("facts"):
        harness.gaps("analysis", GAP_NO_FACTS,
                     "分析基于当事人陈述，事实部分未经材料核验")

    for note in data.get("evidence_gap_notes") or []:
        text = str(note).strip()
        if text:
            harness.gaps("analysis", text, "该缺口会影响结论的确定性，交付时须一并呈现")


def analysis_digest(state: LegalState) -> dict[str, Any]:
    """分析结果摘要，供 ReportNode 组装结论。"""
    lc = state.get("limitation_check") or {}
    return {
        "issues": len(state.get("issues") or []),
        "strategy": len(state.get("strategy") or []),
        "limitation_period": lc.get("period") or "",
        "limitation_uncertain": "需核实" in (lc.get("conclusion") or ""),
    }
