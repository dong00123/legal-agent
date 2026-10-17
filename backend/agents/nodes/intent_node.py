"""IntentNode：意图判定与路径选择（方案 4.1 Router Agent）。

原文约束：「判定法律咨询/合同审查/类案检索/文书起草/法规查询；**简单任务直接
单 Agent 完成**」。这条约束的含义是：不要为了"多 Agent"把所有请求都塞进完整
状态机 —— 一个"《劳动合同法》第八十七条讲的什么"的提问不值得跑材料解析 +
类案检索 + 合同审查。

本节点做三件事：
1. 调 :class:`IntentRouter` 定意图（规则优先、LLM 仅兜底，理由见 router.py）；
2. 定 / 校正 `doc_type`（决定后续 Skill 匹配与领域检索式）；
3. **把执行计划落进 State**（``route`` 字段），让前端能显示"打算做什么"，
   也让律师在结果不对时能指出"第一步就判错了"。

意图含糊时**不猜死**：仍按最保守的咨询路径走（不阻断办案），但写进
``evidence_gap`` 并标注 —— 因为意图是整个流程的分叉点，错一次后面全错，
且"错得很像对的"。
"""

from __future__ import annotations

import logging

from backend.agents.harness import Harness
from backend.agents.planning import draft_plan, dynamic_enabled, refine_with_llm, sanitize
from backend.agents.router import IntentResult, IntentRouter, explicit_result
from backend.agents.state import LegalState
from backend.app.config import settings

logger = logging.getLogger("legal_agent.nodes.intent")

AGENT = "router"

# 单例：IntentRouter 无状态，重复构造没有意义
_router = IntentRouter()


