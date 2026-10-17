"""ReportNode：汇总输出（方案 4.1 Report Agent）。

原文约束：「汇总输出（附引用清单与置信度）；**强制标注「仅供律师工作参考」**」。

本节点是全流程的最后一关，也是**唯一**适合做 Skill 完整性校验的位置：

- 在中间节点调用 ``apply_skill_gaps`` 会把"还没轮到"误报成"没做"；
- 在最后调用才能如实回答"这次的作业路径走完了几步"。

所以本节点的顺序是刻意的：
1. **先 flush Trace**，再校验 Skill —— 合同审查第 5 步"是否真的扫过"要靠 Trace 事件
   区分"扫了没发现问题"与"根本没扫"，Trace 没落进 State 之前校验会误判（假阴性）。
2. 校验 Skill 并登记缺口；
3. 合成报告（含引用清单、证据缺口、置信度、免责声明）——置信度先算初值供正文显示，
   报告落进 State 后定稿并改写正文那一行（见 ``_restamp_confidence``）；
4. ``harness.finish()`` 统一收尾并落快照 —— 此时置信度已定稿，重算结果一致。

报告在引用终审未通过时**仍然生成**，但会带上"被拦截引用清单"——
律师最需要看到的恰恰是"哪些依据不能用"。
"""

from __future__ import annotations

import logging
import re as _re          # 模块级正则用；函数内仍按本文件惯例就地 ``import re``
from typing import Any

from backend.agents.harness import Harness
from backend.agents.harness.router import is_direct
from backend.agents.harness.state_manager import compute_confidence
from backend.agents.state import LegalState
from backend.app.config import settings
from backend.llm.client import llm_client
from backend.llm.evidence_sanitize import sanitize_evidence_text
from backend.llm.prompts import (
    ASSISTANT_REPORT_PROMPT,
    DIRECT_ANSWER_PROMPT,
    LEGAL_SYSTEM_PROMPT,
    QUICK_ANSWER_PROMPT,
    REPORT_PROMPT,
    build_messages,
    format_case_evidence,
    format_knowledge_evidence,
    format_statute_evidence,
    market_skill_context,
    system_with_persona,
)
from backend.skills import apply_skill_gaps, skill_for_intent

logger = logging.getLogger("legal_agent.nodes.report")

AGENT = "report"


def _expert_retrieval_guide(state: LegalState) -> str:
    """专家定向检索指引。

    专家声明了 ``expert_domains`` / ``expert_hints`` 时，生成一段指引注入
    system，让模型在调用法规 / 类案 / 知识库检索工具时**带上领域过滤**并
    **附加专业检索词** —— 专家版与普通助手在「知识来源」上拉开真实差异。
    专家未配置时返回空串，行为与普通助手逐字一致。
    """
    domains = [str(d) for d in (state.get("expert_domains") or []) if str(d)]
    hints = [str(h) for h in (state.get("expert_hints") or []) if str(h)]
    if not domains and not hints:
        return ""
    from backend.rag.domain import domain_label

    lines = ["【定向检索指引（当前以专家身份运行，必须遵守）】"]
    if domains:
        labels = "、".join(domain_label(d) or d for d in domains)
        lines.append(
            f"- 检索法规 / 类案 / 知识库时，将 domain 过滤参数优先设为：{labels}。"
            "（可结合问题实际场景，必要时以推断领域兜底，避免漏检。）"
        )
    if hints:
        lines.append(
            f"- 检索关键词建议附加以下专业线索，提升召回聚焦度：{'、'.join(hints)}。"
        )
    lines.append("- 用户点名要求 / 精确命中的内容不受领域过滤限制，仍须完整引用并标注来源。")
    return "\n".join(lines)


def _report_system(state: LegalState) -> str:
    """报告入口的系统提示：专家人设 + 专属回答模板 + 定向检索指引 + 基础纪律 + 意图路由的市场技能（方案 B）。

    报告是回答链路的最终定型节点，启用/关闭市场技能必须在此真实改变回答：
    按意图（intent）只注入与本次任务相关的已启用技能，
    模型按技能能力范围组织报告内容，不再全量注入造成答案缝合。

    专家增强（可选，全空时行为与普通助手逐字一致）：
    - ``answer_template``：专属回答模板，经 ``system_with_persona``
      插在人设与基础纪律之间，约束回答组织骨架；
    - ``expert_domains`` / ``expert_hints``：定向检索指引，提示生成节点
      在调用检索工具时按专家领域过滤、并附加专业检索词。
    """
    base = system_with_persona(
        state.get("expert_persona", ""),
        answer_template=state.get("expert_template", ""),
    )
    guide = _expert_retrieval_guide(state)
    if guide:
        base = f"{base}\n\n{guide}"
    mkt = market_skill_context(
        state.get("user_id", ""),
        intent=state.get("intent", ""),
        query=state.get("query", ""),
    )
    if not mkt:
        return base
    return f"{base}\n\n{mkt}"


def _expert_signature(state: LegalState) -> str:
    """专家落款：专家版在回答末尾追加一段可肉眼辨识的署名与领域声明。

    仅当专家存在且配置了 domains 时输出；无专家 / 未配置 domains 时返回空串，
    普通助手行为逐字不变。落款在免责声明之前拼接，与免责声明互不干扰。
    """
    domains = [str(d) for d in (state.get("expert_domains") or []) if str(d)]
    if not domains:
        return ""
    try:
        from backend.rag.domain import domain_label
    except Exception:
        domain_label = lambda d: d  # noqa: E731
    labels = "、".join(domain_label(d) or d for d in domains[:3])
    return f"\n\n---\n\n> 本意见由所选专家出具，以「{labels}」领域定向检索为依据。"


def _apply_expert_polish(state: LegalState, text: str) -> str:
    """报告 / 对话 / 直答统一收尾：追加专家落款；无专家或已含落款时原样返回。

    幂等判断以正文是否已含落款标记为准，避免缓存回放 / 重复调用时叠加。
    """
    sig = _expert_signature(state)
    if not sig or "本意见由所选专家出具" in text:
        return text
    return text + sig


async def report_node(state: LegalState, harness: Harness) -> LegalState:
    """汇总报告 + Skill 完整性校验 + 收尾。"""
    harness.step("ReportNode")

    # 1. Trace 先落 State：Skill 的判据读 state["trace"]，顺序不能反
    harness.flush_trace()

    # 2. Skill 完整性校验（唯一合适的调用点）
    intent = state.get("intent") or "consult"
    # 与法律无关的直答**不做 Skill 校验**：它没有作业路径可言，
    # 校验只会产出"缺少合同审查第 X 步"这类毫无意义的假缺口。
    if intent == "off_topic":
        harness.tracer.info("ReportNode", "off_topic", "非法律问题：跳过作业路径校验，直接作答")
        harness.flush_trace()
        state["confidence"] = compute_confidence(state)
        state["report"] = await _build_report(state, harness)
        _restamp_confidence(state, state["confidence"], compute_confidence(state))
        harness.finish()
        return state

    skill = skill_for_intent(intent)
    # 引用清单兜底：**必须在拼报告之前** —— 见 _ensure_citations 的说明
    _ensure_citations(state, harness)
    unmet = apply_skill_gaps(intent, state, harness)
    if skill is not None:
        complete = skill.completeness(state)
        harness.tracer.info(
            "ReportNode", "skill_audit",
            f"「{skill.name}」{len(skill.steps)} 步完成度 {complete:.0%}"
            + (f"；未完成关键步骤 {len(unmet)} 项" if unmet else ""),
        )
        if unmet:
            harness.ctx.note(f"作业路径未走完：{'；'.join(unmet[:3])}")

    # 3. 报告正文。**必须先算置信度再拼报告**：报告里的「置信度」段落读的是
    #    state["confidence"]，而它原先只在第 5 步 harness.finish() 里才写入，
    #    拼报告时读到的恒为 0 —— 检索再满、引用再干净也显示 0%。
    #
    #    但置信度**定稿**只能在报告落进 State 之后：``compute_confidence`` 的
    #    「内容完整性」看的是本次有没有产出交付物，而咨询/快速问答的交付物
    #    就是报告本身（DraftNode 只在要文书时才跑）。报告还没写进去就算，
    #    这一类任务那 0.10 分永远拿不到 —— 0.35 + 0.20 − 证据缺口就是全部，
    #    天花板压在缓存准入门槛之下，"相似问题直接命中缓存"永远触发不了。
    #
    #    所以：先算个初值供正文显示，定稿后再把正文里那一行改掉。
    #    报告上写的与落库的必须是同一个数 —— 同一次任务两处显示不同置信度，
    #    比分数偏低更难解释。
    state["confidence"] = compute_confidence(state)
    report = await _build_report(state, harness)
    report = _apply_expert_polish(state, report)
    state["report"] = report
    _restamp_confidence(state, state["confidence"], compute_confidence(state))

    # 4. 强制免责声明（方案原文要求"强制标注"）
    state["disclaimer"] = state.get("disclaimer") or settings.DISCLAIMER
    if settings.DISCLAIMER not in report:
        state["report"] = f"{report}\n\n---\n\n> {settings.DISCLAIMER}"

    # 5. 收尾：置信度、免责声明、落快照
    harness.finish()
    harness.tracer.info("ReportNode", "done",
                        f"报告 {len(state['report'])} 字，置信度 {state.get('confidence')}，"
                        f"证据缺口 {len(state.get('evidence_gap') or [])} 项")
    return state


