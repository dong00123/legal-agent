"""合同审查 Skill —— 方案第六章的 9 步路径。

原文（第六章）：
```
输入：contract_text, contract_type（买卖/租赁/劳动/服务）, client_role（甲方/乙方）
执行：
 1. 合同结构解析（主体/标的/价款/履行/违约/争议解决）
 2. 主体资格审查要素提取
 3. 检索适用法律（民法典合同编及司法解释，校验时效）
 4. 检索本所该类合同审查清单（Knowledge RAG）
 5. 逐条风险扫描：违约金、解除权、管辖、知识产权、保密
 6. 检索类案中同类条款的裁判倾向
 7. 生成风险点清单（条款定位 + 风险等级 + 修改建议）
 8. 生成修改对照稿
 9. Citation Validator 核对全部引用
```

**这 9 步不是建议，是判据**。每一步都有读 State 的 ``check``：
第 5 步"逐条风险扫描"如果没产出 ``risk_points``，审查报告就不该以完整形态交付。
第 9 步是 ``required=True`` —— 引用没过终审的审查意见，按方案红线不得输出。
"""

from __future__ import annotations

from backend.agents.state import LegalState
from backend.skills.base import SkillSpec, SkillStep, final_note, register


# --------------------------------------------------------------------------- 判据
def _has_structure(state: LegalState) -> bool:
    """第 1 步：结构解析。至少要能拆出条款，或至少拿到正文。"""
    return bool(state.get("clauses")) or len(state.get("material_text") or "") >= 200


def _has_parties(state: LegalState) -> bool:
    """第 2 步：主体资格审查要素。"""
    return bool(state.get("parties"))


def _has_statutes(state: LegalState) -> bool:
    """第 3 步：适用法律检索，且必须带效力状态（方案红线二）。"""
    items = state.get("statutes") or []
    return any((s.get("status") or "") not in ("", "未知") for s in items)


def _has_checklist(state: LegalState) -> bool:
    """第 4 步：本所审查清单。

    注意这是**非必需**步骤：知识库可能尚未收录该类合同的审查清单
    （本项目知识库当前只有少量指引）。此时应当降级为"按通用条款清单审查"
    并记录缺口，而不是中止审查 —— 审查能力不应被知识库的完备度绑架。
    """
    return bool(state.get("knowledges"))


def _has_risk_scan(state: LegalState) -> bool:
    """第 5 步：风险扫描确实跑过。

    判据是「有过一次扫描记录」而不是「有风险点」—— 一份正常的合同可能确实没有
    高风险条款，把"零风险"判成"没审查"会造成假阴性。所以这里看 **Trace 里是否
    有 ReviewNode 的扫描事件**，用它区分"扫了但没发现"与"根本没扫"。
    """
    if state.get("risk_points"):
        return True
    return any(
        (e.get("node") or "") == "ReviewNode" and (e.get("event") or "") in ("scanned", "risk_scan")
        for e in (state.get("trace") or [])
    )


def _has_risk_points(state: LegalState) -> bool:
    """第 7 步：风险点清单，且必须带条款定位。

    只判 ``risk_points`` 非空不够：没有 ``clause_no`` 的风险点，
    律师在长合同里根本找不到是在说哪一条，等于不可用。
    """
    items = state.get("risk_points") or []
    return bool(items) and any((r.get("clause_no") or "") for r in items)


def _has_case_tendency(state: LegalState) -> bool:
    """第 6 步：类案裁判倾向。方案第十三章要求类案不可用时明确标注缺口。"""
    return bool(state.get("cases"))


def _has_revision(state: LegalState) -> bool:
    """第 8 步：修改对照稿。"""
    return bool(state.get("revision_draft")) or bool(state.get("draft"))


def _citation_passed(state: LegalState) -> bool:
    """第 9 步：引用终审通过。

    只认 ``passed``。``pending``（没跑）与 ``failed``（没通过）都不算完成 ——
    这是三条反幻觉红线里「无来源不出引用」的落点。
    """
    return (state.get("citation_check") or "") == "passed"


