"""ValidatorNode：引用终审（方案 4.1 Citation Validator）。

原文约束：「终审：条文号/版本/效力、案号存在性逐条核对，**校验不过直接拦截，
不进入输出**」。这是三条反幻觉红线里唯一"硬拦"的一环。

本节点只做一件薄事：把 State 交给 Harness 的 ``validate_citations()``，
然后**决定流程怎么走**：

- ``citation_check == "passed"`` → 放行；
- ``citation_check == "failed"`` → 拦截。**拦截不等于抛异常终止**：
  已经核过的引用留在 ``state["citations"]`` 里（每条的 ``verified``/``reason``
  都写清楚为什么不可信），下游 DraftNode / ReportNode 据此把不可信引用剔除，
  而不是让整个任务失败 —— 律师仍需要那份"哪些依据不能用"的清单。

回环由 ``graph.py`` 负责：本节点把失败信息写进 State 与 Trace，图根据
``citation_check`` 决定是重试检索（回 Statute/Case 节点）还是带缺口交付。

**为什么不在本节点内部重试检索**：重试需要重新规划检索式，那是 Planner 的职责；
Validator 只做判定。职责混在一起会让"终审"变得可以自己给自己开后门。
"""

from __future__ import annotations

import logging

from backend.agents.harness import Harness
from backend.agents.state import LegalState

logger = logging.getLogger("legal_agent.nodes.validator")

AGENT = "validator"

# 终审失败的拦截提示：给律师看的措辞，不是给程序看的错误码
BLOCK_NOTE = ("引用终审未通过：部分法条/案号无法在本案检索证据中核验，"
              "已在引用清单中逐条标注，相关表述不得作为正式法律意见引用")
PASS_NOTE = "引用终审通过：全部引用均可回溯到本次检索证据"


async def validator_node(state: LegalState, harness: Harness) -> LegalState:
    """引用终审。"""
    harness.step("ValidatorNode")

    pending, why = _validatable(state)
    if not pending:
        # 没有任何待校验内容：不能谎报"通过"，也不能报"失败"。
        # 标 pending 是最诚实的 —— 但路由上不该走到这里（Draft/Review 在后）。
        harness.tracer.warn("ValidatorNode", "no_content", f"无待校验内容（{why}）")
        harness.gaps("citation", "本次没有需要校验的引用内容",
                     "若预期应产出文书或审查意见，说明上游节点未产出内容，需排查")
        state["citation_check"] = "pending"
        harness.snapshot()
        return state

    result = await harness.validate_citations()
    passed = result.passed
    state["citation_check"] = "passed" if passed else "failed"

    if passed:
        harness.ctx.note(PASS_NOTE)
        harness.tracer.info("ValidatorNode", "passed",
                            f"核对 {result.checked} 条，通过 {result.verified} 条"
                            + (f"，提示 {len(result.warnings)} 条" if result.warnings else ""))
    else:
        # 拦截必须同时出现在 notes（给律师的报告里）与 evidence_gap（结构化输出里）
        harness.ctx.note(BLOCK_NOTE)
        harness.tracer.error("ValidatorNode", "blocked",
                             f"拦截 {result.blocking} 条不可信引用："
                             f"{'；'.join(result.rejected[:5])}")
        harness.gaps("citation",
                     f"引用终审拦截 {result.blocking} 条不可信引用",
                     "被拦截的引用已在引用清单中标注，须以可核验依据重述后方可对外使用")
        # 已通过的引用也要点名保留，避免下游"全部剔除"
        kept = [c for c in result.citations if c.get("verified")]
        if kept:
            harness.tracer.info("ValidatorNode", "partial_pass",
                                f"保留 {len(kept)} 条已核验引用，剔除 "
                                f"{len(result.citations) - len(kept)} 条未通过项")

    # 拦截名单单独留一份，供 ReportNode 生成"需人工核实清单"
    state["citation_summary"] = {
        **(state.get("citation_summary") or {}),
        **result.to_summary(),
    }

    harness.snapshot()
    return state


def validator_digest(state: LegalState) -> dict:
    s = state.get("citation_summary") or {}
    return {
        "check": state.get("citation_check") or "pending",
        "checked": s.get("checked", 0),
        "verified": s.get("verified", 0),
        "blocking": s.get("blocking", 0),
        # 证据池核验数：正文没有引用时，这是"终审确实核过"的唯一凭据
        "pool_statutes": s.get("pool_statutes", 0),
        "inspected": s.get("inspected", 0),
        "rejected": s.get("rejected") or [],
        "warnings": s.get("warnings") or [],
    }


def _validatable(state: LegalState) -> tuple[bool, str]:
    """有没有"需要终审核对"的内容。

    **判据必须与 ``CitationValidator`` 实际核验的范围一一对应**，
    一共两类，缺一不可：

    1. **正文里的引用表述** —— 扫描范围见 ``_collect_mentions`` / ``_free_texts``
       （draft / revision_draft / report / risk_points / strategy / issues）。
       原先这里只看 draft / report / risk_points，而咨询与案件分析的结论在
       ``strategy``（攻防要点）与 ``issues[].basis``（争议焦点依据）里，
       于是那一整类任务被判成"无待校验内容"、终审直接跳过，
       报告里却写着「本次未执行引用终审」——
       律师拿到一份引用了《劳动法》第21条、却没有任何人核过这条能不能用的意见。

    2. **检索到的证据池本身** —— 见 ``_verify_statutes``。
       核的是"条文号/版本/效力"（validator_node 开头引的方案原文），
       这件事与"正文有没有引用它"无关。缺了这一类，**没有正文的链路就必然空转**：
       快速问答（statute → report）的结论正文在终审那一刻还没生成，
       ``strategy`` 也可能被动态规划裁掉，六项检查全空 → 明明检索到了 14 条法条、
       却判定"无可校验内容"，于是引用未经核验就交付、置信度拿不到引用那 0.25 分。

    这条不变式值得单独说：**收集范围与闸门判据分处两个文件时，
    只要有人扩了一边而忘了另一边，终审就会静默变成空转**，
    而日志里只有一句看起来无害的 "no_content"。
    """
    checks = [
        ("文书草稿", (state.get("draft") or "").strip()),
        ("修改对照稿", (state.get("revision_draft") or "").strip()),
        ("报告", (state.get("report") or "").strip()),
        ("风险点依据", state.get("risk_points")),
        ("攻防要点", state.get("strategy")),
        ("争议焦点依据", [i for i in (state.get("issues") or []) if i.get("basis")]),
        # 证据池：终审的第二类对象，与正文无关
        ("检索到的法条", state.get("statutes")),
        ("检索到的类案", state.get("cases")),
    ]
    for label, value in checks:
        if value:
            return True, label
    return False, "无草稿、无报告、无风险点依据、无攻防要点，也未检索到法条或类案"