# --------------------------------------------------------------------------- 报告
def _restamp_confidence(state: LegalState, before: float, after: float) -> None:
    """报告定稿后，用最终置信度改写正文里那一行，并把最终值写回 State。

    匹配的是**整行**（``- 综合置信度：**50%**``）而不是 ``**50%**``：
    正文的金额测算里出现一个孤立的加粗百分比完全正常，只替换片段会改错地方。
    找不到那一行（走直答路径的报告没有置信度段落）就地返回 —— 这不是错误。
    """
    state["confidence"] = after
    if abs(after - before) < 0.005:
        return
    old = f"- 综合置信度：**{before:.0%}**"
    text = state.get("report") or ""
    if old in text:
        state["report"] = text.replace(old, f"- 综合置信度：**{after:.0%}**", 1)


def _is_conversational(state: LegalState) -> bool:
    """本次交付物要不要走对话形态。

    缺省 ``report``：老任务重放（State 是快照恢复的，没有这个键）与另外三个
    产品入口都落在这里，行为与改动前逐字一致。
    """
    return str(state.get("answer_style") or "report") == "conversational"


#: 判定「证据池为空 / 覆盖率极低」用的条目有效字段 —— 与
#: :func:`_report_context_blocks` 的过滤口径**保持一致**（法条看 ``text``、
#: 指引看 ``snippet``、类案看 ``gist``/``case_name``）。口径分叉的后果是
#: "参考资料里一个块都没有、判定却说不空"，模型照常被叫起来编结论。
_EVIDENCE_TEXT_FIELDS: dict[str, tuple[str, ...]] = {
    "statutes": ("text",),
    "knowledges": ("snippet",),
    "cases": ("gist", "case_name"),
}

#: 参考资料稀缺提示：**证据非空但条数极少**时，由本节点在 Prompt 尾部注入一行
#: （不新增章节、不改四节正文结构）。模型看到「参考资料有限」就会把拿不准的
#: 结论写成证据缺口，而不是硬凑依据 —— 真机上问「我们的保险合同包括哪些」被
#: 答成社会保险/五险一金、还编造《环境保护法》《社会保险条例》的那一类幻觉，
#: 正是缺了这一行「检索到什么就答什么、检索不到就明说」的硬约束。
_SCARCE_EVIDENCE_HINT = (
    "参考资料有限，不足以支撑的结论须明确写出证据缺口，不得自行补充依据。"
)
#: 有效证据条数不超过该值时视为「覆盖率极低」（恰好只有 1~2 条依据）。
_SCARCE_EVIDENCE_MAX = 2


def _usable_evidence(state: LegalState) -> dict[str, list[dict[str, Any]]]:
    """本次实际可引用的检索证据（按参考资料块的过滤口径，不截断）。"""
    out: dict[str, list[dict[str, Any]]] = {}
    for kind, fields in _EVIDENCE_TEXT_FIELDS.items():
        out[kind] = [
            e for e in (state.get(kind) or [])
            if isinstance(e, dict) and any((e.get(f) or "").strip() for f in fields)
        ]
    return out


def _evidence_empty(state: LegalState) -> bool:
    """证据池为空或检索全部未命中：法条/本所指引/类案没有任何一条可用。"""
    return not any(_usable_evidence(state).values())


def _usable_evidence_count(state: LegalState) -> int:
    """有效证据总条数（用于「覆盖率极低」判定）。"""
    return sum(len(v) for v in _usable_evidence(state).values())


def _scarcity_note(state: LegalState) -> str:
    """证据非空但覆盖率极低时，返回要注入提示词的那一行；否则返回空串。"""
    total = _usable_evidence_count(state)
    return _SCARCE_EVIDENCE_HINT if 0 < total <= _SCARCE_EVIDENCE_MAX else ""


def _record_empty_evidence_gap(state: LegalState, harness: Harness) -> None:
    """证据池为空：如实登记一条证据缺口（``kind`` 取代码已有合法取值）。

    注意 `add_evidence_gap` 按 ``reason`` 去重（``_gap_key``），重复登记会被
    吞掉 —— 这里只在证据池为空的那一条路上调用，不会污染其它缺口。
    """
    harness.gaps(
        "analysis",
        "本次未检索到相关法规、本所指引与类案依据，未生成分析正文",
        "以下输出仅为检索结果的结构化汇总，不是分析意见；请补充资料或调整检索范围后重试",
    )


async def _build_report(state: LegalState, harness: Harness) -> str:
    """优先用 LLM 汇总（结构化部分仍由代码拼装，避免模型改写引用）。

    **直答路径不进报告模板**：非法律问题与"一句话能答完"的简单法律问题，
    走 :func:`_direct_answer`（见 prompts 里的 DIRECT/QUICK_ANSWER_PROMPT 说明）。
    它们本来就没有证据链可汇总，套报告模板只会逼模型编引用。
    """
    intent = state.get("intent") or "consult"
    if intent == "off_topic" or is_direct(intent):
        return await _direct_answer(state, harness)

    # 交付物形态分流（助手页 = 对话正文＋报告尾巴）。**放在直答之后**：
    # 直答本来就不套模板，形态对它没有意义，分流放前面会让直答白绕一圈。
    if _is_conversational(state):
        return await _build_conversational(state, harness)

    # 金额先由代码算好、作为 ``T1`` 资料块喂给模型（**与对话形态同一套**）。
    # 不喂的后果 2026-10-07 在真机上抓到了：类案检索页同一条 query，
    # 报告的「核心结论」写 **153,000 元**（模型自己按 9 个月折算），
    # 正文分析里又写 136,000，还留了一句"自我校对：上文误写为 153,000"——
    # 一份交付物里两个数，律师只看结论那行就会拿错数去谈判。
    calc = await _calc_block(state, harness)
    extra = [calc] if calc else None

    if await _llm_available():
        try:
            text = await _llm_report(state, harness, extra=extra)
            if text:
                return _assemble(state, body=text)
        except Exception as exc:  # noqa: BLE001
            harness.tracer.warn("ReportNode", "llm_report_failed",
                                f"LLM 汇总失败（{exc}），改用结构化拼装")
    return _assemble(state, body="")


async def _build_conversational(state: LegalState, harness: Harness) -> str:
    """对话式交付物：四节正文（模型）＋ 报告尾巴（代码）。

    与报告形态的两处结构性差别：

    1. **先算金额再写正文**。``damages_calc`` 的结果必须作为 ``T1`` 参考资料块
       进 Prompt —— 数字由代码给、模型只组织语言。**不让模型自己调工具**：
       ``llm_client.chat`` 是一次生成、不是 function-calling 通道，
       让模型"先请求计算、再复述结果"等于在这里塞一个 ReAct 循环，
       而**复述本身就是幻觉入口**（抄错一位数是最难发现的错）。
    2. 降级时**明确标注、不冒充分析**（见 :func:`_assemble_conversational`）。
    """
    calc = await _calc_block(state, harness)
    extra = [calc] if calc else None

    if await _llm_available():
        try:
            text = await _llm_conversational(state, harness, extra=extra)
            if text:
                # 结构兜底：五节不齐 / 顺序错 / 自拟标题 ⇒ 带上一版输出让模型改写一次
                # （模型无视 ASSISTANT_REPORT_PROMPT 结构约束、自行编出
                # 「行政违法/刑事犯罪/刑事免责/案件移送」这类分类小节时，
                # 不能只靠提示词——2026-10-13 实测约束在代码里、进程已重启，
                # 回答仍是自拟结构。能由代码钉死的不要交给模型。）
                text = await _ensure_conversational_structure(
                    state, harness, text, extra=extra)
                return _assemble_conversational(state, body=text)
        except Exception as exc:  # noqa: BLE001
            harness.tracer.warn("ReportNode", "llm_conversational_failed",
                                f"LLM 汇总失败（{exc}），改用结构化拼装")
    # 降级也**照样给金额**：``damages_calc`` 是纯函数，不依赖模型。
    # 文书那边已经栽过一次"降级后连算得出来的数都没了"—— 律师看到的是一份
    # 什么都齐、就是没有金额的报告，而这份报告看上去完全正常。
    return _assemble_conversational(state, body="", calc=calc)


