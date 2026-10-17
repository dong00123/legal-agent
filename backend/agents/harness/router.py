"""Harness Router：节点声明与 Tool 的路由表。

方案 4.1 的 11 个节点职责拆分、4.2 的节点流转、第五章的 14 个 MCP Tool，
在本模块被显式登记为**数据**而不是散落在 if/else 里。这样做的收益：

- 管理后台可以直接展示"每一步能用哪些工具"（方案 12.3 系统配置）；
- 加一个新节点不用改流程代码，只需登记一行；
- 越权调用在 Router 层就能拦住，不用等执行时才发现。

⚠ **这里登记的是节点，不是对外的 Agent。** :data:`NODE_SPECS` 的 11 条与
``backend.agents.graph.NODES`` 的 11 个节点一一对应，一条不多一条不少；
对外的 Agent 只有 :data:`PRODUCT_AGENTS` 那 4 个（一个页面一个）。
"是不是有 11 个 Agent"这个误读，成因就是这份表曾经叫 ``AGENTS`` ——
名字一改，问题就不存在了，不用靠解释。

路由原则（方案 4.1）：**简单任务直接单节点完成**，不要为了"多 Agent"
把所有请求都塞进完整状态机。所以 :func:`route_for_intent` 会返回
"是否走完整状态机"以及"最少需要哪些节点"。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("legal_agent.harness.router")


@dataclass
class NodeSpec:
    """一个节点的声明：这一步是谁、允许调哪些工具。

    ``name`` 是这条声明的 id（``statute`` / ``review`` …），**不是节点名** ——
    节点名在 ``node`` 里。两者一一对应，但别把 ``name`` 读成"又一个 Agent"：
    它是 :data:`NODE_SPECS` 的键，条数恒等于 ``graph.NODES`` 的节点数。
    """

    name: str
    role: str
    tools: list[str] = field(default_factory=list)
    node: str = ""              # 对应的 LangGraph 节点名
    order: int = 100            # 在完整流程中的执行顺序（越小越前）
    description: str = ""


# --------------------------------------------------------------------------- 节点声明表
NODE_SPECS: dict[str, NodeSpec] = {
    "router": NodeSpec(
        "router", "Router / 意图 Agent", [], "IntentNode", 10,
        "判定法律咨询/合同审查/类案检索/文书起草/法规查询；简单任务直接单 Agent 完成",
    ),
    "intake": NodeSpec(
        "intake", "Intake Agent（材料解析）", ["material_parse", "material_ocr"],
        "IntakeNode", 20,
        "解析案件材料/合同结构，提取主体、事实、争点要素，输出结构化要素供下游复用",
    ),
    "statute": NodeSpec(
        "statute", "Statute Agent（法规检索）",
        ["statute_search", "statute_validity_check", "statute_version_compare"],
        "StatuteNode", 30,
        "检索法条、校验时效性、版本比对；检索 ≤3 次，强制带效力状态",
    ),
    "case": NodeSpec(
        "case", "Case Agent（类案检索）",
        ["case_search", "case_summarize", "case_no_verify"],
        "CaseNode", 40,
        "按案由/争议焦点/法院层级检索类案，提炼裁判要旨；检索 ≤2 次",
    ),
    "knowledge": NodeSpec(
        "knowledge", "Knowledge Agent（律所知识库）", ["knowledge_search"],
        "KnowledgeNode", 50,
        "律所知识库、办案指引、合同模板的 RAG 检索，标注指引版本",
    ),
    "review": NodeSpec(
        "review", "Review Agent（合同审查）",
        ["revision_draft_generate", "material_cross_compare", "review_checklist_export"],
        "ReviewNode", 60,
        "逐条比对风险点，生成审查意见清单；按 Skill 规定的审查路径执行",
    ),
    "analysis": NodeSpec(
        "analysis", "Analysis Agent（案件分析）",
        ["case_summarize", "material_cross_compare", "damages_calc"],
        "AnalysisNode", 60,
        "争议焦点归纳、攻防要点、诉讼策略、时效核查、法定金额测算（N/2N/代通知金/加班费）；"
        "类案不可用时明确标注证据缺口",
    ),
    "draft": NodeSpec(
        "draft", "文书生成 Agent",
        ["draft_generate", "revision_draft_generate", "format_normalize"],
        "DraftNode", 80,
        "起诉状/答辩状/意见书/合同修改稿草稿；引用必须来自检索证据",
    ),
    "validator": NodeSpec(
        "validator", "Citation Validator（引用终审）",
        ["statute_validity_check", "case_no_verify"],
        "ValidatorNode", 70,
        "终审：条文号/版本/效力、案号存在性逐条核对；校验不过直接拦截，不进入输出",
    ),
    "memory": NodeSpec(
        "memory", "Memory Agent",
        ["case_progress_record", "review_checklist_export"],
        "MemoryNode", 90,
        "案件档案摘要、历史办案经验的存取与脱敏；脱敏后入库，权限隔离",
    ),
    "report": NodeSpec(
        "report", "Report Agent（汇总输出）",
        ["review_checklist_export", "case_progress_record", "damages_calc"],
        "ReportNode", 100,
        "汇总输出（附引用清单与置信度）；金额一律取 damages_calc 的计算结果，不自行计算；"
        "强制标注「仅供律师工作参考」",
    ),
}


def get_node_spec(name: str) -> NodeSpec | None:
    """按声明 id（``statute`` / ``review`` …）取一条节点声明。"""
    return NODE_SPECS.get(name)


def tools_for(node: str) -> list[str]:
    spec = NODE_SPECS.get(node)
    return list(spec.tools) if spec else []


def nodes_with_tool(tool: str) -> list[str]:
    """一个工具被哪些节点允许调用（反向索引，用于越权检查）。"""
    return [name for name, spec in NODE_SPECS.items() if tool in spec.tools]


def can_call(node: str, tool: str) -> bool:
    """越权检查。检索类工具（statute_search 等）由 Router 显式授权给多个节点，
    因此这里采用"授权表 + 检索类通配"的规则。"""
    allowed = tools_for(node)
    if tool in allowed:
        return True
    # 只读检索类工具允许任何节点调用（它们不写数据、不泄露权限边界外的内容）
    readonly = {"statute_search", "statute_validity_check", "case_search",
                "knowledge_search", "case_summarize",
                # 方案 B：市场技能工具只读、幂等，任何节点均可按需拉取技能能力卡片
                "skill_list", "skill_invoke"}
    return tool in readonly


# --------------------------------------------------------------------------- 产品 Agent
@dataclass
class ProductAgent:
    """**对外的**产品级 Agent —— 律师在侧边栏上看到的那 4 个。

    与上面 :data:`NODE_SPECS` 的区别，是这一层最容易被混淆的地方：

    - :data:`NODE_SPECS` 是**实现**：11 个节点的声明，一个节点一条，用来回答
      "这一步是谁干的、它允许调哪些工具"（越权检查、能力卡、管理后台都读它）。
      **它不是 11 个 Agent** —— 它就是节点本身，条数恒等于 ``graph.NODES``。
    - :data:`PRODUCT_AGENTS` 是**门面**：4 个，一个页面一个，用来回答
      "律师点进去的这个入口，背后钉的是哪条意图、哪个端点"。

    两者不是一套命名空间，也不该合并：节点会随实现调整（加一个 MemoryNode、
    拆一个检索节点），而产品上的 4 个入口是承诺给用户的，不该跟着实现漂移。
    把节点直接当对外 Agent 展示的后果是"界面上有 11 个 Agent、用户只认得 4 个"，
    而对不上号的地方没有任何一处会报错。

    **运行时（2026-10-07）**：门面背后的"执行事实"已由
    :mod:`backend.agents.products` 独立化 —— 每个产品入口绑定**自己的
    ProductRuntime**（独立节点池 / 工具池 / 必需节点 / 自主规划与裁决提示词），
    :func:`product_directory` 输出键保持原样（前端菜单零变动），
    运行时目录见 ``products.runtime.runtimes_directory``。
    """

    id: str                    # 稳定标识，端点路径与卡片用它
    name: str                  # 界面上显示的名字（与侧边栏逐字一致）
    page: str                  # 前端页面 id（NAV 表里的 pg-*）
    intent: str                # 钉住的意图；**留空 = 由 IntentNode 判定**
    endpoint: str              # 该页面的入口端点
    description: str = ""


PRODUCT_AGENTS: dict[str, ProductAgent] = {
    "assistant": ProductAgent(
        "assistant", "AI 助手", "pg-ai", "",
        "/api/v1/agents/assistant/stream",
        "通用法律问答。**唯一不钉意图的入口** —— 由 IntentNode 判定路径，"
        "可能退化为快速问答或直答（判据见 Route/IntentNode 的 Trace）",
    ),
    "case_search": ProductAgent(
        "case_search", "类案检索", "pg-search", "case_search",
        "/api/v1/agents/case-search/stream",
        "按案情描述检索类案，逐条核验案号存在性；条数由页面的「返回条数」决定",
    ),
    "contract_review": ProductAgent(
        "contract_review", "合同审查", "pg-contract", "contract_review",
        "/api/v1/contract/review/stream",
        "上传即审查：风险点、缺失条款、修改对照稿；必须提供合同材料，否则 400",
    ),
    "document": ProductAgent(
        "document", "文书中心", "pg-doc", "doc_compose",
        "/api/v1/doc/compose",
        "先检索 + 引用终审，再按模板渲染成可编辑正文（不调模型起草）",
    ),
}


def product_agent(agent_id: str) -> ProductAgent | None:
    return PRODUCT_AGENTS.get(agent_id)


def product_agent_of_intent(intent: str) -> ProductAgent | None:
    """反查：某个意图属于哪个产品入口（``assistant`` 不钉意图，不参与反查）。"""
    for spec in PRODUCT_AGENTS.values():
        if spec.intent and spec.intent == intent:
            return spec
    return None


# --------------------------------------------------------------------------- 意图路由
# 每个意图：走完整状态机的节点序列 + 是否必须走完整流程
#
# **节点数是按"这个问题的复杂度"定的，不是按意图名字定的。**同一个 consult 意图，
# "试用期最长几个月"和"公司把我辞了还扣了竞业限制，能不能维权"需要的证据完全不同，
# 让后者跑 7 个节点是应该的，让前者也跑 7 个节点只是白白烧时间与 token
# —— 律师等 40 秒拿到一句"试用期最长六个月"，比慢更伤的是他下次就不问了。
# 所以意图定"方向"，复杂度定"节点数"，两者正交（见 agents/router.py 的降档判据）。
INTENT_ROUTES: dict[str, dict[str, Any]] = {
    "off_topic": {
        # 与法律无关（寒暄、闲聊、技术/写作/翻译等）：**零检索节点**，只留 ReportNode 直答。
        "nodes": ["ReportNode"],
        "full_pipeline": False,
        "direct": True,
        "label": "非法律问题（直答）",
        "why": "与法律无关，不检索、不终审，直接作答",
    },
    "quick_answer": {
        # 简单法律问题：只检索法条后直答，跳过材料解析、类案、本所指引与争议焦点归纳。
        #
        # **含 ValidatorNode**：与同族的 statute_lookup 对齐（那边写着"法条检索 +
        # 时效校验"）。原先这里只有两个节点，于是这类任务的 `citation_check`
        # 恒为 pending、报告里写着"本次未执行引用终审"，而正文照样列出检索到的
        # 法条 —— 等于把"没人核过效力状态"的依据当结论交付，
        # 正是 state.py 里"过时的法律依据比没有依据更危险"要防的事。
        # 终审在这里只多一次 statute_validity_check 批量调用（实测几十毫秒），
        # 换取"每一条依据都有人核过"，不值当省。
        "nodes": ["StatuteNode", "ValidatorNode", "ReportNode"],
        "full_pipeline": False,
        "direct": False,
        "label": "法律快速问答",
        "why": "单一结论/概念解释类提问，法条即可支撑，无需完整证据链",
    },
    "statute_lookup": {
        "nodes": ["StatuteNode", "ValidatorNode", "ReportNode"],
        "full_pipeline": False,
        "label": "法规查询",
        "why": "只需法条检索 + 时效校验，不需要材料解析与类案",
    },
    "case_search": {
        "nodes": ["StatuteNode", "CaseNode", "ValidatorNode", "ReportNode"],
        "full_pipeline": False,
        "label": "类案检索",
        "why": "需要类案 + 支撑法条，无需合同审查路径",
    },
    "consult": {
        "nodes": ["IntakeNode", "StatuteNode", "CaseNode", "KnowledgeNode",
                  "AnalysisNode", "ValidatorNode", "ReportNode"],
        "full_pipeline": True,
        "label": "法律咨询",
        "why": "需要法条 + 类案 + 本所指引，走完整检索链",
    },
    "case_analysis": {
        "nodes": ["IntakeNode", "StatuteNode", "CaseNode", "KnowledgeNode",
                  "AnalysisNode", "ValidatorNode", "ReportNode"],
        "full_pipeline": True,
        "label": "案件分析",
        "why": "争议焦点与攻防要点需要完整证据链支撑",
    },
    "contract_review": {
        # **刻意不含 DraftNode**：合同审查的交付物是「审查意见（ReportNode）+ 修改对照稿
        # （ReviewNode 第 8 步）」，没有诉讼／非诉文书。原先把 DraftNode 串在路径上，
        # 它会按「劳动」关键词生成一份**劳动仲裁申请书** —— 与本次交付毫无关系，
        # 还会因为缺少当事人诉求段被记一条「文书缺少必备段落」的假缺口。
        # 假缺口比缺功能更伤：律师一旦发现缺口清单里有条不成立的，剩下的也不再信。
        "nodes": ["IntakeNode", "StatuteNode", "CaseNode", "KnowledgeNode",
                  "ReviewNode", "AnalysisNode", "ValidatorNode", "ReportNode"],
        "full_pipeline": True,
        "label": "合同审查",
        "why": "按方案第六章合同审查 Skill 的 9 步路径执行，需产出修改对照稿",
    },
    "doc_draft": {
        "nodes": ["IntakeNode", "StatuteNode", "CaseNode", "KnowledgeNode",
                  "AnalysisNode", "ValidatorNode", "DraftNode", "ReportNode"],
        "full_pipeline": True,
        "label": "文书起草",
        "why": "文书中的每一处引用都必须是检索证据，必须先完成检索与终审",
    },
    "doc_compose": {
        # 文书中心的「检索 + 终审」段。**刻意不含 DraftNode / ReportNode。**
        #
        # 交付物由模板渲染（``template_service.compose``），不靠模型起草：
        # 31 份模板的段落形状、``__DELETE__`` 哨兵、Word 导出链路都是确定性的
        # 资产，让模型再写一遍只会把它们弄丢。这条路要的只是"套模板之前先有
        # 可核验的法条依据"。
        #
        # **这条路是真终审，不是空转**：``validator_node._validatable`` 的判据里
        # 包含"检索到的法条/类案"这类**证据池**对象，与正文有没有引用它无关 ——
        # 所以在还没生成正文的阶段跑 ValidatorNode，核的是条文号/版本/效力本身。
        "nodes": ["StatuteNode", "KnowledgeNode", "ValidatorNode"],
        "full_pipeline": False,
        "direct": False,
        "label": "文书套用（先检索终审）",
        "why": "文书交付物由模板渲染，只需要法条依据与引用终审，不需要文书生成与报告汇总",
    },
}


def route_for_intent(intent: str) -> dict[str, Any]:
    """按意图给出节点序列。未知意图按最保守的咨询路径走。"""
    route = INTENT_ROUTES.get(intent)
    if route is None:
        logger.info("意图 %s 未登记专用路由，按「法律咨询」路径执行", intent)
        route = INTENT_ROUTES["consult"]
        return {**route, "intent": "consult", "fallback_from": intent}
    return {**route, "intent": intent}


def is_simple(intent: str) -> bool:
    """是否是可以"单 Agent 直接完成"的简单任务。"""
    r = INTENT_ROUTES.get(intent)
    return bool(r and not r["full_pipeline"])


def is_direct(intent: str) -> bool:
    """是否**连检索都不用做**的直答（与法律无关）。

    与 :func:`is_simple` 是两档：simple 是"少跑几个节点"，direct 是"不检索直接答"。
    """
    return bool(INTENT_ROUTES.get(intent, {}).get("direct"))


def all_routes() -> dict[str, Any]:
    return {
        "intents": {k: {"label": v["label"], "nodes": v["nodes"],
                        "full_pipeline": v["full_pipeline"],
                        "direct": bool(v.get("direct")), "why": v["why"]}
                    for k, v in INTENT_ROUTES.items()},
        # ⚠ 键名 ``agents`` 是**留给管理后台的响应键**，别改（``harness_status()``
        # 里也有一个同名键）。它装的是 11 条**节点声明**。内部符号已统一改名成
        # :data:`NODE_SPECS`，只有这个键为了不动管理后台而保留原名。
        "agents": {name: {"role": s.role, "node": s.node, "tools": s.tools,
                          "order": s.order, "description": s.description}
                   for name, s in NODE_SPECS.items()},
        # 对外的 4 个产品入口。上面 ``agents`` 那份是节点声明，两份都要露，
        # 但**不能混在同一个键下** —— 混了以后"界面上有 11 个 Agent"这个
        # 观感就会回来，而那正是这次要修的东西。
        "products": product_directory(),
    }


def product_directory() -> list[dict[str, Any]]:
    """4 个产品 Agent 的对外目录（含各自钉住的意图与节点路线）。"""
    out: list[dict[str, Any]] = []
    for spec in PRODUCT_AGENTS.values():
        route = INTENT_ROUTES.get(spec.intent) or {}
        out.append({
            "id": spec.id,
            "name": spec.name,
            "page": spec.page,
            "intent": spec.intent,
            "intent_label": route.get("label", ""),
            "nodes": list(route.get("nodes") or []),
            "endpoint": spec.endpoint,
            "description": spec.description,
        })
    return out
