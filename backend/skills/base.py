"""Skill 基础设施：把律所办案指引固化成**可校验的步骤表**。

方案第六章的定位很明确：Skill「本质是把作业标准和律师经验固化成标准流程」。
所以这里刻意**不**把 Skill 做成"一段更长的 Prompt" —— Prompt 无法回答
"这一步到底做没做"。一个 Skill 由三部分组成：

1. :class:`SkillStep` 步骤表：编号、动作说明、承担者（Agent）、绑定工具、
   以及**完成判据** ``check``（一个读 State 的纯函数）；
2. :class:`SkillSpec` 的输入/输出契约：输入字段与产出字段，
   让调用方（节点/API/前端）知道该喂什么、能拿到什么；
3. :func:`SkillSpec.audit`：跑完流程后回看 State，逐条判定步骤完成度。
   未完成且 ``required=True`` 的步骤会变成**证据缺口**，而不是被静默略过。

为什么"完成判据"要用 State 字段而不是日志：State 是唯一的事实来源，
日志会滚掉、Trace 会采样，只有 State 是流程的输入输出本身。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from backend.agents.state import LegalState

logger = logging.getLogger("legal_agent.skills")

__all__ = [
    "SkillStep", "StepStatus", "SkillSpec", "SKILLS",
    "register", "get_skill", "has_skill", "skills_for_intent",
    "skills_for_doc_type", "list_skills", "audit_skill", "final_note",
]


# --------------------------------------------------------------------------- 步骤
@dataclass
class SkillStep:
    """办案指引中的一个步骤。"""

    no: int
    name: str
    action: str                                   # 给律所看的作业说明
    agent: str = ""                               # 承担该步骤的 Agent
    tool: str = ""                                # 绑定的 MCP Tool（空=由 LLM/节点承担）
    node: str = ""                                # 对应的 LangGraph 节点
    required: bool = False                        # 未完成是否算证据缺口
    produces: tuple[str, ...] = ()                # 产出的 State 字段
    check: Callable[[LegalState], bool] | None = None
    # 未完成时给律师的说明。可以是字符串，也可以是读 State 的函数 ——
    # 有些步骤的"没完成"本身就有两种含义，必须分开说：
    # 引用终审 "pending"（压根没跑）与 "failed"（跑了没过）不是一回事，
    # 用同一句"终审未通过"去描述 pending，等于凭空告诉律师一个不存在的结论。
    absent_note: str | Callable[[LegalState], str] = ""


@dataclass
class StepStatus:
    """一步的完成判定结果。

    ``kind`` 携带"这一步缺的是什么类型的证据"。它必须在这里定下来，
    而不是事后从步骤定义再推一遍 —— :meth:`SkillSpec.missing_required` 返回的是
    StepStatus，拿不到 SkillStep；原先 ``apply_gaps`` 就因此对着 StepStatus
    取 ``.tool``，一跑就抛 ``AttributeError: 'StepStatus' object has no attribute 'tool'``。
    """

    no: int
    name: str
    done: bool
    required: bool
    detail: str = ""
    kind: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"no": self.no, "name": self.name, "done": self.done,
                "required": self.required, "detail": self.detail, "kind": self.kind}


@dataclass
class SkillSpec:
    """一个可执行 Skill 的完整声明。"""

    id: str
    name: str
    description: str
    intents: list[str] = field(default_factory=list)   # 适用意图
    inputs: dict[str, str] = field(default_factory=dict)
    outputs: list[str] = field(default_factory=list)
    steps: list[SkillStep] = field(default_factory=list)
    prompt_task: str = ""                              # 交给 LLM 时的 task 名
    doc_types: list[str] = field(default_factory=list)  # 适用文书类型

    # ------------------------------------------------------------------ 校验
    def audit(self, state: LegalState) -> list[StepStatus]:
        """逐条判定步骤完成度。

        没有 ``check`` 的步骤一律判定为"未完成"——这不是懒，而是刻意：
        一个步骤如果连判据都写不出来，它就不是可固化的作业标准，
        不应该出现在 Skill 里。宁可让它显示未完成，也不要假装走过了。
        """
        out: list[StepStatus] = []
        for st in self.steps:
            kind = self._gap_kind(st)
            if st.check is None:
                out.append(StepStatus(st.no, st.name, False, st.required,
                                      "未定义完成判据（该步骤不可自动校验）", kind))
                continue
            try:
                done = bool(st.check(state))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skill %s 步骤 %d 判据执行异常：%s", self.id, st.no, exc)
                done = False
            detail = ""
            if not done:
                detail = self._note_of(st, state)
            out.append(StepStatus(st.no, st.name, done, st.required, detail, kind))
        return out

    @staticmethod
    def _note_of(step: SkillStep, state: LegalState) -> str:
        """取该步骤未完成的说明（支持按 State 动态生成）。"""
        note = step.absent_note
        if callable(note):
            try:
                return str(note(state)) or "该步骤未产出所需内容"
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skill 步骤 %d 的 absent_note 生成失败：%s", step.no, exc)
                return "该步骤未产出所需内容"
        return note or "该步骤未产出所需内容"

    def completeness(self, state: LegalState) -> float:
        """完成度 = 已完成步骤 / 总步骤。等权重，因为每个步骤都是作业标准的一环。"""
        statuses = self.audit(state)
        if not statuses:
            return 0.0
        return round(sum(1 for s in statuses if s.done) / len(statuses), 3)

    def missing_required(self, state: LegalState) -> list[StepStatus]:
        return [s for s in self.audit(state) if s.required and not s.done]

    def to_dict(self, state: LegalState | None = None) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "intents": self.intents,
            "doc_types": self.doc_types,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "prompt_task": self.prompt_task,
            "steps": [
                {"no": s.no, "name": s.name, "action": s.action, "agent": s.agent,
                 "tool": s.tool, "node": s.node, "required": s.required,
                 "produces": list(s.produces)}
                for s in self.steps
            ],
            "step_count": len(self.steps),
        }
        if state is not None:
            d["audit"] = [s.to_dict() for s in self.audit(state)]
            d["completeness"] = self.completeness(state)
        return d

    # ------------------------------------------------------------------ 与 Harness 协作
    def apply_gaps(self, state: LegalState, harness: Any) -> list[str]:
        """把未完成的关键步骤登记为证据缺口。

        这一步是 Skill 存在的**主要收益**：如果 Skill 只声明不校验，
        那和写在文档里的流程说明没有区别 —— 律师仍然不知道哪一步没走。
        返回被登记的缺口列表，便于接口层直接回显。
        """
        notes: list[str] = []
        for st in self.missing_required(state):
            note = st.detail or f"「{self.name}」第 {st.no} 步「{st.name}」未完成"
            notes.append(note)
            if harness is not None:
                harness.gaps(st.kind or "skill", note,
                             "该步骤属本所作业标准的必要环节，缺失会使结论的完备性下降")
        return notes

    def _gap_kind(self, step: SkillStep) -> str:
        """按步骤绑定的工具推断证据缺口类型，以便前端分类展示。"""
        tool = step.tool or ""
        if tool.startswith("statute"):
            return "statute"
        if tool.startswith("case"):
            return "case"
        if tool.startswith("knowledge"):
            return "knowledge"
        if tool.startswith("material"):
            return "material"
        return "skill"


def final_note(what: str):
    """生成「引用终审」这一步的未完成说明。

    这一步的"未完成"有两种完全不同的含义，必须分开说：

    - ``pending``：终审**压根没执行**（本次流程没产生待校验内容，或没走到 ValidatorNode）；
    - ``failed``：跑了，并且拦下了引用。

    用同一句"引用终审未通过"去描述 pending，是凭空告诉律师一个不存在的结论 ——
    而这句话会出现在每份咨询报告的证据缺口清单里；
    反过来把 failed 说成"未执行"，又会掩盖真实发生的拦截。
    两种失真都会让整份清单失去可信度，所以这里按 ``citation_check`` 分别措辞。
    """

    def _note(state: LegalState) -> str:
        check = state.get("citation_check") or "pending"
        if check == "failed":
            n = int((state.get("citation_summary") or {}).get("blocking") or 0)
            return (f"引用终审未通过（拦截 {n} 条不可信引用），{what}不得作为正式交付物输出，"
                    f"相关表述须以可核验依据重述")
        if check == "pending":
            return (f"本次未执行引用终审，{what}中的引用未经核验，"
                    f"对外使用前须逐条人工核对")
        return f"引用终审未确认通过，{what}不得作为正式交付物输出"

    return _note


# --------------------------------------------------------------------------- 注册表
SKILLS: dict[str, SkillSpec] = {}


def register(skill: SkillSpec) -> SkillSpec:
    if skill.id in SKILLS:
        raise RuntimeError(f"Skill 重复注册：{skill.id}")
    SKILLS[skill.id] = skill
    return skill


def get_skill(skill_id: str) -> SkillSpec:
    if skill_id not in SKILLS:
        raise KeyError(f"未注册的 Skill：{skill_id}；可用：{', '.join(SKILLS)}")
    return SKILLS[skill_id]


def has_skill(skill_id: str) -> bool:
    return skill_id in SKILLS


def skills_for_intent(intent: str) -> list[SkillSpec]:
    return [s for s in SKILLS.values() if intent in s.intents]


def skills_for_doc_type(doc_type: str) -> list[SkillSpec]:
    return [s for s in SKILLS.values() if doc_type and doc_type in s.doc_types]


def list_skills() -> list[dict[str, Any]]:
    return [s.to_dict() for s in SKILLS.values()]


def audit_skill(skill_id: str, state: LegalState) -> dict[str, Any]:
    """给接口用：带完成度的 Skill 视图。"""
    return get_skill(skill_id).to_dict(state)