async def _direct_answer(state: LegalState, harness: Harness) -> str:
    """直答：不套报告模板，最多一段话给出答复。

    两条刻意的区别对待：
    - ``off_topic``：**不加法律免责声明**。它不是法律意见，挂一句"仅供律师参考"
      会让律师误以为系统把闲聊也当成了法律结论，反而污染免责声明的严肃性；
    - ``quick_answer``：仍然检索过法条，所以保留免责声明，且引用纪律不变。
    """
    intent = state.get("intent") or "consult"
    query = (state.get("query") or "").strip()

    if not await _llm_available():
        # 没模型可用时**如实说**，不要拿规则引擎的抽取拼装冒充"回答"
        harness.gaps("report", "未连接模型服务，本次未生成答复文本",
                     "请检查推理服务（LLM_BASE_URL）后重试")
        return f"（未连接模型服务，无法回答：{query}）" if intent == "off_topic" else \
               f"（未连接模型服务，未能回答该问题：{query}）"

    prompt = (DIRECT_ANSWER_PROMPT if intent == "off_topic"
              else QUICK_ANSWER_PROMPT.format(disclaimer=settings.DISCLAIMER))
    try:
        res = await llm_client.chat(
            build_messages(_report_system(state),
                           prompt, _evidence_blocks(state)),
            task="direct_answer",
            payload={
                "intent": intent,
                "query": query,
                "statutes": state.get("statutes") or [],
                "knowledges": state.get("knowledges") or [],
            },
        )
        text = (res.text or "").strip()
    except Exception as exc:  # noqa: BLE001
        harness.tracer.warn("ReportNode", "direct_answer_failed", f"直答失败：{exc}")
        return ""

    if not text:
        return ""

    if intent == "off_topic":
        return text
    # 快速问答：免责声明是合规项，模型漏了就由代码补（不指望模型守规矩）
    if settings.DISCLAIMER not in text:
        text = f"{text}\n\n---\n\n> {settings.DISCLAIMER}"
    return text


def _evidence_blocks(state: LegalState) -> list[tuple[str, str]]:
    """直答也要带上检索到的法条 —— 快速问答只跑 StatuteNode，但那正是它的依据。"""
    blocks: list[tuple[str, str]] = []
    for s in (state.get("statutes") or [])[:8]:
        name = s.get("doc_name") or ""
        art = s.get("article_no") or ""
        text = (s.get("text") or "").strip()
        if not text:
            continue
        blocks.append((f"{name} {art}".strip() or "法规", text[:400]))
    return blocks


async def _llm_available() -> bool:
    """是否真的有模型可用。

    走 ``status()`` 的公开接口而不是读 ``_selected`` 私有字段：
    规则引擎 provider 的名字是 "rule"，它的输出是**抽取拼装**而非生成，
    直接拿它当"分析正文"会把结构化摘录当成分析，读起来像结论但不是结论。

    注意必须**先主动探测**再判断：直答路径（寒暄/快速问答）往往是进程
    启动后的第一个请求，此时 ``provider`` 还是"未探测"——只读缓存会把
    明明可用的网关误报成"未连接模型服务"（真机踩过：网关 HTTP 200，
    页面却提示未连接）。探测结果有缓存，命中时开销可忽略。
    """
    from backend.llm.client import llm_client as _c

    provider = await _c.ensure_provider()
    if provider.name == "rule":
        # 钉在兜底上：按间隔重探一次真模型（内部自带节流，不会每次都打网关）
        await _c.reprobe()
        provider = await _c.ensure_provider()
    return provider.name != "rule"


def _report_context_blocks(state: LegalState,
                           extra: list[tuple[str, str]] | None = None,
                           ) -> list[tuple[str, str]]:
    """报告的**参考资料块**：案情 + 法条 + 本所指引 + 类案 + 前序分析（+ 额外块）。

    真机踩过的坑：这里曾把 ``context_blocks`` 传成 ``None`` ——
    prompt 里写着"把上面的分析汇总"，但上面**什么都没有**：没有案情、
    没有法条、没有类案。模型的反应是如实拒绝（"未提供参考资料，
    无法生成金额测算"），或者更糟：凭系统提示里的【引用纪律】瞎猜条文号，
    然后《劳动合同法》第40条这种检索结果里根本没有的条文被 Validator
    一条条拦下来，整条"无来源不出引用"的红线空转。

    ``extra`` 是**追加块**（目前只有 ``T1`` 法定金额测算），排在最后：
    模型写到最后一段（二）具体分析时，权威数字就在眼前。
    保持同步函数 —— 它有多处调用与测试，别为了取一次性数据把它改成协程。

    ⚠️ **``S{i}`` / ``K{i}`` 的编号必须与前端卡片编号一致**（前端按
    ``data.statutes`` / ``data.knowledges`` 的**数组下标**造 ``S1``/``K1`` 卡片，
    见 ``web/app.js`` 的 ``answerShape``）。所以这里的 ``enumerate`` 一律
    直接遍历**切片后的原数组**，靠 ``continue`` 跳过没有正文的条目 ——
    ``i`` 跟着数组下标走，跳过的条目仍然占号。**谁把它改成
    ``enumerate([s for s in ... if s.get("text")], start=1)`` 就会整体错位**：
    症状是点 ``[S3]`` 跳到另一部法，静默、且看着像"检索不准"。
    有测试钉住这条（``tests/test_assistant_deliverable.py``）。
    """
    blocks: list[tuple[str, str]] = []

    # ---- 案情（用户陈述 + IntakeNode 提取的事实）
    facts = [f for f in (state.get("facts") or []) if str(f).strip()]
    if facts:
        blocks.append(("F1", "当事人陈述的案件事实：\n" + "\n".join(f"- {f}" for f in facts[:12])))
    q = (state.get("query") or "").strip()
    if q:
        blocks.append(("F2", f"用户提问原文：{q[:600]}"))

    # ---- 法条（已按效力状态过滤过，这里不再截断依据，只截断条文正文）
    for i, s in enumerate((state.get("statutes") or [])[:10], start=1):
        if not (s.get("text") or "").strip():
            continue
        blocks.append(format_statute_evidence(
            i, s.get("doc_name") or "", s.get("article_no") or "",
            s.get("status") or "现行有效", (s.get("text") or "")[:300],
            s.get("publisher") or "",
        ))

    # ---- 本所知识库指引（``K{i}``）。
    #
    # 为什么必须有这一段：前端会给 ``data.knowledges`` 造 ``K1``…``K{n}`` 卡片
    # （``web/app.js``），而这里**原先一个 K 块都不给** —— 模型看不见 ``K`` 编号，
    # 于是正文里要么不引指引（知识库白检索），要么自己瞎编一个 ``[K2]``
    # 指向一张别的卡片。编号规则与 statutes 逐字一致（切片后照原下标编号）。
    for i, k in enumerate((state.get("knowledges") or [])[:8], start=1):
        # 片段字段是 ``snippet``（``KnowledgeHit``，见 state.py）——
        # 不是 ``text``（那是法条）也不是 ``content``。取错键的表现是
        # 每个 K 块都是空正文，模型看不见任何指引内容。
        snippet = (k.get("snippet") or "").strip()
        if not snippet:
            continue
        blocks.append(format_knowledge_evidence(
            i, k.get("title") or "本所文件", k.get("doc_type") or "",
            k.get("version") or "未标注版本", snippet[:300],
        ))

    # ---- 类案
    for i, c in enumerate((state.get("cases") or [])[:6], start=1):
        if not (c.get("gist") or c.get("case_name") or "").strip():
            continue
        blocks.append(format_case_evidence(
            i, c.get("case_no") or "（无案号）", c.get("court") or "",
            c.get("judge_date") or "", c.get("cause_of_action") or "",
            (c.get("gist") or "")[:400],
        ))

    # ---- 前序分析（AnalysisNode 的结构化产出）
    issues = state.get("issues") or []
    strategy = state.get("strategy") or []
    if issues:
        lines = [f"- {x.get('title') or ''}：{x.get('description') or ''}（{('、'.join(x.get('basis') or []))}）"
                 for x in issues if isinstance(x, dict)]
        if lines:
            blocks.append(("A1", "争议焦点分析：\n" + "\n".join(lines)))
    if strategy:
        blocks.append(("A2", "攻防要点与策略：\n" + "\n".join(f"- {s}" for s in strategy[:10])))
    lc = state.get("limitation_check") or {}
    if lc.get("period"):
        blocks.append(("A3", f"时效核查：{lc.get('period')}｜起算：{lc.get('start_point') or '未确定'}"
                             f"｜结论：{lc.get('conclusion') or '需核实'}｜依据：{'、'.join(lc.get('basis') or [])}"))

    # ---- 风险点（合同审查）
    risks = state.get("risk_points") or []
    if risks:
        lines = [f"- {r.get('clause') or ''}（{r.get('level') or ''}）：{r.get('issue') or ''}"
                 for r in risks[:12] if isinstance(r, dict)]
        if lines:
            blocks.append(("R1", "合同风险点：\n" + "\n".join(lines)))

    # ---- 追加块（法定金额测算等），排在最后
    if extra:
        blocks.extend(extra)
    return blocks


# --------------------------------------------------------------------------- 金额测算
# 「法定金额测算」块的标题。**提示词按这个名字引用它**
# （``ASSISTANT_REPORT_PROMPT`` 规则 3：「金额一律照抄「法定金额测算」块」）——
# 改了这里必须同步改提示词，测试里钉了这条相等关系
# （``test_calc_block_title_matches_prompt``）。名字对不上的后果是
# 模型在参考资料里找不到"那个块"，于是自己估一个数出来，而它看起来很正常。
CALC_BLOCK_TITLE = "法定金额测算"

# 解除信号：出现这些词才值得去算金额。**没有信号时连缺口都不记** ——
# 概念题（"试用期最长几个月"）本来就没有金额可算，记一条"未测算"是噪音，
# 与 ``off_topic`` 跳过 Skill 校验同一个道理。
_TERMINATION_RE = _re.compile(
    r"违法解除|违法辞退|违法终止|无故辞退|强行辞退|被辞退|辞退|开除|劝退|裁员|"
    r"解除劳动合同|解除劳动关系|被解除|终止劳动合同"
)
_ILLEGAL_RE = _re.compile(r"违法|无故|强行|擅自|非法|未依法")

