"""类案检索 Skill —— 把 4.1 Case Agent 的三条硬约束固化成步骤。

约束来自方案 4.1 与三条红线：
- 检索 **≤2 次**（防死循环第二层，由 ``ToolExecutor.guard_quota`` 强制）；
- 提炼裁判要旨，且**不得改写法院原话的实质**；
- 案号/案例名真实性核验 —— 核验不通过的判例只能用于理解裁判思路，
  **不得在文书中作为正式引用**（第 4 步的判据就是卡这条）。

第 4 步之所以要求有"核验动作"而不是"全部核验通过"：本项目类案库里的
93 个典型案例**全部没有案号**（公开典型案例的普遍情况）。要求"全部通过"
会让这个 Skill 永远无法完成，反而逼着下游去放宽判据 —— 那才是真的危险。
所以判据是「核验跑过 且 不可引用项已明确标记」，把风险点暴露给律师，
而不是假装干净。
"""

from __future__ import annotations

from backend.agents.state import LegalState
from backend.skills.base import SkillSpec, SkillStep, final_note, register


# --------------------------------------------------------------------------- 判据
def _has_issue(state: LegalState) -> bool:
    """第 1 步：案由或争议焦点至少有一个，否则检索式无从构造。"""
    if state.get("issues"):
        return True
    text = f"{state.get('query') or ''} {state.get('material_text') or ''}"
    return any(k in text for k in ("纠纷", "案由", "争议", "仲裁", "诉讼"))


def _has_cases(state: LegalState) -> bool:
    return bool(state.get("cases"))


def _has_gist(state: LegalState) -> bool:
    """第 3 步：至少提炼出一段裁判要旨。

    只判 ``cases`` 非空不够 —— 召回回来的判例如果只有标题没有要旨，
    律师拿去也写不进意见书。
    """
    return any((c.get("gist") or "").strip() for c in (state.get("cases") or []))


def _verify_done(state: LegalState) -> bool:
    """第 4 步：核验跑过，且每一项都有明确的 verified 标记。"""
    cases = [c for c in (state.get("cases") or [])[:10]]
    if not cases:
        return False
    if not all("verified" in c for c in cases):
        return False
    # 不可引用的项必须在证据缺口里留痕，不能只是字段上悄悄标个 False
    unverifiable = sum(1 for c in cases if not c.get("verified"))
    if unverifiable == 0:
        return True
    return any("核验" in (g.get("reason") or "") for g in (state.get("evidence_gap") or []))


def _citation_passed(state: LegalState) -> bool:
    return (state.get("citation_check") or "") == "passed"


# --------------------------------------------------------------------------- Skill
CASE_RESEARCH_SKILL = register(
    SkillSpec(
        id="case_research",
        name="类案检索 Skill",
        description="按案由与争议焦点检索类案、提炼裁判要旨、核验判例真实性，"
                    "核验不通过的判例只可用于理解裁判思路、不得作为正式引用",
        intents=["case_search", "case_analysis", "consult"],
        prompt_task="case_analysis",
        inputs={
            "query": "str=检索问题或争议焦点",
            "cause_of_action": "str=案由（如劳动合同纠纷），缺省自动推断",
            "court_level": "str=法院层级（最高法/高院/中院），可选",
            "issues": "list=争议焦点（可选，缺省由 IntakeNode 归纳）",
            "case_id": "str=关联案件",
        },
        outputs=["cases", "strategy", "limitation_check", "citations", "citation_check"],
        steps=[
            SkillStep(
                no=1, name="确定案由与争议焦点",
                action="从材料与提问中归纳案由及争议焦点，作为检索式的构造依据",
                agent="intake", node="IntakeNode",
                required=True, produces=("issues",),
                check=_has_issue,
                absent_note="未确定案由或争议焦点，类案检索式将缺乏针对性",
            ),
            SkillStep(
                no=2, name="类案检索（≤2 次）",
                action="按案由/争议焦点/法院层级检索指导性案例与典型判例，检索次数上限 2",
                agent="case", tool="case_search", node="CaseNode",
                required=True, produces=("cases",),
                check=_has_cases,
                absent_note="本次类案检索未取得有效结果，相关判断缺少类案依据",
            ),
            SkillStep(
                no=3, name="提炼裁判要旨",
                action="从判例全文中提炼裁判要旨、裁判结果与裁判理由，不得改写实质",
                agent="case", tool="case_summarize", node="CaseNode",
                required=True, produces=("cases",),
                check=_has_gist,
                absent_note="召回判例缺少裁判要旨，无法用于撰写意见书",
            ),
            SkillStep(
                no=4, name="核验判例真实性并标记可引用性",
                action="按案号或「案例名+来源」核验存在性；核验不通过者标记为不可正式引用",
                agent="case", tool="case_no_verify", node="CaseNode",
                required=True, produces=("cases", "evidence_gap"),
                check=_verify_done,
                absent_note="判例真实性未核验，文书中不得出现未核验的案号",
            ),
            SkillStep(
                no=5, name="引用终审",
                action="逐条核对案号存在性与法条效力，未通过即拦截输出",
                agent="validator", tool="case_no_verify", node="ValidatorNode",
                required=True, produces=("citations", "citation_check"),
                check=_citation_passed,
                absent_note=final_note("检索结果"),
            ),
        ],
    )
)


def case_research_skill() -> SkillSpec:
    return CASE_RESEARCH_SKILL
