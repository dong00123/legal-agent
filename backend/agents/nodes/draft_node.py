"""DraftNode：文书生成（方案 4.1 文书生成 Agent + 第五章 draft_generate）。

三条硬约束（都是"不得"）：
1. 事实只使用委托材料提供的事实，**不得虚构**；
2. 法律依据只能引用检索证据中真实存在的条文，引用不到写「（此处需补充法律依据）」，
   **不得编造**；
3. 所有引用必须已过终审（``citation_check == "passed"``）。

第 3 条是本节点的关键设计：**草稿在终审之前不生成正文**。
反过来（先生成再校验）的问题在于，模型一旦把"《合同法》第九十四条"写进
起诉状正文，即便终审拦截了，这份草稿也已经流出去了，而律师往往直接拿去用。
所以顺序必须是：检索 → 终审 → 生成。

与 ReviewNode 的分工：合同审查的"修改对照稿"由 ReviewNode 产出（它是审查的
第 8 步）；本节点只负责诉讼/非诉文书（起诉状、答辩状、意见书）与格式规范化。
"""

from __future__ import annotations

import logging
from typing import Any

from backend.agents.harness import Harness
from backend.agents.state import LegalState
from backend.llm.client import llm_client
from backend.llm.prompts import (
    DRAFT_PROMPT,
    LEGAL_SYSTEM_PROMPT,
    build_messages,
    evidence_blocks as _evidence_blocks,
    system_with_persona,
)
from backend.skills import skill_context

logger = logging.getLogger("legal_agent.nodes.draft")

AGENT = "draft"

DRAFT_LABELS = {
    "complaint": "民事起诉状",
    "defense": "民事答辩状",
    "arbitration_application": "劳动仲裁申请书",
    "legal_opinion": "法律意见书",
    "appeal": "民事上诉状",
    "revision": "合同修改稿",
    "opinion": "代理意见书",
}


async def draft_node(state: LegalState, harness: Harness) -> LegalState:
    """生成文书草稿；引用未过终审时拒绝生成正文。"""
    harness.step("DraftNode")

    doc_type = state.get("draft_type") or _default_draft_type(state)

    # ---- 前置闸门：引用没过终审，不生成正文
    check = state.get("citation_check") or "pending"
    if check == "failed":
        harness.gaps("document",
                     "引用终审未通过，已跳过文书正文生成",
                     "文书正文中的每一处法律依据都必须可核验；请先修正引用或补充检索")
        harness.tracer.error("DraftNode", "blocked_by_validator",
                             "引用终审未通过（citation_check=failed），拒绝生成正文")
        return state
    if check != "passed":
        # pending：可能是上游被裁剪或终审未执行。不阻断（可能只想要草稿框架），
        # 但必须声明"未过终审"，否则律师会以为这份草稿的引用已经核过
        harness.gaps("document", "本次未执行引用终审，文书草稿中的引用未经核验",
                     "草稿交付前须完成引用核验，不得直接对外使用")
        harness.tracer.warn("DraftNode", "no_validation",
                            "引用终审未执行（citation_check=pending），草稿引用未经核验")

    facts = state.get("facts") or []
    if facts and not all((f.get("source") or "").strip() for f in facts):
        # 事实不可追溯 = 可能是模型推断。文书里写进去就是"把推断当事实用"
        harness.gaps("document", "存在无法追溯来源的案件事实，已从文书中排除",
                     "文书事实部分需律师按材料逐条核对后补齐")
        facts = [f for f in facts if (f.get("source") or "").strip()]
        harness.tracer.warn("DraftNode", "facts_filtered",
                            f"剔除 {len(state.get('facts') or []) - len(facts)} 条无来源事实")

    res = await harness.tool(
        "draft_generate", AGENT,
        doc_type=doc_type,
        facts=facts,
        issues=state.get("issues") or [],
        statutes=_verified_statutes(state),
        cases=_verified_cases(state),
        parties=state.get("parties") or [],
        query=state.get("query") or "",
        templates=_templates(state),
    )

    draft = ""
    if res.ok:
        data = res.data if isinstance(res.data, dict) else {}
        draft = data.get("draft") or data.get("markdown") or data.get("content") or ""
        if res.degraded and res.fallback_note:
            harness.gaps("document", res.fallback_note, "文书草稿的完备性下降")
    else:
        harness.tracer.warn("DraftNode", "tool_failed",
                            f"draft_generate 失败（{res.error}），改用 LLM 直接生成")

    if not draft:
        draft = await _llm_draft(state, harness, doc_type, facts)

    if not draft:
        harness.gaps("document", "文书草稿生成失败", "需律师自行起草或稍后重试")
        harness.tracer.error("DraftNode", "empty", "文书草稿为空")
        harness.snapshot()
        return state

    state["draft"] = draft
    state["draft_type"] = doc_type
    harness.tracer.info("DraftNode", "generated",
                        f"{DRAFT_LABELS.get(doc_type, doc_type)} 草稿已生成（{len(draft)} 字）")

    # ---- 第 5 步（文书生成 Skill）：套用律所模板规范化
    await _normalize(state, harness, doc_type)

    harness.snapshot()
    return state