# 「数额」与「工资」二字的最大间距。**只认标签明确的数额** ——
# 与 ``template_service.rule_extract`` 同一条纪律：正则分不清哪笔钱归哪
# （"公司欠我 30000 元工资"里的 30000 是欠薪，不是月工资），认不出就留白、
# 并如实记一条缺口，**绝不猜**。
_WAGE_WORDS = ("工资", "薪资", "月薪", "月均", "月平均", "月收入", "平均工资",
               "劳动报酬", "报酬")
_WAGE_NEAR = 12
_ENTRY_WORDS = ("入职", "到岗", "上班", "开始工作", "参加工作")
# ⚠ 「不再上班」这类复合词不要放进 exit 表：判定是**先查 entry 再查 exit**，
#    它会被 entry 的「上班」先认走，把离职日期记成入职日期。
_EXIT_WORDS = ("离职", "辞退", "解除", "终止", "开除", "劝退", "裁员", "离开")
# 标签紧跟其后的裸数额（"月均 8500" —— 很多提问根本不带单位）。
# 末尾的负向断言排掉"工资发放至 2023-08-01"这类把日期当数额的形状。
_WAGE_BARE_RE = _re.compile(
    r"(?:" + "|".join(_WAGE_WORDS) + r")[^0-9\n]{0,8}?"
    r"([0-9][0-9,，]*(?:\.[0-9]{1,2})?)\s*(万元|万|元)?(?![年月日\-/.\d])"
)
# 只到月的日期（"2023年8月被辞退"）：真实提问里很常见。按当月 1 日计，
# 并**如实记一条缺口**（差一个月的边界可能差 0.5 个月，那是半个月工资）。
_YM_RE = _re.compile(r"((?:19|20)\d{2})\s*[年\-/.]\s*(\d{1,2})\s*月?(?![\d日])")
# 月工资的天花板：超过这个数的多半是欠薪总额/合同价款而不是月工资。
_WAGE_CEILING = 1_000_000.0


def _calc_source_text(state: LegalState) -> str:
    """测算要素的来源文本：提问原文 + IntakeNode 提取的事实。

    事实放前面：它通常已被 IntakeNode 规整成"入职…离职…工资…"的短句，
    比用户口语里的指代可靠。
    """
    facts = "\n".join(str(f) for f in (state.get("facts") or []) if str(f).strip())
    return f"{facts}\n{state.get('query') or ''}"


def _to_yuan(num: str, unit: str) -> float | None:
    """``12,000`` / ``1.2万`` → 数值（元）。解析不了或非正数返回 None。"""
    try:
        v = float(str(num).replace(",", "").replace("，", ""))
    except (TypeError, ValueError):
        return None
    if unit in ("万元", "万"):
        v *= 10000
    return v if v > 0 else None


def _pick_wage(text: str) -> tuple[float | None, str]:
    """抽出"月工资"数额。返回 ``(数额, 缺口说明)``；抽不出时数额为 None。

    **两个来源必须一致**：带单位的那一路（``MONEY_RE``，且前面 12 字内有
    「工资」类词）与不带单位的那一路（``月均 8500``）。两边给出不同的数
    说明这句话里有多个候选，此时**不算** —— 与其挑一个，不如让律师指明。
    """
    from backend.tools.material_tools import MONEY_RE

    found: dict[float, str] = {}
    for m in MONEY_RE.finditer(text):
        before = text[max(0, m.start() - _WAGE_NEAR):m.start()]
        if not any(w in before for w in _WAGE_WORDS):
            continue          # 标签不在附近：欠薪、合同价款、赔偿诉求金额都可能
        v = _to_yuan(m.group(1), m.group(2))
        if v:
            found[v] = m.group(0).strip()
    for m in _WAGE_BARE_RE.finditer(text):
        v = _to_yuan(m.group(1), m.group(2) or "")
        if v:
            found[v] = m.group(0).strip()

    if not found:
        return None, "未识别到月工资数额（请写明离职前十二个月的平均工资）"
    if len(found) > 1:
        shown = "、".join(f"{k:g} 元" for k in sorted(found))
        return None, f"识别到多个不一致的工资数额（{shown}），请指明以哪个为准"
    wage = next(iter(found))
    if wage > _WAGE_CEILING:
        return None, f"识别到的数额 {wage:g} 元不像月工资，请确认后重述"
    return wage, ""


def _date_family(text: str, start: int, end: int) -> str:
    """这个日期属于入职还是离职：取**紧邻**的事件词。

    先看日期**后面** 4 个字（中文习惯是"3月1日入职"、"8月被辞退"），
    再看前面 8 个字（"入职时间：2019-03-01"）。两处都没有事件词 ⇒ 空串。
    """
    after = text[end:end + 4]
    before = text[max(0, start - 8):start]
    for words, fam in ((_ENTRY_WORDS, "entry"), (_EXIT_WORDS, "exit")):
        if any(w in after for w in words):
            return fam
    for words, fam in ((_ENTRY_WORDS, "entry"), (_EXIT_WORDS, "exit")):
        if any(w in before for w in words):
            return fam
    return ""


def _pick_span(text: str) -> tuple[str, str, str, bool]:
    """抽出用工起止日期：``(入职, 离职, 缺口说明, 是否只精确到月)``。

    只认**日期紧挨着事件词**的写法（见 :func:`_date_family`）。抽不出就返回缺口 ——
    两个日期都在但不知道该谁是谁时，硬配就是把年限算反，而年限直接乘进金额。
    """
    from backend.tools.material_tools import DATE_RE

    entry = exit_ = ""
    approx = False
    full_spans = [(m.start(), m.end()) for m in DATE_RE.finditer(text)]
    for m in DATE_RE.finditer(text):
        fam = _date_family(text, m.start(), m.end())
        key = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        if fam == "entry" and not entry:
            entry = key
        elif fam == "exit" and not exit_:
            exit_ = key
    if not (entry and exit_):
        for m in _YM_RE.finditer(text):
            # 完整日期已经盖住的区间不再当"只到月"处理
            if any(s <= m.start() < e for s, e in full_spans):
                continue
            fam = _date_family(text, m.start(), m.end())
            key = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-01"
            if fam == "entry" and not entry:
                entry, approx = key, True
            elif fam == "exit" and not exit_:
                exit_, approx = key, True
    if entry and exit_:
        return entry, exit_, "", approx
    return entry, exit_, "未识别到明确的入职与离职日期（请写明具体日期，含年份）", approx


def _yuan(v: float) -> str:
    """金额显示：千分位、去掉无意义的小数尾巴（``76500`` → ``76,500``）。"""
    return f"{v:,.2f}".rstrip("0").rstrip(".") if v % 1 else f"{v:,.0f}"


def _render_calc_block(res: Any) -> str:
    """把 ``damages_calc`` 的结果渲染成 ``T1`` 参考资料块。"""
    data = res.data or {}
    lines = [f"{CALC_BLOCK_TITLE}（**由代码计算**：正文与表格里的金额必须与本块逐位一致）"]
    for it in data.get("items") or []:
        unit = it.get("unit") or ""
        value = it.get("value") or 0
        shown = f"{_yuan(value)} 元" if unit == "元" else f"{value:g} {unit}"
        lines.append(f"- {it.get('label')}：{it.get('formula')}｜{shown}"
                     + (f"｜{it['basis']}" if it.get("basis") else ""))
        if it.get("note"):
            lines.append(f"  （{it['note']}）")
    total = data.get("total")
    if total is not None:
        head = {"primary": "主请求金额", "sum": "合计"}.get(data.get("total_kind") or "",
                                                          "金额")
        tail = {"primary": "（与其余各行互为备选，不是相加关系）",
                "sum": "（各项之和）"}.get(data.get("total_kind") or "", "")
        lines.append(f"- **{head}：{_yuan(float(total))} 元**{tail}")
    for a in data.get("assumptions") or []:
        lines.append(f"- 口径：{a}")
    return "\n".join(lines)