# --------------------------------------------------------------------------- Skill
CONTRACT_REVIEW_SKILL = register(
    SkillSpec(
        id="contract_review",
        name="合同审查 Skill",
        description="按资深律师审查路径逐条扫描合同风险，产出风险点清单（条款定位+等级+修改建议）"
                    "与修改对照稿，引用必须通过终审",
        intents=["contract_review"],
        doc_types=["contract"],
        prompt_task="contract_review",
        inputs={
            "contract_text": "str=合同全文（必填）",
            "contract_type": "str=买卖/租赁/劳动/服务，缺省自动判定",
            "client_role": "str=甲方/乙方，决定风险倾向（站在谁的角度改）",
            "clauses": "list=已拆分的条款序列（可选，缺省由 material_parse 提供）",
            "case_id": "str=关联案件，用于审计与权限隔离",
        },
        outputs=["clauses", "parties", "statutes", "knowledges", "risk_points",
                 "missing_clauses", "revision_draft", "citations", "citation_check"],
        steps=[
            SkillStep(
                no=1, name="合同结构解析",
                action="解析主体/标的/价款/履行/违约/争议解决，拆出可定位的条款序列",
                agent="intake", tool="material_parse", node="IntakeNode",
                required=True, produces=("clauses", "material_text"),
                check=_has_structure,
                absent_note="未能拆出合同条款结构，风险点将无法定位到具体条款",
            ),
            SkillStep(
                no=2, name="主体资格审查要素提取",
                action="提取甲方/乙方/签署人等主体及其身份类型，用于核查签约主体适格性",
                agent="intake", node="IntakeNode",
                required=False, produces=("parties",),
                check=_has_parties,
                absent_note="未提取到当事人信息，主体适格性需律师人工核查",
            ),
            SkillStep(
                no=3, name="检索适用法律并校验时效",
                action="检索民法典合同编及对应司法解释，逐条核对现行有效/已修订/已废止",
                agent="statute", tool="statute_search", node="StatuteNode",
                required=True, produces=("statutes",),
                check=_has_statutes,
                absent_note="未取得带效力状态的法条依据，修改建议的法律依据需律师自行补充",
            ),
            SkillStep(
                no=4, name="检索本所该类合同审查清单",
                action="从律所知识库取该类合同的审查清单与合同模板（须标注指引版本）",
                agent="knowledge", tool="knowledge_search", node="KnowledgeNode",
                required=False, produces=("knowledges",),
                check=_has_checklist,
                absent_note="知识库未命中该类合同的审查清单，本次按通用条款清单审查",
            ),
            SkillStep(
                no=5, name="逐条风险扫描",
                action="按违约金/解除权/管辖/知识产权/保密/付款交付等维度逐条扫描",
                agent="review", node="ReviewNode",
                required=True, produces=("risk_points",),
                check=_has_risk_scan,
                absent_note="未产出任何风险点，需人工复核（也可能确实无风险，但应留痕说明）",
            ),
            SkillStep(
                no=6, name="检索类案同类条款裁判倾向",
                action="检索该类条款在司法实践中的裁判倾向，作为修改幅度的参照",
                agent="case", tool="case_search", node="CaseNode",
                required=False, produces=("cases",),
                check=_has_case_tendency,
                absent_note="本次类案检索不可用，修改建议缺少裁判倾向参照",
            ),
            SkillStep(
                no=7, name="生成风险点清单（条款定位+等级+建议）",
                action="把扫描结果整理为可逐条定位的清单，含缺失条款提示",
                agent="review", tool="review_checklist_export", node="ReviewNode",
                required=True, produces=("risk_points", "missing_clauses"),
                check=_has_risk_points,
                absent_note="风险点清单缺少条款定位，律师无法在原文中跳转核对",
            ),
            SkillStep(
                no=8, name="生成修改对照稿",
                action="按「原文 → 修改后 → 修改理由」三列生成对照稿",
                # 产出 ``revision_draft`` 的是 ReviewNode（它调用 revision_draft_generate），
                # 不是 DraftNode —— 判据表写错节点名，审计与缺口信息就会指向一个
                # 压根没参与本次作业的 Agent，律师按图索骥只会更糊涂。
                agent="review", tool="revision_draft_generate", node="ReviewNode",
                required=True, produces=("revision_draft",),
                check=_has_revision,
                absent_note="未生成修改对照稿，交付前需人工补充",
            ),
            SkillStep(
                no=9, name="Citation Validator 核对全部引用",
                action="逐条核对条文号/版本/效力与案号存在性，不通过即拦截输出",
                agent="validator", tool="statute_validity_check", node="ValidatorNode",
                required=True, produces=("citations", "citation_check"),
                check=_citation_passed,
                absent_note=final_note("审查意见"),
            ),
        ],
    )
)


def contract_review_skill() -> SkillSpec:
    return CONTRACT_REVIEW_SKILL
