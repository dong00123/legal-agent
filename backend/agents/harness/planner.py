"""Harness Planner：任务拆解。

方案 4.4 节 Harness 的 ``Planner``。它把"用户一句话"变成**可执行、可计价、
可追踪**的步骤清单，并给出每步的检索式与产出要求。

为什么要有 Planner（而不是让各节点自己看着办）：
1. **检索式质量决定 RAG 效果**。合同审查要搜"违约金 过高 调整"，案件分析要搜
   "违法解除 赔偿金"，两者检索式不同。集中生成比每个节点各写一份更可控、可回归测试。
2. **可解释**：律师能看懂"系统打算做哪几步"，也就能指出"这一步不该做"。
3. **可计价**：步数直接对应 token 与耗时预算，是控制成本与超时的依据。

与 Router 的分工：Router 决定**去哪几个节点**，Planner 决定**在每个节点里
用什么检索式、要拿到什么**。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from backend.agents.harness.router import INTENT_ROUTES, is_simple, route_for_intent

logger = logging.getLogger("legal_agent.harness.planner")


@dataclass
class PlanStep:
    """一个规划步骤。"""

    node: str
    agent: str
    action: str
    queries: list[str] = field(default_factory=list)
    expects: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"node": self.node, "agent": self.agent, "action": self.action,
                "queries": self.queries, "expects": self.expects,
                "tools": self.tools, "required": self.required}


@dataclass
class TaskPlan:
    """一次任务的完整计划。"""

    intent: str
    label: str
    simple: bool
    steps: list[PlanStep] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    max_steps: int = 8
    # 因**步数预算**被砍掉的节点。刻意与"动态规划主动省去"分开记：
    # 前者是"想跑但跑不下"，是证据缺口；后者是"这个意图不需要"，是正常工作。
    # 混在一起会让每一次正常的动态规划都被记成缺口并扣置信度。
    trimmed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "label": self.label,
            "simple": self.simple,
            "steps": [s.to_dict() for s in self.steps],
            "notes": self.notes,
            "max_steps": self.max_steps,
            "trimmed": self.trimmed,
        }

    def nodes(self) -> list[str]:
        return [s.node for s in self.steps]

    def queries_for(self, node: str) -> list[str]:
        for s in self.steps:
            if s.node == node:
                return s.queries
        return []


# --------------------------------------------------------------------------- 节点 → Agent
NODE_AGENT = {
    "IntentNode": "router",
    "IntakeNode": "intake",
    "StatuteNode": "statute",
    "CaseNode": "case",
    "KnowledgeNode": "knowledge",
    "ReviewNode": "review",
    "AnalysisNode": "analysis",
    "ValidatorNode": "validator",
    "DraftNode": "draft",
    "MemoryNode": "memory",
    "ReportNode": "report",
}

NODE_ACTION = {
    "IntentNode": "判定意图并选定执行路径",
    "IntakeNode": "解析材料、提取主体/事实/争点",
    "StatuteNode": "检索适用法条并校验时效性",
    "CaseNode": "检索类案并提炼裁判要旨",
    "KnowledgeNode": "检索本所办案指引与审查清单",
    "ReviewNode": "逐条风险扫描并生成风险点清单",
    "AnalysisNode": "归纳争议焦点、攻防要点与诉讼策略",
    "ValidatorNode": "引用终审：条文号/效力/案号逐条核对",
    "DraftNode": "生成文书草稿或修改对照稿",
    "MemoryNode": "归档案件档案与办案经验（脱敏）",
    "ReportNode": "汇总输出并附引用清单与置信度",
}

NODE_TOOLS = {
    "IntakeNode": ["material_parse"],
    "StatuteNode": ["statute_search", "statute_validity_check"],
    "CaseNode": ["case_search", "case_no_verify"],
    "ValidatorNode": ["statute_validity_check", "case_no_verify"],
    "DraftNode": ["draft_generate", "format_normalize"],
    # 金额由代码算：AnalysisNode 归纳争议焦点时要给数，ReportNode 要落进表格。
    # 只授给这两个节点，**不进 router.can_call 的只读通配符** ——
    # damages_calc 是"算"不是"检"，进了通配符 ReviewNode/DraftNode 也能自己算金额。
    "AnalysisNode": ["damages_calc"],
    "ReportNode": ["review_checklist_export", "damages_calc"],
}

# 各节点**承诺写入 State 的字段**。
# 提到模块级是为了让 ``agents.protocol.registry`` 的 Agent 卡片直接引用同一张表 ——
# 卡片写"我产出 cases"而节点实际不产出，调用方会一直等一个永远不来的东西。
NODE_PRODUCES: dict[str, list[str]] = {
    "IntentNode": ["intent", "doc_type"],
    "IntakeNode": ["parties", "facts", "issues", "clauses"],
    "StatuteNode": ["statutes（含效力状态）"],
    "CaseNode": ["cases（含案号或案例名）"],
    "KnowledgeNode": ["knowledges（含版本）"],
    "ReviewNode": ["risk_points", "missing_clauses"],
    "AnalysisNode": ["issues", "strategy", "limitation_check"],
    "ValidatorNode": ["citations", "citation_check"],
    "DraftNode": ["draft / revision_draft"],
    "MemoryNode": ["案件档案与经验落库"],
    "ReportNode": ["report", "confidence", "disclaimer"],
}


class Planner:
    """任务拆解器。"""

    def plan(self, *, intent: str, query: str, doc_type: str = "unknown",
             has_material: bool = False, case_id: str = "",
             material_text: str = "",
             max_steps: int | None = None,
             nodes_override: list[str] | None = None) -> TaskPlan:
        route = route_for_intent(intent)
        nodes: list[str] = route["nodes"]
        effective_intent = route["intent"]

        # 动态规划（planning.py）：按问题本身裁剪/扩张节点，而不是照抄固定路由。
        # 传进来的序列已经过 sanitize（候选池过滤 + ReportNode 收尾），
        # 这里只做一件事：让"计划"与"实际执行的 route"是同一份，避免两张皮。
        if nodes_override:
            if nodes_override != nodes:
                logger.info("采用动态规划结果（静态路由为 %s）：%s", nodes, nodes_override)
            nodes = list(nodes_override)

        # 合同审查要先认出**哪一类合同**：检索式、风险规则、必备条款清单都按类型走。
        # 只看 doc_type 不够 —— 它只说"这是合同"，不说"这是劳动合同还是房屋租赁合同"。
        contract_type = _contract_type_of(effective_intent, material_text, query)
        domain = _domain_hint(doc_type, f"{material_text}\n{query}", contract_type)

        plan = TaskPlan(
            intent=effective_intent,
            label=route["label"],
            simple=is_simple(effective_intent),
            max_steps=max_steps or _max_steps(),
        )
        plan.notes.append(f"路由依据：{route['why']}")
        if contract_type:
            plan.notes.append(f"合同类型判定：{contract_type}（决定适用的风险规则与检索条文）")
        if route.get("fallback_from"):
            plan.notes.append(f"原始意图「{route['fallback_from']}」未登记专用路由，已回退到「{route['label']}」")

        # 没有材料时，IntakeNode 只能做要素推断，降级说明要提前写清楚
        if "IntakeNode" in nodes and not has_material:
            plan.notes.append("本次未提供案件材料，IntakeNode 将只做要素推断（不解析文件）")

        for node in nodes:
            agent = NODE_AGENT.get(node, "")
            step = PlanStep(
                node=node,
                agent=agent,
                action=NODE_ACTION.get(node, ""),
                tools=list(NODE_TOOLS.get(node, [])),
                queries=self._queries_for(node, effective_intent, query, doc_type,
                                          domain, contract_type),
                expects=self._expects_for(node),
                required=node not in ("KnowledgeNode", "CaseNode"),
            )
            plan.steps.append(step)

        if plan.simple:
            plan.notes.append("判定为简单任务：按方案 4.1，跳过材料解析与类案，直接单 Agent 完成")

        if len(plan.steps) > plan.max_steps:
            # 步数超预算时优先保留必需节点
            required = [s for s in plan.steps if s.required]
            optional = [s for s in plan.steps if not s.required]
            keep = required + optional[: max(0, plan.max_steps - len(required))]
            kept = {s.node for s in keep}
            plan.trimmed = [n for n in nodes if n not in kept]
            plan.steps = sorted(keep, key=lambda s: nodes.index(s.node))
            plan.notes.append(
                f"节点数 {len(nodes)} 超过步数预算 {plan.max_steps}，"
                f"已裁剪可选节点（保留：{'、'.join(plan.nodes())}）"
            )

        logger.info("任务规划完成：意图=%s 简单任务=%s 节点=%s",
                    plan.intent, plan.simple, "→".join(plan.nodes()))
        return plan

    # ------------------------------------------------------------------ 检索式
    def _queries_for(self, node: str, intent: str, query: str,
                     doc_type: str, domain: str,
                     contract_type: str = "") -> list[str]:
        """为每个节点生成检索式。

        设计要点：法规检索式要带**法律术语**而不是原话（律师说"公司把我开了不给钱"，
        检索式应是"违法解除劳动合同 经济补偿 赔偿金"）。这里用意图 + 领域词做扩展，
        不做 LLM 改写 —— 检索式必须可复现、可回归测试。
        """
        base = (query or "").strip()

        if node == "StatuteNode":
            if intent == "contract_review":
                # 合同审查的法规检索式**由规则表声明的条文反推**，不用泛化关键词。
                # 原因：泛化关键词召回的是"最像的"条文，而审查要用的是"确定的"条文。
                # 实测「合同 合同 违约责任 违约金 调整」命中的是民法典第592条，
                # 而规则表要引第五百八十五条 —— 每条风险点的依据都会落空。
                qs = _cite_queries(_required_cites(contract_type), domain) + [base]
                return _dedupe(qs)[:3]
            qs = [base]
            if intent in ("case_analysis", "consult"):
                qs += [f"{domain} 法律责任 赔偿 举证责任",
                       f"{domain} 诉讼时效 期间 起算"]
            elif intent == "case_search":
                qs += [f"{domain} 裁判要点 适用法律"]
            elif intent == "doc_draft":
                qs += [f"{domain} 请求权基础 责任构成"]
            return _dedupe(qs)[:3]

        if node == "CaseNode":
            qs = [base]
            if intent == "contract_review":
                qs.append(f"{domain} 违约金过高 调整 裁判倾向")
            elif intent in ("case_analysis", "consult"):
                qs.append(f"{domain} 类案 裁判要旨 争议焦点")
            return _dedupe(qs)[:2]

        if node == "KnowledgeNode":
            qs = [base]
            if intent == "contract_review":
                qs += [f"{domain} 合同审查清单 风险点", "合同审查 必备条款 缺失"]
            elif intent in ("case_analysis", "consult"):
                qs += [f"{domain} 办案指引 办理要点"]
            elif intent == "doc_draft":
                qs += ["文书起草规范 格式 要素"]
            return _dedupe(qs)[:3]

        if node == "IntakeNode":
            return [base] if base else []

        if node == "ReviewNode":
            return [f"{domain} 风险点 修改建议"] if domain else []

        if node == "AnalysisNode":
            return [f"{domain} 争议焦点 攻防要点"] if domain else []

        return []

    @staticmethod
    def _expects_for(node: str) -> list[str]:
        return list(NODE_PRODUCES.get(node, []))


# --------------------------------------------------------------------------- 工具
_KEYWORD_HINTS = (
    ("劳动合同", "劳动合同"), ("竞业限制", "竞业限制"), ("试用期", "劳动合同"),
    ("解除", "劳动合同解除"), ("劳动", "劳动"),
    ("租赁", "房屋租赁"), ("买卖", "买卖"), ("供货", "买卖合同"),
    ("知识", "知识产权"), ("专利", "专利"), ("商标", "商标"), ("著作", "著作权"),
    ("工伤", "工伤"), ("社保", "社会保险"), ("加班", "加班工资"), ("工资", "工资"),
    ("股权", "股权"), ("借款", "民间借贷"), ("侵权", "侵权"), ("建设", "建设工程"),
)


def _keyword_hint(text: str) -> str:
    for kw, hint in _KEYWORD_HINTS:
        if kw in (text or ""):
            return hint
    return ""


def _domain_hint(doc_type: str, text: str, contract_type: str = "") -> str:
    """领域词：优先合同子类型，其次按关键词猜，最后给一个中性词。

    ``doc_type == "contract"`` 时**不能直接返回"合同"就收工**：
    "合同"这个词在法规库里的区分度接近零，检索式会变成
    「合同 合同 违约责任 违约金 调整」，召回的是民法典合同编里
    排在最前面的条文，与本次要审的合同类型无关。
    """
    if contract_type:
        return contract_type
    hint = _keyword_hint(text)
    if hint:
        return hint
    if doc_type in ("litigation", "evidence"):
        return "诉讼"
    if doc_type == "contract":
        return "合同"
    return "民事"


def _contract_type_of(intent: str, material_text: str, query: str) -> str:
    """合同审查意图下判出合同子类型；其它意图返回空串。

    只看 ``doc_type`` 是不够的：它只说"这是合同"。而适用的风险规则、
    必备条款清单、必须检索的条文，全都按**子类型**分叉 ——
    把劳动合同的「试用期上限」规则套到房屋租赁合同上，
    会产出一条凭空的伪造风险。
    """
    if intent != "contract_review":
        return ""
    from backend.llm.rule_engine import _guess_contract_type

    text = material_text or ""
    if not text.strip():
        text = query or ""
    return _guess_contract_type(text)


def _required_cites(contract_type: str) -> list[tuple[str, str]]:
    from backend.llm.rule_engine import required_cites

    return required_cites(contract_type)


def _cite_queries(cites: list[tuple[str, str]], domain: str,
                  max_groups: int = 2) -> list[str]:
    """把规则声明的条文编成**靶向检索式**，每条都写成「《法规名》第X条」。

    两个刻意的设计：

    1. **每条重复写法规名**。「《劳动合同法》第十九条 第二十四条」里的第二条
       没有法规名，解析不出精确条文要求 —— 而漏掉的条文不会报错，
       只会在风险点里表现为"需补充法律依据"，属于最难查的静默失效。
    2. **最多 2 组**。法规检索配额只有 3 次（方案 4.1「检索 ≤3 次」），
       留一次给原始提问做语义兜底：规则表覆盖的是"已知要用的条文"，
       原始提问覆盖的是"规则表没预见的东西"，两者都不能丢。
    """
    if not cites:
        return []
    ordered = sorted(cites, key=lambda c: c[0])  # 同法规的条文排在一起，分组后不互相夹杂
    per_group = max(1, -(-len(ordered) // max_groups))
    out: list[str] = []
    for i in range(0, len(ordered), per_group):
        chunk = ordered[i:i + per_group]
        refs = " ".join(f"《{law}》{art}" for law, art in chunk)
        out.append(f"{refs} {domain}".strip())
    return out[:max_groups]


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        v = (v or "").strip()
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def _max_steps() -> int:
    from backend.app.config import settings

    return settings.MAX_STEPS


def plan_summary() -> dict[str, Any]:
    """路由与规划配置总览（管理后台展示用）。"""
    return {
        "intents": list(INTENT_ROUTES),
        "node_agents": NODE_AGENT,
        "max_steps": _max_steps(),
    }


planner = Planner()