async def _calc_block(state: LegalState, harness: Harness) -> tuple[str, str] | None:
    """法定金额测算 → ``T1`` 参考资料块。算不了就登记缺口并返回 None。

    **为什么由代码算完塞给模型、而不是让模型自己调工具**：``llm_client.chat``
    是一次生成、不是 function-calling 通道；让模型"先请求计算、再复述结果"
    等于在 ReportNode 里塞一个 ReAct 循环，而**复述本身就是幻觉入口**
    —— 抄错一位数是最难发现的错。数字由代码给，模型只负责组织语言。

    场景选择**取保守的那一个**：只有问句里明确出现「违法／无故」等字样时
    才按 2N（``termination_bundle``，三行备选）；否则只算 N
    （``economic_compensation``）。理由：2N 以"解除被认定违法"为前提，
    在没有该前提的提问里把它当主请求金额摆出来，是**替律师下结论**。
    """
    text = _calc_source_text(state)
    if not _TERMINATION_RE.search(text):
        return None

    wage, wage_problem = _pick_wage(text)
    entry, exit_, span_problem, approx = _pick_span(text)
    if wage is None or not (entry and exit_):
        harness.gaps("calc", "；".join(p for p in (wage_problem, span_problem) if p),
                     "本次未测算金额；补齐上述要素后重跑即可")
        return None
    if approx:
        harness.gaps("calc", "用工起止日期只精确到月（已按当月 1 日计）",
                     "折算月数可能有半个月的出入，请核对到日")

    scenario = ("termination_bundle" if _ILLEGAL_RE.search(text)
                else "economic_compensation")
    res = await harness.tool("damages_calc", AGENT, scenario=scenario,
                             monthly_wage=wage, start_date=entry, end_date=exit_)
    if not res.ok:
        harness.gaps("calc", res.error or "金额测算未完成",
                     getattr(res, "fallback_note", "") or "请补齐要素后重跑")
        return None

    months = (res.data or {}).get("years_months") or {}
    harness.tracer.info(
        "ReportNode", "damages_calc",
        f"法定金额测算（{scenario}）：{months.get('text') or ''}"
        f"｜折算 {months.get('compensated_months')} 个月"
        f"｜主请求 {(res.data or {}).get('total')} 元")
    return "T1", _render_calc_block(res)


async def _llm_report(state: LegalState, harness: Harness,
                      extra: list[tuple[str, str]] | None = None) -> str:
    # 证据池为空 / 检索全部未命中：**不调用 LLM**，直接返回空串复用降级分支
    # （``_assemble(state, body="")`` 会如实输出「结论摘要 / 无引用 / 证据缺口 /
    # 置信度」）。模型在零证据下编结论的路从源头断掉 —— 问「保险合同包括哪些」
    # 被答成社会保险、还编造本地语料不存在的《环境保护法》，正是缺了这道闸。
    if _evidence_empty(state):
        _record_empty_evidence_gap(state, harness)
        return ""

    prompt = REPORT_PROMPT.format(disclaimer=settings.DISCLAIMER)
    note = _scarcity_note(state)
    if note:
        prompt = f"{prompt}\n\n{note}"
    res = await llm_client.chat(
        build_messages(_report_system(state), prompt,
                       _report_context_blocks(state, extra=extra)),
        task="report",
        payload={
            "intent": state.get("intent") or "consult",
            "domain": state.get("domain") or "unknown",
            "statutes": state.get("statutes") or [],
            "cases": state.get("cases") or [],
            "knowledges": state.get("knowledges") or [],
            "risk_points": state.get("risk_points") or [],
            "issues": state.get("issues") or [],
            "strategy": state.get("strategy") or [],
            "evidence_gap": state.get("evidence_gap") or [],
            "disclaimer": settings.DISCLAIMER,
        },
    )
    if res.degraded:
        harness.tracer.warn("ReportNode", "llm_degraded",
                            "报告由确定性规则引擎汇总（未使用大模型）")
        # ⚠ 降级时**必须交白卷**，不能把 ``res.text`` 当"分析正文"交出去。
        #
        # ``res.text`` 这时是规则引擎 ``rule_engine._h_report`` 写的一整篇报告
        # （自带「一、结论摘要／二、引用清单／三、检索证据片段」），而调用方
        # ``_build_report`` 收到非空正文后会走 ``_assemble(state, body=text)``，
        # 把它整篇塞进**「二、分析说明」这一节**。于是交付物里出现两份报告：
        # 外面是代码拼的「法律辅助分析报告（法律咨询）」，里面套着规则引擎那份
        # 「法律辅助分析意见（consult）」—— 标题、编号、结论摘要各说一遍。
        # 真机上 2026-09-29 与 10-07 都出现过（全库 76 条 degraded 任务，
        # 66 条挂在真账号下），因为云端 403 是**间歇**的 ——
        # 模型偶尔不可用，不是"配错了"。
        #
        # 返回空串不是"什么都不产出"：``_assemble(state, body="")`` 会由代码
        # 生成同一套结构（结论摘要 / 引用清单 / 证据缺口），只出一份。
        return ""
    return res.text or ""


async def _llm_conversational(state: LegalState, harness: Harness,
                              extra: list[tuple[str, str]] | None = None) -> str:
    """对话式正文：一次生成、四节结构，参考资料块与报告形态**同一套**。

    降级处理与 :func:`_llm_report` 逐字同理（``res.degraded`` ⇒ 交白卷），
    理由见那边长长的那段注释 —— 规则引擎交出来的是一整篇报告，
    当成"正文"塞进四节里就是第二份报告。
    """
    # 证据池为空 / 检索全部未命中：**不调用 LLM**，直接返回空串复用降级分支
    # （``_assemble_conversational(state, body="")`` 会如实输出「未检索到相关依据 +
    # 检索结果结构化汇总」），并补记证据缺口。与 :func:`_llm_report` 逐字同理。
    if _evidence_empty(state):
        _record_empty_evidence_gap(state, harness)
        return ""

    prompt = ASSISTANT_REPORT_PROMPT.format(disclaimer=settings.DISCLAIMER)
    note = _scarcity_note(state)
    if note:
        prompt = f"{prompt}\n\n{note}"
    res = await llm_client.chat(
        build_messages(_report_system(state), prompt,
                       _report_context_blocks(state, extra=extra)),
        task="assistant_report",
        payload={
            "intent": state.get("intent") or "consult",
            "domain": state.get("domain") or "unknown",
            "answer_style": "conversational",
            "statutes": state.get("statutes") or [],
            "knowledges": state.get("knowledges") or [],
            "cases": state.get("cases") or [],
            "issues": state.get("issues") or [],
            "evidence_gap": state.get("evidence_gap") or [],
            "disclaimer": settings.DISCLAIMER,
        },
    )
    if res.degraded:
        harness.tracer.warn("ReportNode", "llm_degraded",
                            "分析正文由确定性规则引擎汇总（未使用大模型）")
        return ""
    return res.text or ""


# --------------------------------------------------------------------------- 五节结构兜底
#: 对话形态规定的五节标题（与 ASSISTANT_REPORT_PROMPT 逐字一致）。
_CONV_SECTION_TITLES: tuple[str, ...] = (
    "核心结论", "具体分析", "通俗化输出", "额外思考", "行动建议",
)
#: 五节标题的**整行**匹配：标题独占一行才算数（正文里"如上核心结论所述"
#: 这类引用不算）；允许前面带 Markdown 标题符（##）、加粗（**）、中文/数字编号，
#: 允许结尾带冒号或加粗收尾符。拿不到这一行的，一律视为"模型没写这一节"。
_CONV_SECTION_RE = _re.compile(
    r"^[ \t]*(?:#{1,4}[ \t]*|\*\*[ \t]*)?"
    r"(?:（[一二三四五]）|[一二三四五][、.．]|[0-9]+[、.．])?[ \t]*"
    r"(?P<title>核心结论|具体分析|通俗化输出|额外思考|行动建议)"
    r"(?:[ \t]*\*\*)?[ \t]*[:：]?[ \t]*$",
    _re.M,
)


def _conversational_first_seen(body: str) -> list[str]:
    """按出现顺序返回正文里命中过的合规五节标题（重复不计）。

    ``_assemble_conversational`` 自带"正文写了两遍 ⇒ 截成第一遍"的去重，
    所以重复段落**不算结构违规**，这里只看首次出现顺序。
    """
    seen: list[str] = []
    for m in _CONV_SECTION_RE.finditer(body or ""):
        t = m.group("title")
        if t in _CONV_SECTION_TITLES and t not in seen:
            seen.append(t)
    return seen


def _conversational_compliant(body: str) -> bool:
    """五节结构是否达标：五个标题全部出现，且首次出现顺序与规定一致。

    任一节缺失 / 顺序错乱（如先写「行动建议」再写「具体分析」）都判不合规 ——
    模型自拟的「行政违法 / 刑事犯罪 / 刑事免责 / 案件移送」等分类小节一个都
    匹配不上，同样判不合规，触发改写。
    """
    return _conversational_first_seen(body) == list(_CONV_SECTION_TITLES)


#: 改写指令：点名违规类型 + 五节逐字清单 + 上一版自拟内容并入「具体分析」。
#: 不让模型自由发挥 —— 上一版 7 次写裸 [T1] 的教训：能明确列出来的约束
#: 必须逐字列出来（见 _clean_body 的注释）。
_CONV_RESTRUCTURE_INSTRUCTION = """你上一版回答没有按照规定的五节结构组织：五节标题缺失、顺序错乱，或自拟了「行政违法 / 刑事犯罪 / 刑事免责 / 案件移送」等分类小节。
请严格按下面的五节结构重写，小节标题一字不差、顺序不乱、不得自拟或新增任何小节：
（一）核心结论
（二）具体分析
（三）通俗化输出
（四）额外思考
（五）行动建议

要求：
1. （二）具体分析 是你上一版所有分类内容的归处 —— 分类判断（如行政违法 / 刑事犯罪 / 刑事免责 / 案件移送）作为本节的子条列展开，不要再单独成节；
2. 开头先接住提问者具体情况，正文以「用户应该怎么办」为骨架；
3. 禁止堆砌法条原文，引用只能来自【参考资料】；
4. 只输出重写后的正文，不要任何开场白、解释或小结。"""


