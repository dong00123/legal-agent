"""ProductRuntime：产品级 Agent 的独立运行时定义。

## 它是什么

改造前，``PRODUCT_AGENTS`` 里 4 个产品入口只是"门面"：每个入口钉一个意图 +
一个端点，背后共享同一套固定管线（同一 ``INTENT_ROUTES`` 静态路由、同一个
``Harness``、同一组控制器工具）。执行事实是"一个图 + 一个控制器"，产品口径
不等于执行事实。

改造后，每个产品拥有**独立的运行时实体**：

- ``node_pool``：该产品 Agent 可以自主规划使用的节点池（从图里 11 个节点中选取）；
- ``tool_pool``：该产品 Agent 自主补证时**自己**能调的工具集（控制器候选裁剪）；
- ``essential``：该产品的必需节点，产品级自主规划不可裁掉；
- ``planner_system`` / ``controller_system``：该产品 Agent 自己的"大脑"提示词，
  自主规划与自主裁决使用各自的产品身份与边界，不再共用同一段系统提示；
- ``force_report``：是否强制以 ReportNode 收尾（文书中心刻意不产出报告，
  交付物由模板渲染，因此设为 False，保持既有路径形状）。

``assistant``（AI 助手）是全功能通用运行时：不钉意图、全量节点池、全量工具池，
行为与改造前逐字一致（AI 助手原本就通过 IntentNode 分类 + 动态规划自主思考）。

## 前端零变动承诺

``PRODUCT_AGENTS`` 仍是前端产品菜单的唯一数据源（``product_directory()``），
``ProductAgent`` 增加的都是运行时字段，目录输出键不变 —— 前端菜单与交互
零变动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.agents.harness.router import product_agent_of_intent as _router_product_of_intent

# 节点执行顺序（与 planning._ORDER / planner.NODE_AGENT 语义顺序一致）。
# 产品级自主规划只能在这个顺序内增删节点，不能打乱依赖。
NODE_ORDER: tuple[str, ...] = (
    "IntakeNode", "StatuteNode", "CaseNode", "KnowledgeNode",
    "ReviewNode", "AnalysisNode", "ValidatorNode", "DraftNode",
    "ReportNode",
)


@dataclass
class ProductRuntime:
    """一个产品 Agent 的独立运行时配置。"""

    id: str                       # 与 PRODUCT_AGENTS 的产品 id 一致
    name: str                     # 产品名（展示/日志用）
    intent: str                   # 该产品钉住的意图（assistant 为空串 = 不钉）
    node_pool: tuple[str, ...]    # 自主规划的节点池（含该产品依赖的全部节点）
    tool_pool: tuple[str, ...]    # 自主补证的工具集（控制器候选裁剪）
    essential: tuple[str, ...]    # 必需节点（产品级规划不可裁掉）
    base_route: tuple[str, ...]   # 规则基线路由（规划未启用/失败时使用 = 原静态路由）
    force_report: bool = True     # 是否强制以 ReportNode 收尾
    planner_system: str = ""      # 该产品自主规划的 LLM 系统提示（空 = 默认）
    controller_system: str = ""   # 该产品自主裁决的 LLM 系统提示（空 = 默认全局）

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "intent": self.intent,
            "node_pool": list(self.node_pool),
            "tool_pool": list(self.tool_pool),
            "essential": list(self.essential),
            "base_route": list(self.base_route),
            "force_report": self.force_report,
        }


# --------------------------------------------------------------------------- 默认提示词
# 规划提示词的占位符：{name} 产品名、{pool} 节点池、{essential} 必需节点、
# {shape} 是否强制 ReportNode 收尾。
_DEFAULT_PLANNER_SYSTEM = """你是「{name}」产品的法律 Agent。在回答律师之前，
你先要自主决定本次任务跑哪些节点。

你的节点池（只能从池里选，不得新增池外节点）：{pool}

规则：
1. 只能从节点池里选，不得新增；不得打乱依赖顺序（Intake → Statute → Case →
   Knowledge → Review → Analysis → Validator → Draft → Report）。
