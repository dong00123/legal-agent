"""Skill 包：律所办案指引的可执行封装（方案第六章）。

对外只暴露四件事：

- :data:`SKILLS` / :func:`get_skill` / :func:`list_skills` —— Skill 注册表；
- :func:`audit_skill` —— 用当前 State 判定某个 Skill 的 9 步/6 步走完没有；
- :func:`apply_skill_gaps` —— 把未完成的关键步骤写进证据缺口（节点收尾时调用）；
- :func:`skill_context` —— 给 LLM 的"固定路径"提示块，避免每次手写 Prompt。

**为什么 Skill 不是 Prompt**：Prompt 里的流程说明无法回答"这一步做了没有"。
Skill 的每一步都带一个读 State 的判据，所以它可以被审计、被回归测试、
可以被前端逐条打勾。这才是方案第六章说的「固化成标准流程」。
"""

from __future__ import annotations

import logging
from typing import Any

from backend.agents.state import LegalState
from backend.skills.base import (
    SKILLS,
    SkillSpec,
    SkillStep,
    StepStatus,
    audit_skill,
    get_skill,
    has_skill,
    list_skills,
    register,
    skills_for_doc_type,
    skills_for_intent,
)

# 导入即注册（下面的模块在 import 时调用 register()）
from backend.skills.case_research import CASE_RESEARCH_SKILL  # noqa: E402
from backend.skills.contract_review import CONTRACT_REVIEW_SKILL  # noqa: E402
from backend.skills.doc_draft import DOC_DRAFT_SKILL  # noqa: E402

logger = logging.getLogger("legal_agent.skills")

__all__ = [
    "SKILLS",
    "SkillSpec",
    "SkillStep",
    "StepStatus",
    "apply_skill_gaps",
    "audit_skill",
    "get_skill",
    "has_skill",
    "list_skills",
    "register",
    "skill_context",
    "skill_for_intent",
    "skills_for_doc_type",
    "skills_for_intent",
    "CASE_RESEARCH_SKILL",
    "CONTRACT_REVIEW_SKILL",
    "DOC_DRAFT_SKILL",
]


# --------------------------------------------------------------------------- 与流程协作
def skill_for_intent(intent: str) -> SkillSpec | None:
    """一个意图对应哪个 Skill。多个匹配时取第一个（注册顺序即优先级）。"""
    found = skills_for_intent(intent or "")
    return found[0] if found else None


def apply_skill_gaps(intent: str, state: LegalState, harness: Any) -> list[str]:
    """节点收尾时调用：把该意图对应 Skill 的未完成关键步骤登记为证据缺口。

    只在**流程结束前**调用一次（ReportNode），不要在中间节点调用 ——
    中间节点调用会把"还没轮到"误报成"没做"。
    """
    skill = skill_for_intent(intent)
    if skill is None:
        return []
    notes = skill.apply_gaps(state, harness)
    if notes:
        logger.info("Skill %s 有 %d 个关键步骤未完成，已登记证据缺口", skill.id, len(notes))
    return notes


def skill_context(intent: str) -> str:
    """生成给 LLM 的固定路径提示块。

    与 ``backend.llm.prompts.CONTRACT_REVIEW_PROMPT`` 的分工：
    那边的 Prompt 写"怎么审"（审查维度与输出 Schema），这里给"按哪几步走"。
    两者拼在一起就是完整作业标准，改动 Skill 步骤不需要同步改 Prompt 文本。
    """
    skill = skill_for_intent(intent)
    if skill is None:
        return ""
    lines = [f"【本所作业路径：{skill.name}】"]
    for st in skill.steps:
        mark = "（必要）" if st.required else "（可选）"
        lines.append(f"{st.no}. {st.name}{mark}：{st.action}")
    lines.append("要求：不要跳步；某一步无法完成时，明确写出「该步骤未完成」及其影响，不要假装完成。")
    return "\n".join(lines)


def skills_status() -> dict[str, Any]:
    """Skill 运行态，供 /health 与管理后台展示。"""
    return {
        "total": len(SKILLS),
        "skills": [
            {"id": s.id, "name": s.name, "steps": len(s.steps),
             "required_steps": sum(1 for x in s.steps if x.required),
             "intents": s.intents,
             "tools": sorted({x.tool for x in s.steps if x.tool})}
            for s in SKILLS.values()
        ],
    }