async def _ensure_conversational_structure(
    state: LegalState, harness: Harness,
    text: str, *, extra: list[tuple[str, str]] | None = None,
) -> str:
    """对话正文的五节结构兜底：不合规 ⇒ 带上一版输出改写一次，仍不合规用回原稿。

    **为什么要有这道闸**：ASSISTANT_REPORT_PROMPT 里"必须五节、禁止自拟小节"
    是**描述性约束**，模型服从性不足时会顺着 LEGAL_SYSTEM_PROMPT 的"结构清晰、
    条目化输出"自己长出分类小节（真机上 2026-10-13 截图：行政违法 / 刑事犯罪 /
    刑事免责 / 案件移送，框内仍是量刑档次罗列）。纯提示词压不住，必须由代码在
    生成后校验并修正 —— 与 ``_clean_body`` 拦截裸 [T1] 是同一类守卫。

    只改写一次：改写仍是同一套 LLM，没有理由比初稿更守规矩，第二次失败就用
    原稿（至少不丢内容），并在 Trace 里留证据。
    """
    if _conversational_compliant(text):
        return text

    harness.tracer.warn(
        "ReportNode", "conversational_structure_mismatch",
        "对话正文未按五节结构输出，触发一次改写重试",
    )
    try:
        messages = build_messages(
            _report_system(state),
            ASSISTANT_REPORT_PROMPT.format(disclaimer=settings.DISCLAIMER),
            _report_context_blocks(state, extra=extra),
        )
        messages.append({"role": "assistant", "content": text})
        messages.append({"role": "user", "content": _CONV_RESTRUCTURE_INSTRUCTION})
        res = await llm_client.chat(
            messages,
            task="assistant_report_restructure",
            payload={
                "intent": state.get("intent") or "consult",
                "answer_style": "conversational",
                "domain": state.get("domain") or "unknown",
                "statutes": state.get("statutes") or [],
                "knowledges": state.get("knowledges") or [],
                "cases": state.get("cases") or [],
                "issues": state.get("issues") or [],
                "evidence_gap": state.get("evidence_gap") or [],
            },
        )
        rewritten = (res.text or "").strip()
        if rewritten and not res.degraded and _conversational_compliant(rewritten):
            harness.tracer.info(
                "ReportNode", "conversational_structure_rewritten",
                "五节结构改写成功，采用改写稿",
            )
            return rewritten
        harness.tracer.warn(
            "ReportNode", "conversational_structure_rewrite_failed",
            "改写仍不合规或失败，保留原稿",
        )
    except Exception as exc:  # noqa: BLE001
        harness.tracer.warn("ReportNode", "conversational_structure_rewrite_error",
                            f"结构改写失败（{exc}），保留原稿")
    return text


#: 参考资料块标签里**没有对应卡片**的那些（``[T1]`` 是「法定金额测算」块）。
#: ``[S1]``/``[K1]`` 是**有**卡片的，绝不能删 —— 删了律师就点不动法条了。
_STRAY_BLOCK_MARK_RE = _re.compile(r"[ \t]*\[T\d+\]")


def _strip_stray_marks(body: str) -> str:
    """删掉没有卡片的块标签；**没有它时逐字节原样返回**（不碰别的空白）。"""
    if "[T" not in body:
        return body
    out: list[str] = []
    for line in body.split("\n"):
        new = _STRAY_BLOCK_MARK_RE.sub("", line)
        # 只在真的删掉标记的行上收尾空白（"136,000 [T1] |" ⇒ "136,000 |"），
        # 没标记的行原样留着 —— Markdown 的行尾两空格是硬换行，不能顺手抹掉。
        out.append(new.rstrip() if new != line else line)
    return "\n".join(out)


def _clean_body(body: str) -> str:
    """正文的公共守卫（两个形态共用，**行为与原 ``_assemble`` 逐字一致**）。

    ① 正文**本身就是一整篇报告** ⇒ 丢掉。它自带标题与全套小节编号，
       套进来就是"报告里套报告"（见 ``_llm_report`` 里那段注释，真机上出现过 76 次）。
       这一层是兜底 —— 降级那条路已在 ``_llm_report`` 里返回空串，
       但"整篇回吐"也可能来自模型自己。
    ② 模型自带"引用清单"小节 ⇒ 从这里截断（合规项由代码在后面补，
       模型看不到核验状态，清单必须以代码版本为准）。
    ③ 参考资料块的标签被当成卡片编号写进正文 ⇒ 删掉（只删没有卡片的那些）。
    ④ 模型把检索片段里的本地路径（``file://`` / ``C:\\...``）与解析器占位符
       （``[TABLE_002: 工资明细]``）照抄进正文 ⇒ 剥离。这是 2026-10 真机 bug
       （前端把 ``file://`` / ``[TABLE_xxx]`` 当附图渲染成"第二张照片"）的兜底：
       证据块在 prompts 层已清洗，这一层防"模型仍从其它路径抄到 / 历史缓存泄漏"。

    ⚠ ③ 为什么是**代码**删而不是只靠提示词：提示词里已逐字点名禁掉 ``[T1]``
    （``ASSISTANT_REPORT_PROMPT`` 规则 2），但 2026-10-07 实测**仍然写了 7 次** ——
    模型看见参考资料的 ``[T1]`` 块，就照着 ``[S1]`` 的样子写进正文，
    而界面上没有 T 卡、``web/app.js:827`` 的 ``citeChip`` 对未知标记**原样返回**，
    律师看到的是满篇裸的 ``[T1]``。能由代码钉死的不要交给模型。
    """
    import re

    body = (body or "").strip()
    if re.match(r"^#\s*法律辅助分析(报告|意见)", body):
        return ""
    m = re.search(r"^#{1,4}\s*引用清单.*$", body, re.M)
    if m:
        body = body[:m.start()].rstrip()
    return sanitize_evidence_text(_strip_stray_marks(body)).strip()


def _assemble(state: LegalState, *, body: str) -> str:
    """交付物拼装**分派器**：按 ``answer_style`` 选形态。

    为什么要分派而不是各自调用：``report_node`` 里 ``_assemble`` 是老名字，
    有多种调用方（含测试与降级兜底），改名会让"看不出走了哪条路"的风险变大。
    分派器只有一行判断，两个实现各自完整。
    """
    if _is_conversational(state):
        return _assemble_conversational(state, body=body)
    return _assemble_report(state, body=body)


def _assemble_report(state: LegalState, *, body: str, ) -> str:
    """拼装最终报告（**报告形态**：七节骨架）。

    **结构化段落由代码生成，不由模型生成** —— 引用清单、证据缺口、置信度这几段
    是合规要求项，让模型"复述"它们会引入改写与遗漏的风险。模型只负责写
    结论摘要与分析正文（``body``）。

    但当模型按新格式（REPORT_PROMPT）输出了**完整结构**（含「核心结论」「金额
    测算」等小节）时，正文本身已经是成稿 —— 这时再套一遍代码模板（结论摘要/
    攻防要点/时效核查）只会得到两份重复的分析。所以检测到完整正文时：
    模型正文为主，代码只保留**合规项**（引用清单、证据缺口），并截掉模型
    自己写的引用清单（它看不到核验状态，清单必须以代码版本为准）。
    """
    import re

    intent = state.get("intent") or "consult"
    body = _clean_body(body)

    # 模型是否已经输出了完整结构化正文
    full_body = bool(re.search(r"^##\s*(核心结论|金额测算|争议焦点)", body, re.M))

    parts: list[str] = [f"# 法律辅助分析报告（{_intent_label(intent)}）", ""]

    if full_body:
        parts.append(body)
        parts.append("")
        sec = "二"  # 代码段编号接在模型正文之后
        # 模型正文里已覆盖的小节不再重复
        has_strategy = bool(re.search(r"攻防要点|行动建议", body))
        has_lc = bool(re.search(r"时效|证据与期限", body))
    else:
        # ---- 结论摘要
        parts.append("## 一、结论摘要")
        parts.extend(f"- {line}" for line in _summary_lines(state))
        parts.append("")

        if body:
            parts.append("## 二、分析说明")
            parts.append(body)
            parts.append("")
            sec = "三"
        else:
            sec = "二"
        has_strategy = False
        has_lc = False

    # ---- 风险点（合同审查）
    if state.get("risk_points"):
        parts.append(f"## {sec}、风险点清单")
        parts.extend(_risk_lines(state))
        parts.append("")
        sec = _next(sec)

    if state.get("strategy") and not has_strategy:
        parts.append(f"## {sec}、攻防要点与策略")
        parts.extend(f"- {s}" for s in state["strategy"])
        parts.append("")
        sec = _next(sec)

    lc = state.get("limitation_check") or {}
    if lc.get("period") and not has_lc:
        parts.append(f"## {sec}、诉讼/仲裁时效核查")
        parts.append(f"- **期间**：{lc.get('period')}")
        parts.append(f"- **起算点**：{lc.get('start_point') or '（未确定）'}")
        parts.append(f"- **初步结论**：{lc.get('conclusion') or '（需核实）'}")
        if lc.get("basis"):
            parts.append(f"- **依据**：{'；'.join(lc['basis'])}")
        parts.append("")
        sec = _next(sec)

    # ---- 引用清单（合规要求项，必须由代码生成）
    parts.append(f"## {sec}、引用清单")
    parts.extend(_citation_lines(state))
    parts.append("")
    sec = _next(sec)

    # ---- 证据缺口
    parts.append(f"## {sec}、证据缺口与不确定性")
    parts.extend(_gap_lines(state))
    parts.append("")
    sec = _next(sec)

    # ---- 置信度
    parts.append(f"## {sec}、置信度")
    parts.extend(_confidence_lines(state))
    parts.append("")

    parts.extend(_disclaimer_lines())
    return "\n".join(parts)