async def intent_node(state: LegalState, harness: Harness) -> LegalState:
    """判定意图并生成执行计划。"""
    harness.step("IntentNode")

    # 调用方钉住的意图优先 —— 产品页面的每个 Agent 入口（类案检索 / 文书中心）
    # 各对应一种意图，不能靠分类器"猜对"。
    #
    # 钉住时**不跑分类器**：分类器是"猜"，这是个明确的要求。跑一遍再覆盖
    # 除了白费一次（可能带 LLM 的）调用，还会在 Trace 里留下一条与最终结果
    # 相矛盾的判定记录，事后排障时读不出来到底发生了哪一件。
    pinned = str(state.get("intent_override") or "").strip()
    result: IntentResult | None = explicit_result(
        pinned, state.get("query") or "",
        material_doc_type=state.get("doc_type") or "",
    ) if pinned else None
    explicit = result is not None

    if not explicit:
        if pinned:
            # 钉了个路由表里没有的意图。**不能静默忽略** —— 静默忽略正是
            # "界面说走 A、管线跑了 B"这一类问题的成因。登记缺口让人看得见。
            harness.gaps("skill",
                         f"入口指定的意图「{pinned}」不在路由表里，已改由分类器判定",
                         "本次的实际路径可能并非该入口预期的路径，请核对后再引用")
            harness.tracer.warn("IntentNode", "bad_override",
                                f"未知的意图指定：{pinned}（可选值见 INTENT_ROUTES）")
        result = await _router.classify(
            state.get("query") or "",
            material_text=state.get("material_text") or "",
            material_doc_type=state.get("doc_type") or "",
        )

    state["intent"] = result.intent  # type: ignore[typeddict-item]
    state["domain"] = result.domain  # type: ignore[typeddict-item]
    if result.doc_type and result.doc_type != "unknown":
        state["doc_type"] = result.doc_type  # type: ignore[typeddict-item]
    if result.draft_type:
        state["draft_type"] = result.draft_type

    harness.tracer.info(
        "IntentNode", "classified",
        f"意图={result.intent} 领域={result.domain} 文书类型={state.get('doc_type')} "
        f"判定方式={result.decided_by} 置信度={result.confidence:.2f}"
        + (f" 命中={','.join(result.hits[:4])}" if result.hits else ""),
    )

    if result.intent in ("off_topic", "quick_answer"):
        # 降档必须**说出来**：律师看到只跑了 1~2 个节点、报告也短，
        # 第一反应会是"是不是漏审了"。这里把判据写进 Trace，他一眼能判断降得对不对。
        why = result.notes[0] if result.notes else "判定为轻量问题"
        harness.tracer.info(
            "IntentNode", "depth_downgrade",
            f"按轻量路径执行（{_label(result.intent)}，{len(_route_nodes(result.intent))} 个节点）：{why}"
            + "；如需完整检索与终审，请在提问中说明诉求（如「帮我审查」「帮我起草」「分析一下胜诉可能」）",
        )

    if result.decided_by in ("rule-ambiguous", "default"):
        # 含糊/无信号：不阻断（按咨询路径继续），但必须让律师知道判定依据薄弱
        why = result.notes[0] if result.notes else "意图特征信号不足"
        harness.gaps("skill",
                     f"意图判定把握较低（{why}），已按「{_label(result.intent)}」路径执行",
                     "若本次并非该类需求，请补充说明后重新提交，以获得正确的检索与审查路径")
        harness.tracer.warn("IntentNode", "low_confidence", why)

    # ---- 规划：节点序列 + 每步检索式，落到 State 供前端展示与审计
    # 动态规划：先按规则给出候选，再（可选）让模型复核。两者都失败时
    # sanitize 会退回静态路由 —— 所以这里不存在"规划失败就跑不起来"的情况。
    #
    # ⚠ 显式钉住意图时**跳过动态规划**。动态规划的本职是"按问题把静态路由裁剪到
    #   够用为止"（见 planning.draft_plan），而钉住的入口要的恰恰是那条完整路由：
    #   类案检索页钉 case_search 就是为了拿到类案，被裁掉 CaseNode 之后页面会
    #   空空如也，且不报任何错。要裁剪的是"用户随口一问"，不是"页面点名要跑"。
    override = None
    draft = None
    if explicit:
        harness.tracer.info(
            "IntentNode", "explicit_intent",
            f"意图由调用方指定为 {result.intent}（{_label(result.intent)}），"
            f"按完整路由执行：{' → '.join(_route_nodes(result.intent))}"
            "；已跳过动态规划（按问题裁剪节点是给自然语言提问用的）",
        )
        # ---- 产品级自主规划（2026-10-07）
        # 钉住的产品入口（AI 助手 / 类案检索 / 合同审查 / 文书中心）从"门面 +
        # 共享固定管线"改造为"各自独立的真正 Agent"后，本次节点序列由**产品自己的
        # 规划器**（LLM 在自身节点池内决策，必需节点不可删，失败回退静态路由）
        # 决定 —— 见 backend/agents/products/planner.py 的 product_plan。
        # orchestrator 已在图运行前把规划结果放进 harness.product_plan：
        # 未绑定产品 / 未启用规划 / 规划失败时为 None —— 全部回退原静态路由，
        # 行为与改造前逐字一致（前端零变动兜底）。
        if harness.product_plan:
            override = sanitize(harness.product_plan, intent=result.intent,
                                fallback=_route_nodes(result.intent))
            harness.tracer.info(
                "IntentNode", "product_plan",
                f"产品 Agent 自主规划（{getattr(harness.product, 'id', '?')}）："
                f"{' → '.join(harness.product_plan)}",
            )
    elif dynamic_enabled():
        draft = draft_plan(intent=result.intent, query=state.get("query") or "",
                           has_material=bool(state.get("material_text")),
                           doc_type=state.get("doc_type") or "",
                           material_text=state.get("material_text") or "")
        if settings.DYNAMIC_PLAN_LLM:
            draft = await refine_with_llm(
                draft, query=state.get("query") or "",
                has_material=bool(state.get("material_text")),
                doc_type=state.get("doc_type") or "",
            )
        override = sanitize(draft.nodes, intent=result.intent,
                            fallback=_route_nodes(result.intent))

    plan = harness.plan(intent=result.intent, nodes_override=override)
    full_nodes = _route_nodes(result.intent)
    state["route"] = plan.nodes()
    if draft is not None:
        state["plan_source"] = draft.source
        added = [n for n in plan.nodes() if n not in full_nodes]
        removed = [n for n in full_nodes if n not in plan.nodes()]
        harness.tracer.info(
            "IntentNode", "dynamic_plan",
            f"按问题规划（依据={draft.source}）：{' → '.join(plan.nodes())}"
            + (f"｜较静态路由增加 { '、'.join(added) }" if added else "")
            + (f"｜较静态路由省去 { '、'.join(removed) }" if removed else "")
            + (f"｜理由：{'; '.join(draft.rules[:3])}" if draft.rules else ""),
        )

    for note in plan.notes:
        harness.ctx.note(note)

    harness.tracer.info("IntentNode", "planned",
                        f"{plan.label}：{' → '.join(plan.nodes())}"
                        f"（简单任务={plan.simple}，步数预算={plan.max_steps}）")
    if plan.simple:
        harness.tracer.info("IntentNode", "simple_task",
                            "判定为简单任务，跳过材料解析与类案检索，直接单 Agent 完成")
    # 证据缺口只认**步数预算真的砍了节点**（plan.trimmed）。
    #
    # 这里原先比的是"计划比静态路由短"，而动态规划按问题裁剪节点正是它的本职工作：
    # 静态路由 7 个节点、按问题只需跑 4 个，会被判成"超出步数预算 16"——
    # 只跑 4 步却说超了 16 步的预算，这条缺口的内容是假的，还白白扣 0.05 置信度，
    # 而这 0.05 恰好卡在缓存准入门槛上。动态规划主动省去哪些节点，
    # 已在上面 dynamic_plan 那条 trace 里逐项写明，不需要再登记为缺口。
    if plan.trimmed:
        harness.gaps("skill",
                     f"节点数超出步数预算（{plan.max_steps}），已裁剪可选节点：{'、'.join(plan.trimmed)}",
                     "本次证据链可能不完整，请留意证据缺口清单")

    harness.snapshot()
    return state


def _route_nodes(intent: str) -> list[str]:
    """取该意图的完整节点序列（用于对比规划后是否被裁剪）。"""
    from backend.agents.harness.router import route_for_intent

    return route_for_intent(intent)["nodes"]


def _label(intent: str) -> str:
    from backend.agents.harness.router import INTENT_ROUTES

    return (INTENT_ROUTES.get(intent) or {}).get("label", intent)


def intent_digest(state: LegalState) -> dict:
    """意图结果摘要，供 ReportNode 与接口层使用。"""
    return {
        "intent": state.get("intent") or "",
        "doc_type": state.get("doc_type") or "",
        "draft_type": state.get("draft_type") or "",
        "route": state.get("route") or [],
    }