2. 必需节点不可省略：{essential}。
3. 报告收尾：{shape}。
4. 宁少勿多：多跑一个检索节点就是多等几十秒，且会引入无关证据。
   证据链够用就不要再加节点。
5. 只输出 JSON：{{"nodes": [...], "why": "一句话说明为什么这样规划"}}"""


def _default_controller_system() -> str:
    """全局默认的自主裁决提示词（与 controller.py 的 _LLM_SYSTEM 同义）。

    放在这里是为了让产品运行时能够用"产品自己的裁决大脑"覆盖它。
    """
    return """你是法律 Agent 的**补证据**裁决器。上一轮的节点已经跑完，
你的唯一任务是判断：现有证据是否足以支撑结论；不足的话，从**候选动作**里选一个来补。

规则：
1. **只能从候选动作里选**，不得新增、不得改写动作名。候选集之外的一律无效。
2. **宁少勿多**：补一次检索要多等几十秒。证据已经够用就选 finish。
3. 不判断案情、不写结论、不回答律师，只决定"要不要再检一次、检什么"。
4. 只输出 JSON：{"action": "<候选动作之一>", "why": "一句话说明缺什么证据"}"""


# --------------------------------------------------------------------------- 注册表
# 4 个产品的独立运行时。
#
# ⚠ ``base_route`` 必须与 ``router.INTENT_ROUTES`` 对应意图的节点序列一致：
# 它既是"未启用产品规划时"的原行为，也是产品级规划失败时的规则兜底。
PRODUCT_RUNTIMES: dict[str, ProductRuntime] = {
    # ---- AI 助手：全功能通用运行时（不钉意图）
    # 节点池 = 全量 11 节点；工具池 = 控制器全部 3 个检索工具。
    # 行为与改造前逐字一致：IntentNode 分类 + 动态规划 + 全量控制器。
    "assistant": ProductRuntime(
        id="assistant",
        name="AI 助手",
        intent="",
        node_pool=("IntentNode", "IntakeNode", "StatuteNode", "CaseNode",
                   "KnowledgeNode", "ReviewNode", "AnalysisNode",
                   "ValidatorNode", "DraftNode", "MemoryNode", "ReportNode"),
        tool_pool=("statute_search", "case_search", "knowledge_search"),
        essential=(),
        base_route=(),
        force_report=True,
    ),

    # ---- 类案检索：自主决定是否加知识库/分析节点
    "case_search": ProductRuntime(
        id="case_search",
        name="类案检索",
        intent="case_search",
        node_pool=("StatuteNode", "CaseNode", "KnowledgeNode",
                   "AnalysisNode", "ValidatorNode", "ReportNode"),
        tool_pool=("statute_search", "case_search"),
        essential=("CaseNode", "ValidatorNode"),
        base_route=("StatuteNode", "CaseNode", "ValidatorNode", "ReportNode"),
        force_report=True,
        controller_system=("你是「类案检索」产品的法律 Agent 的补证据裁决器。"
                           "类案检索页的核心交付是类案与裁判要旨。"
                           "判断现有证据（法条/类案）是否足以支撑结论；不足时从候选动作里选一个补。"
                           "规则：1. 只能从候选动作里选；2. 宁少勿多；3. 只输出 JSON："
                           "{\"action\": \"<候选动作之一>\", \"why\": \"一句话说明缺什么证据\"}"),
    ),

    # ---- 合同审查：自主决定是否加类案/分析节点
    "contract_review": ProductRuntime(
        id="contract_review",
        name="合同审查",
        intent="contract_review",
        node_pool=("IntakeNode", "StatuteNode", "CaseNode", "KnowledgeNode",
                   "ReviewNode", "AnalysisNode", "ValidatorNode", "ReportNode"),
        tool_pool=("statute_search", "knowledge_search"),
        essential=("IntakeNode", "ReviewNode", "ValidatorNode"),
        base_route=("IntakeNode", "StatuteNode", "CaseNode", "KnowledgeNode",
                    "ReviewNode", "AnalysisNode", "ValidatorNode", "ReportNode"),
        force_report=True,
        controller_system=("你是「合同审查」产品的法律 Agent 的补证据裁决器。"
                           "合同审查的核心交付是逐条风险点与修改建议，依据是法条与本所审查指引。"
                           "判断现有证据是否足以支撑风险结论；不足时从候选动作里选一个补。"
                           "规则：1. 只能从候选动作里选；2. 宁少勿多；3. 只输出 JSON："
                           "{\"action\": \"<候选动作之一>\", \"why\": \"一句话说明缺什么证据\"}"),
    ),

    # ---- 文书中心：刻意不产出报告（交付物由模板渲染），不强制 ReportNode
    "document": ProductRuntime(
        id="document",
        name="文书中心",
        intent="doc_compose",
        node_pool=("StatuteNode", "KnowledgeNode", "ValidatorNode"),
        tool_pool=("statute_search", "knowledge_search"),
        essential=("StatuteNode", "ValidatorNode"),
        base_route=("StatuteNode", "KnowledgeNode", "ValidatorNode"),
        force_report=False,
        controller_system=("你是「文书中心」产品的法律 Agent 的补证据裁决器。"
                           "文书起草的核心交付是依据充分、引用已核验的文书草稿。"
                           "判断现有证据（法条/本所指引）是否足以支撑起草依据；不足时从候选动作里选一个补。"
                           "规则：1. 只能从候选动作里选；2. 宁少勿多；3. 只输出 JSON："
                           "{\"action\": \"<候选动作之一>\", \"why\": \"一句话说明缺什么证据\"}"),
    ),
}


def planner_system(runtime: ProductRuntime) -> str:
    """该产品的自主规划提示词（未配置时用默认模板按产品参数渲染）。"""
    if runtime.planner_system:
        return runtime.planner_system
    pool = " / ".join(runtime.node_pool) if runtime.node_pool else "（未限定，按意图路由）"
    essential = "、".join(runtime.essential) if runtime.essential else "（无强制节点）"
    shape = "必须包含 ReportNode 收尾" if runtime.force_report else "不需要 ReportNode（交付物由文书模板渲染）"
    return _DEFAULT_PLANNER_SYSTEM.format(
        name=runtime.name, pool=pool, essential=essential, shape=shape)


def controller_system(runtime: ProductRuntime) -> str:
    """该产品的自主裁决提示词（未配置时用全局默认）。"""
    return runtime.controller_system or _default_controller_system()


def product_agent_of_intent(intent: str) -> Any | None:
    """按钉住的意图反查产品门面配置（转发 router 同名函数）。"""
    return _router_product_of_intent(intent)


def infer_product(intent_override: str = "") -> ProductRuntime:
    """按入口钉住的意图推断产品运行时。

    钉了意图且能反查到产品 → 该产品的独立运行时；
    其余（AI 助手入口、未知意图）→ 通用运行时（assistant）。
    """
    if intent_override:
        pa = product_agent_of_intent(intent_override)
        if pa and pa.id in PRODUCT_RUNTIMES:
            return PRODUCT_RUNTIMES[pa.id]
    return PRODUCT_RUNTIMES["assistant"]


def resolve_runtime(*, intent: str = "", state: dict[str, Any] | None = None) -> ProductRuntime:
    """解析一次任务绑定的产品运行时。

    ``intent`` 优先（接口层已明确钉住的意图）；其次从 State 的
    ``intent_override`` 兜底读取，兼容部分入口只往 State 写钉意图的场景。
    """
    override = intent or (state or {}).get("intent_override") or ""
    return infer_product(override)


def runtimes_directory() -> list[dict[str, Any]]:
    """产品运行时的目录（管理后台/健康检查用，**不是**前端菜单数据源）。"""
    return [r.to_dict() for r in PRODUCT_RUNTIMES.values()]


__all__ = [
    "ProductRuntime", "PRODUCT_RUNTIMES", "NODE_ORDER",
    "planner_system", "controller_system",
    "product_agent_of_intent", "infer_product", "resolve_runtime",
    "runtimes_directory",
]