def _assemble_conversational(state: LegalState, *, body: str,
                             calc: tuple[str, str] | None = None) -> str:
    """拼装最终交付物（**对话形态**：四节正文＋报告尾巴）。

    与报告形态共用同三个生成器（``_citation_lines`` / ``_gap_lines`` /
    ``_confidence_lines``）—— 合规项的两份写法迟早会分叉，所以只有一份。

    ⚠ **尾巴一律用不带中文数字的 ``##`` 标题，这里一个 ``_next()`` 都不调用。**
    正文那四节的编号（（一）…（四））由模型自己写；代码这边接不上号：
    ``_next("（二）")`` 里的 ``order.find`` 对全角括号返回 ``-1``，
    函数会**原样返回**（见 ``_next``），喂进去以后每一节都会叫同一个号。
    有测试钉住（``test_conversational_tail_has_no_chinese_numbering``）。

    裁掉的两节及理由：**诉讼/仲裁时效核查** 与 **攻防要点与策略** 已经分别作为
    ``A3`` / ``A2`` 参考资料块喂给模型了，正文（一）（二）（四）必然带出来 ——
    尾巴再列一遍就是同一件事说两遍。而 ``state["strategy"]`` / ``limitation_check``
    字段本身不动：案件分析页与合同审查页另在渲染它们。

    ``calc`` 只在**没有正文**（降级）时用到：模型不在了，代码算出来的那几个数
    照样要交出去（见 :func:`_build_conversational`）。
    """
    import re

    body = _clean_body(body)
    # 模型把四节写了两遍 ⇒ 只留第一遍。**两个标题都数够两遍才截**：
    # 只看"第二个核心结论"会把"（一）写重了、后面接着写（二）"这种
    # 前半段重复、后半段正常的情况一刀切掉，把真内容一起丢了。
    # 报告形态不需要这一条：那边的骨架本来就允许模型输出完整正文。
    dup_re = r"^\s*(?:#{1,4}\s*|\*\*)?\s*(?:（%s）|[一二三四]、)?\s*%s"
    heads = list(re.finditer(dup_re % ("一", "核心结论"), body, re.M))
    bodies = list(re.finditer(dup_re % ("二", "具体分析"), body, re.M))
    if len(heads) > 1 and len(bodies) > 1:
        body = body[:heads[1].start()].rstrip()
        body = body or ""

    parts: list[str] = []
    if body:
        parts.append(body)
        parts.append("")
    else:
        # **降级/无正文：明确标注，不冒充分析。** 报告形态在降级时还能交出一份
        # 「结论摘要」骨架，那是因为它本来就长成那样；对话形态没有正文就是没有正文，
        # 硬拿检索条数凑成四节，读起来像结论但一个字都不是分析。
        # 原因要**如实区分**：证据池为空是"没检索到相关依据"（不是模型的问题），
        # 否则才是"模型服务未连接或未返回可用内容" —— 两种情况的应对不同。
        reason = ("未检索到相关依据" if _evidence_empty(state)
                  else "模型服务未连接或未返回可用内容")
        parts.append(f"> ⚠ 本次未生成分析正文（{reason}）。"
                     "以下为**检索结果的结构化汇总，不是分析意见**，仅供定位事实与依据。")
        parts.append("")
        if calc:
            # 金额是代码算的，模型不在了也照样给。块的第 1 行是标题+口径说明
            # （那是给模型看的），这里换成章节标题，只取其后的条目行。
            parts.append(f"## {CALC_BLOCK_TITLE}")
            parts.extend((calc[1] or "").splitlines()[1:])
            parts.append("")
        parts.append("## 检索结果摘要")
        parts.extend(f"- {line}" for line in _summary_lines(state))
        parts.append("")

    # ---- 风险点清单：**仅在有值时出现**。咨询意图下它恒为空（等价于删掉），
    #      合同审查意图下它是交付物的主体，不能一起砍。
    if state.get("risk_points"):
        parts.append("## 风险点清单")
        parts.extend(_risk_lines(state))
        parts.append("")

    parts.append("## 引用清单")
    parts.extend(_citation_lines(state))
    parts.append("")
    parts.append("## 证据缺口与不确定性")
    parts.extend(_gap_lines(state))
    parts.append("")
    parts.append("## 置信度")
    parts.extend(_confidence_lines(state))
    parts.append("")
    parts.extend(_disclaimer_lines())
    return "\n".join(parts)


def _gap_lines(state: LegalState) -> list[str]:
    """证据缺口段落（**两个形态共用**，只出正文行，不带标题）。"""
    gaps = state.get("evidence_gap") or []
    if not gaps:
        return ["本次未记录证据缺口。"]
    out = [f"本次共记录 {len(gaps)} 项证据缺口："]
    for g in gaps[:15]:
        reason = g.get("reason") or ""
        impact = g.get("impact") or ""
        out.append(f"- **{_gap_label(g.get('kind') or '')}**：{reason}"
                   + (f"（影响：{impact}）" if impact else ""))
    if len(gaps) > 15:
        out.append(f"- …另有 {len(gaps) - 15} 项，详见结构化输出")
    return out


def _confidence_lines(state: LegalState) -> list[str]:
    """置信度段落（**两个形态共用**）。

    ⚠ 第一行必须与 :func:`_restamp_confidence` 匹配的字符串**逐字一致** ——
    那是报告定稿后回填最终置信度的锚点，改一个字，报告上就会永远留着初值。
    """
    conf = float(state.get("confidence") or 0.0)
    return [
        f"- 综合置信度：**{conf:.0%}**（低于 {settings.CITATION_MIN_CONFIDENCE:.0%} 时"
        f"须律师逐项复核后再采信）",
        f"- 已检索：法条 {len(state.get('statutes') or [])} 条、"
        f"类案 {len(state.get('cases') or [])} 个、"
        f"本所指引 {len(state.get('knowledges') or [])} 份",
        f"- 引用终审：{_check_label(state.get('citation_check') or 'pending')}",
    ]


def _disclaimer_lines() -> list[str]:
    """免责声明（**两个形态共用**）：强制标注，方案原文要求。"""
    return ["---", f"> {settings.DISCLAIMER}"]


def _summary_lines(state: LegalState) -> list[str]:
    lines: list[str] = []
    statutes = state.get("statutes") or []
    cases = state.get("cases") or []
    if statutes:
        valid = sum(1 for s in statutes if (s.get("status") or "") == "现行有效")
        lines.append(f"检索到 {len(statutes)} 条相关法条，其中标注现行有效 {valid} 条。")
    else:
        lines.append("本次法规检索未命中有效条文，结论范围相应缩小。")

    if state.get("risk_points"):
        rps = state["risk_points"]
        high = sum(1 for r in rps if r.get("level") == "high")
        lines.append(f"审查识别风险点 {len(rps)} 个，其中高风险 {high} 个"
                     f"（必须修改）。")
    if state.get("missing_clauses"):
        lines.append(f"提示可能缺失的条款 {len(state['missing_clauses'])} 项。")

    if cases:
        lines.append(f"检索到 {len(cases)} 个类案可作裁判倾向参照，"
                     f"其中 {sum(1 for c in cases if c.get('verified'))} 个通过真实性核验。")
    else:
        lines.append("本次类案检索未取得有效支撑，相关判断缺少类案依据。")

    if state.get("issues"):
        lines.append("归纳争议焦点 " + "；".join(
            (i.get("title") or "") for i in state["issues"][:4]) + "。")

    check = state.get("citation_check") or "pending"
    if check == "passed":
        lines.append("引用终审通过，全部引用均可回溯到本次检索证据。")
    elif check == "failed":
        summary = state.get("citation_summary") or {}
        lines.append(f"**引用终审未通过**：{summary.get('blocking', 0)} 条引用被拦截，"
                     f"详见引用清单中的标注。")
    else:
        lines.append("本次未执行引用终审，报告中的引用未经核验。")
    return lines


def _risk_lines(state: LegalState) -> list[str]:
    order = {"high": 0, "medium": 1, "low": 2}
    labels = {"high": "高（必须改）", "medium": "中（建议改）", "low": "低（提示性）"}
    out: list[str] = []
    for i, rp in enumerate(sorted(state["risk_points"], key=lambda r: order.get(r.get("level"), 9)), start=1):
        head = f"**{i}. {rp.get('clause_no') or '全文'} {rp.get('clause_title') or ''}**".strip()
        out.append(f"{head}｜等级：{labels.get(rp.get('level'), rp.get('level') or '')}")
        if rp.get("risk"):
            out.append(f"   - 风险：{rp['risk']}")
        if rp.get("suggestion"):
            out.append(f"   - 建议：{rp['suggestion']}")
        basis = [b for b in (rp.get("basis") or []) if b]
        out.append(f"   - 依据：{'；'.join(basis) if basis else '（需补充法律依据）'}")
    return out


