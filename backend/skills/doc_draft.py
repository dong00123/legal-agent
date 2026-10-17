"""文书生成 Skill —— 起草类交付物的作业标准。

方案第五章 ``draft_generate`` 的三条硬约束 + 第 4 条红线要求：
1. 结构符合法定文书格式（首部/事实与理由/法律依据/请求事项/尾部）；
2. **事实部分只使用用户提供的事实，不得虚构**；
3. 法律依据只能引用检索证据中真实存在的条文，引用不到就写
   「（此处需补充法律依据）」而不是编造；
4. 交付前必须过引用终审。

第 2 条是本 Skill 最关键的一步，也是最容易在实现里被忽略的一条：
文书里的"事实"必须能追溯到 ``facts`` 数组（每条 fact 都带 source），
而不是散落在正文里的自由文本。所以第 1 步的判据要求
「事实条数 ≥ 1 且每条都有 source」，把"事实可追溯"变成可校验条件。
"""

from __future__ import annotations

from backend.agents.state import LegalState
from backend.skills.base import SkillSpec, SkillStep, final_note, register


# --------------------------------------------------------------------------- 判据
def _facts_traceable(state: LegalState) -> bool:
    """第 1 步：事实齐备且**每条都能追溯到来源**。

    允许 ``来源 = 用户陈述（未经材料核验）`` —— 这是 IntakeNode 在无材料时
    写入的显式标注。要求的是"知道它从哪来"，不是"必须已核实"。
    但**没有 source 的事实一律不算**：那意味着它可能是模型编的。
    """
    facts = state.get("facts") or []
    return bool(facts) and all((f.get("source") or "").strip() for f in facts)


def _has_statutes(state: LegalState) -> bool:
    items = state.get("statutes") or []
    return any((s.get("status") or "") not in ("", "未知") for s in items)


def _has_draft(state: LegalState) -> bool:
    return len(state.get("draft") or "") >= 80


def _has_draft_type(state: LegalState) -> bool:
    return bool(state.get("draft_type"))


def _citation_passed(state: LegalState) -> bool:
    return (state.get("citation_check") or "") == "passed"


# --------------------------------------------------------------------------- Skill
DOC_DRAFT_SKILL = register(
    SkillSpec(
        id="doc_draft",
        name="文书生成 Skill",
        description="起草起诉状/答辩状/代理意见书/合同修改稿：事实只来自委托材料且可追溯，"
                    "法律依据只来自检索证据，交付前必须过引用终审",
        intents=["doc_draft"],
        doc_types=["litigation", "consult", "opinion"],
        prompt_task="draft",
        inputs={
            "doc_type": "str=complaint/defense/opinion/arbitration_application/revision",
            "case_facts": "list=案件事实（每条须带 source）",
            "issues": "list=争议焦点与请求事项",
            "parties": "list=当事人",
            "citations": "list=已通过终审的引用",
            "template": "str=律所模板标识，可选",
        },
        outputs=["draft", "draft_type", "citations", "citation_check", "disclaimer"],
        steps=[
            SkillStep(
                no=1, name="要素齐备性检查（事实可追溯）",
                action="确认当事人、事实、请求事项齐备；每条事实必须能追溯到来源",
                agent="intake", node="IntakeNode",
                required=True, produces=("facts", "parties", "issues"),
                check=_facts_traceable,
                absent_note="存在无法追溯来源的案件事实，文书事实部分不得生成"
                            "（避免把模型推断写成案件事实）",
            ),
            SkillStep(
                no=2, name="检索法律依据",
                action="检索支撑请求权的法条并校验时效，作为「法律依据」章节的唯一来源",
                agent="statute", tool="statute_search", node="StatuteNode",
                required=True, produces=("statutes",),
                check=_has_statutes,
                absent_note="未取得带效力状态的法条，文书的「法律依据」章节只能留空待补",
            ),
            SkillStep(
                no=3, name="检索类案（可选）",
                action="检索可参照的同类裁判，用于强化说理",
                agent="case", tool="case_search", node="CaseNode",
                required=False, produces=("cases",),
                check=lambda s: bool(s.get("cases")),
                absent_note="本次未检索到类案，文书说理缺少裁判倾向参照",
            ),
            SkillStep(
                no=4, name="生成文书草稿",
                action="按法定文书格式生成草稿，引用不到依据处写「（此处需补充法律依据）」",
                agent="draft", tool="draft_generate", node="DraftNode",
                required=True, produces=("draft", "draft_type"),
                check=_has_draft,
                absent_note="未生成文书正文",
            ),
            SkillStep(
                no=5, name="套用律所模板规范化",
                action="套用律所模板统一标题/当事人/落款/日期格式",
                agent="draft", tool="format_normalize", node="DraftNode",
                required=False, produces=("draft", "draft_type"),
                check=_has_draft_type,
                absent_note="未标注文书类型，交付前需人工套用律所模板",
            ),
            SkillStep(
                no=6, name="引用终审",
                action="逐条核对条文号/效力与案号存在性，不通过即拦截输出",
                agent="validator", tool="statute_validity_check", node="ValidatorNode",
                required=True, produces=("citations", "citation_check"),
                check=_citation_passed,
                absent_note=final_note("文书"),
            ),
        ],
    )
)


def doc_draft_skill() -> SkillSpec:
    return DOC_DRAFT_SKILL