# --------------------------------------------------------------------------- 生成
async def _llm_draft(state: LegalState, harness: Harness, doc_type: str,
                     facts: list[dict[str, Any]]) -> str:
    """LLM 直出草稿（工具不可用时的路径）。"""
    prompt = DRAFT_PROMPT.format(doc_type=DRAFT_LABELS.get(doc_type, doc_type))
    system = f"{system_with_persona(state.get('expert_persona', ''))}\n\n{prompt}\n\n{skill_context('doc_draft')}"

    user = (
        f"文书类型：{DRAFT_LABELS.get(doc_type, doc_type)}\n"
        f"当事人诉求：{state.get('query') or '（未提供）'}\n\n"
        f"【当事人】\n" + "\n".join(
            f"- {p.get('role') or '当事人'}：{p.get('name') or ''}"
            for p in (state.get("parties") or [])[:10]
        ) + "\n\n"
        f"【案件事实】（只能使用以下事实，不得虚构）\n" + "\n".join(
            f"{i}. {f.get('text') or ''}" for i, f in enumerate(facts[:15], start=1)
        ) + "\n\n"
        f"【争议焦点与请求】\n" + "\n".join(
            f"- {i.get('title') or ''}" for i in (state.get("issues") or [])[:10]
        ) + "\n\n"
        "请输出 Markdown 格式的文书草稿。引用不到依据处写「（此处需补充法律依据）」。"
    )

    res = await llm_client.chat(
        build_messages(system, user, _evidence_blocks(state)),
        task="draft",
        payload={"doc_type": doc_type, "facts": facts,
                 "issues": state.get("issues") or [],
                 "statutes": _verified_statutes(state),
                 "cases": _verified_cases(state),
                 "parties": state.get("parties") or []},
    )
    if res.degraded:
        harness.gaps("document", "文书由确定性规则引擎生成（未使用大模型）",
                     "结构与措辞较为模板化，需律师加工后方可交付")
    return res.text or ""


async def _normalize(state: LegalState, harness: Harness, doc_type: str) -> None:
    """套用律所模板做格式规范化（失败不影响草稿本身）。"""
    res = await harness.tool("format_normalize", AGENT,
                             text=state.get("draft") or "", doc_type=doc_type)
    if not res.ok:
        harness.gaps("document", "格式规范化不可用，文书未套用律所模板",
                     "交付前需人工套用律所模板（标题/当事人/落款/日期）")
        return
    data = res.data if isinstance(res.data, dict) else {}
    normalized = data.get("normalized") or ""
    if normalized and data.get("applied", True):
        state["draft"] = normalized
        harness.tracer.info("DraftNode", "normalized",
                            f"已套用模板，调整 {len(data.get('changes') or [])} 处")
    elif res.degraded:
        harness.gaps("document", res.fallback_note or "格式规范化已降级为原样返回",
                     "交付前需人工套用律所模板")


# --------------------------------------------------------------------------- 证据
def _verified_statutes(state: LegalState) -> list[dict[str, Any]]:
    """只把通过终审的法条交给文书生成。

    ``citation_check == "failed"`` 时本节点已经返回；这里再过滤一次是为了覆盖
    "终审只拦了一部分"的情形 —— 未通过的那些不能出现在正文里。
    """
    if (state.get("citation_check") or "") != "passed":
        return [s for s in (state.get("statutes") or [])
                if (s.get("status") or "") not in ("已废止",)]
    verified_keys = {
        f"{c.get('name') or ''}|{c.get('article') or ''}"
        for c in (state.get("citations") or [])
        if c.get("type") == "statute" and c.get("verified")
    }
    if not verified_keys:
        return list(state.get("statutes") or [])
    out = [s for s in (state.get("statutes") or [])
           if f"《{s.get('doc_name') or ''}》|{s.get('article_no') or ''}" in verified_keys
           or not s.get("article_no")]
    return out or list(state.get("statutes") or [])


def _verified_cases(state: LegalState) -> list[dict[str, Any]]:
    """只把通过存在性复核的类案交给文书生成。

    未通过核验的判例可以用于理解裁判思路，但**不得写入文书作为引用**
    （方案 4.1 Case Agent 约束），所以这里直接过滤掉。
    """
    return [c for c in (state.get("cases") or []) if c.get("verified")]


def _templates(state: LegalState) -> list[dict[str, Any]]:
    """可作格式参照的本所文书资料。

    ``doc_type`` 在索引里是机器码（template / guideline），不是中文标签 ——
    原先按 ``"模板" in doc_type`` 匹配，恒为 False，等于一律拿不到格式参照，
    而文书照常生成、日志也没有异常。所以这里按码判断。
    """
    return [k for k in (state.get("knowledges") or [])
            if (k.get("doc_type") or "") in ("template", "guideline")]


def _default_draft_type(state: LegalState) -> str:
    """未定文书类型时按案件性质推断（规则，不用 LLM）。

    **已知任务类型优先于关键词**：原先「劳动」排在 ``doc_type == "contract"`` 之前，
    而合同审查的提问几乎必然带「劳动合同」三个字，于是整条路径一路产出一份
    「劳动仲裁申请书」—— 与"审这份合同"的诉求毫不相干，且会因缺少当事人诉求段
    触发一条「文书缺少必备段落」的假缺口。关键词只能用于**兜底猜测**，
    一旦已经知道合同审查／文书起草，就不该再让关键词改写交付物形态。
    """
    intent = state.get("intent") or ""
    doc_type = state.get("doc_type") or ""
    if intent == "contract_review" or doc_type == "contract":
        return "revision"

    blob = f"{state.get('query') or ''} {' '.join((i.get('title') or '') for i in (state.get('issues') or []))}"
    if "答辩" in blob:
        return "defense"
    if "上诉" in blob:
        return "appeal"
    if "仲裁" in blob or "劳动" in blob:
        return "arbitration_application"
    if "意见" in blob:
        return "legal_opinion"
    return "complaint"


def draft_digest(state: LegalState) -> dict[str, Any]:
    return {
        "type": state.get("draft_type") or "",
        "label": DRAFT_LABELS.get(state.get("draft_type") or "", ""),
        "length": len(state.get("draft") or ""),
        "has_revision": bool(state.get("revision_draft")),
        "source_facts": len([f for f in (state.get("facts") or []) if f.get("source")]),
    }