def _ensure_citations(state: LegalState, harness: Harness) -> int:
    """给 ``state["citations"]`` 为空的路径回填"未核验"的引用条目。

    ⛔ 为什么非要补：``_citation_lines`` 照着空清单写"本次未取得可引用的法条、
    类案或知识库来源，**不生成任何引用**"，而同一份报告的置信度段落写着
    "已检索：法条 6 条" —— 律师拿到的是自相矛盾的报告，
    且真实检索到的依据被白白丢掉。

    补齐的条目一律 ``verified=False``（含义是"未回溯到正文表述"），
    由 ``_citation_lines`` 归入「不可引用（仅供定位问题）」一节：
    补的目的是**不让来源消失**，绝不是冒充已核验。

    ``reason`` 必须区分两种"清单为空"，否则报告会撒谎：

    - ``citation_check`` 仍是 ``pending`` → 终审**没跑**（路径里没有核验节点），
      此时才该说"未走引用终审"；
    - ``passed`` / ``failed`` → 终审**跑了**，只是本轮结论正文没有逐条引用具体条文。
      早先这里不分青红皂白都写"未走引用终审"，而终审一旦在更多路径上跑起来，
      报告就会对着一条已经核过的依据说"没人核过" —— 这是把真的说成假的，
      和把假的说成真的同样伤。

    方案A（2026-10-12）：**case 回填区分处理**。``case_node._verify_case_nos``
    已给 ``state["cases"]`` 写入 ``verified``（``case_no_verify`` 的 exists），
    回填时不再一律标 False：

    - 预校验通过（``c["verified"] is True``）且终审无拦截（``citation_check``
      为 ``passed``，或 ``citation_summary.blocking == 0``）→ 回填
      ``verified=True``，reason 用中性参考文案（检索命中 + 真实性核验通过，
      但正文未逐条回溯，作参考引用）；
    - 预校验未通过（exists=False / 校验工具不可用）或终审存在拦截 →
      保持 ``verified=False``，reason 用核验失败文案。

    statute 与 knowledge 回填逻辑保持原样不动（仍 ``verified=False``），
    终审的 blocking 拦截逻辑不得削弱。
    """
    if state.get("citations"):
        return 0

    check = state.get("citation_check") or "pending"
    summary = state.get("citation_summary") or {}
    blocked = int(summary.get("blocking") or 0) > 0
    if check in ("passed", "failed"):
        statute_reason = ("本轮结论正文未逐条引用具体条文；本条为本次检索到的依据，"
                          "效力状态已随终审核验，但未回溯到正文表述，故不标为可引用")
    else:
        statute_reason = "未走引用终审（本次路径不含核验节点），须人工核验后方可写入文书"
    case_ok_reason = "检索命中且类案真实性核验通过，正文未逐条回溯，作参考引用"
    case_fail_reason = ("该案号未通过类案真实性核验（预校验未通过或校验工具不可用），"
                        "须人工核验后方可写入文书")

    out: list[dict[str, Any]] = []
    for s in state.get("statutes") or []:
        out.append({"type": "statute", "verified": False,
                    "name": s.get("doc_name") or "",
                    "article": s.get("article_no") or "",
                    "status": s.get("status") or "未知",
                    "reason": statute_reason})
    for c in state.get("cases") or []:
        pre_ok = bool(c.get("verified"))
        if pre_ok and not blocked:
            out.append({"type": "case", "verified": True,
                        "case_no": c.get("case_no") or "",
                        "name": c.get("case_name") or "",
                        "court": c.get("court") or "",
                        "reason": case_ok_reason})
        else:
            out.append({"type": "case", "verified": False,
                        "case_no": c.get("case_no") or "",
                        "name": c.get("case_name") or "",
                        "court": c.get("court") or "",
                        "reason": c.get("reason") or case_fail_reason})
    for k in state.get("knowledges") or []:
        out.append({"type": "knowledge", "verified": False,
                    "name": k.get("title") or "",
                    "reason": statute_reason})
    if not out:
        return 0

    state["citations"] = out
    harness.tracer.info("ReportNode", "citations_backfilled",
                        ("已按本次检索来源回填 %d 条依据（正文未逐条引用，故标为不可引用）"
                         % len(out)) if check in ("passed", "failed")
                        else f"本次未走引用终审，已按已检索来源回填 {len(out)} 条未核验引用")
    return len(out)


def _failed_reason(reason: str) -> bool:
    """真实核验失败判据：与 validator / case_tools / statute_tools 写回的失败原文对齐。

    方案B（2026-10-12）：只有命中真实失败原文的条目才留在「不可引用」区，
    「正文未逐条回溯 / 核验未跑 / 参考引用」等非失败条目归入「参考依据」区，
    避免把"核验过但没写进正文"的条目渲染成"未通过校验"。
    """
    if not reason:
        return False
    return any(k in reason for k in (
        "未找到该案号", "案号未能核验", "校验工具不可用", "疑似编造案号",
        "未在本次检索的类案证据中", "已废止", "不得作为裁判依据",
        "未通过类案真实性核验",
    ))


def _is_backfilled_reference(c: dict[str, Any]) -> bool:
    """回填/未逐条回溯的参考引用：verified=True 但 reason 含参考回溯字样。"""
    r = c.get("reason") or ""
    return "未逐条回溯" in r or "作参考引用" in r


def _citation_lines(state: LegalState) -> list[str]:
    """引用清单：代码生成，逐条标注可核验状态。

    这是报告里**唯一**有对外效力的部分（对方律师会拿它来核），所以格式必须稳定，
    不能由模型自由发挥。三区分列，让律师一眼看出哪些能用、哪些只能参考：

    - 「可引用」：终审核验通过（含正文逐条引用）；
    - 「参考依据」：检索命中 + 真实性核验通过，但正文未逐条回溯 / 核验未跑
      （方案B新增，非删除线）；
    - 「不可引用」：真实核验失败（案号查无、工具不可用、已废止等），删除线。
    """
    citations = state.get("citations") or []
    if not citations:
        return ["本次未取得可引用的法条、类案或知识库来源。**本报告不生成任何引用**。"]

    ok = [c for c in citations if c.get("verified") and not _is_backfilled_reference(c)]
    refs = [c for c in citations
            if (not c.get("verified") and not _failed_reason(c.get("reason") or ""))
            or (c.get("verified") and _is_backfilled_reference(c))]
    failed = [c for c in citations
              if not c.get("verified") and _failed_reason(c.get("reason") or "")]

    lines: list[str] = []
    if ok:
        lines.append("### 可引用（已通过核验）")
        for c in ok:
            lines.append(f"- {_cite_text(c)}"
                         + (f"｜{c['reason']}" if c.get("reason") else ""))
    if refs:
        lines.append("### 参考依据（核验通过但正文未逐条回溯，供参考）")
        for c in refs:
            lines.append(f"- {_cite_text(c)}｜{c.get('reason') or '参考引用'}")
    if failed:
        lines.append("### 不可引用（未通过核验，仅供定位问题）")
        for c in failed:
            lines.append(f"- ~~{_cite_text(c)}~~｜{c.get('reason') or '未通过核验'}")

    rejected = (state.get("citation_summary") or {}).get("rejected") or []
    if rejected:
        lines.append("### 已拦截的引用")
        lines.extend(f"- {r}" for r in rejected)
    return lines


def _cite_text(c: dict[str, Any]) -> str:
    t = c.get("type") or "statute"
    if t == "statute":
        return f"{c.get('name') or ''}{c.get('article') or ''}" + (
            f"（{c['status']}）" if c.get("status") else "")
    if t == "case":
        return "｜".join(x for x in [c.get("case_no") or "", c.get("name") or "",
                                     c.get("court") or ""] if x)
    return f"{c.get('name') or ''}（本所知识库）"


def _next(sec: str) -> str:
    order = "一二三四五六七八九十"
    i = order.find(sec)
    return order[i + 1] if 0 <= i < len(order) - 1 else sec


def _intent_label(intent: str) -> str:
    from backend.agents.harness.router import INTENT_ROUTES

    return (INTENT_ROUTES.get(intent) or {}).get("label", intent)


def _gap_label(kind: str) -> str:
    return {"calc": "金额测算缺口",
            "statute": "法规证据缺口", "case": "类案证据缺口", "knowledge": "作业标准缺口",
            "material": "材料缺口", "vlm": "图像识别缺口", "tool": "工具缺口",
            "citation": "引用核验缺口", "review": "审查完整性缺口",
            "analysis": "分析完整性缺口", "document": "文书缺口",
            "memory": "记忆归档缺口", "skill": "作业路径缺口"}.get(kind, kind or "其他缺口")


def _check_label(check: str) -> str:
    return {"passed": "通过", "failed": "未通过（部分引用已被拦截）",
            "pending": "未执行（引用未经核验）"}.get(check, check)


def report_digest(state: LegalState) -> dict[str, Any]:
    return {
        "length": len(state.get("report") or ""),
        "confidence": state.get("confidence") or 0.0,
        "citation_check": state.get("citation_check") or "pending",
        "evidence_gap": len(state.get("evidence_gap") or []),
        "has_disclaimer": settings.DISCLAIMER in (state.get("report") or ""),
    }
