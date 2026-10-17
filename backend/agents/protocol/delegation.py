"""A2A 自主委派层：把"某个 Agent 需要另一个 Agent 的专业能力"做成一次受控的协议调用。

**为什么要有这一层，而不是各节点自己写 ``A2AClient``。**
``A2AClient`` 只管"把消息发出去、把回执收回来"，它是一条**裸通道**。
而真实委派要处理的问题远不止收发：

1. **委派会改写 ``state["route"]``。** 每个节点第一件事是 ``harness.step(...)``，
   而 ``Harness.step`` 会调 ``mark_route``；``mark_route``（``state_manager`` ）
   在节点不在 route 里时**直接 append**。于是委派给一个被预算裁掉的节点时：

       CaseNode 执行 → mark_route → CaseNode 被追加进 state["route"]
                   → graph.next_node 视其为 pending → 它在 ReportNode 之后又跑一遍
                   → 此时报告已生成，新类案根本进不去，还白烧一次配额

   所以本层在调用前保存 ``route``、在 ``finally`` 里恢复。**这件事只能做在这里** ——
   ``client`` 是给跨进程对端也用的裸通道，改它的语义会波及远端入口。

2. **两条通道的写回方式不同。** 进程内委派共用同一个 State dict，被调方写进去就等于
   生效了；**HTTP 不会** —— 回执里的 ``fields`` 只是 JSON，不并回本地 State 的话，
   委派方**静默地什么都没拿到**。本层统一用 ``harness.merge`` 写回，进程内是幂等去重、
   HTTP 是真的落库。

3. **委派是"另一个 Agent 的专业能力"，得有边界。** 无上限的委派会把步数预算和
   工具配额吃光，让后面排程的节点无米下炊。本层管住 depth / 次数 / 两档范围。

4. **必须留痕。** 委派绕过 ``graph._execute``，所以它没有 ``step`` 事件 ——
   不额外发 Trace 的话，界面上"哪个 Agent 找了哪个 Agent"完全看不见。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from backend.agents.protocol.client import client
from backend.agents.protocol.message import A2AAction, A2AArtifact
from backend.agents.protocol.registry import card_for
from backend.agents.state import A2A_FOCUS_KEY

logger = logging.getLogger("legal_agent.a2a.delegation")


# 委派方给被调方传检索式的键。被调方（Statute/Case/Knowledge）优先用它，
# 拿不到才退回自己按 plan 生成的检索式。
#
# 权威定义在 ``agents.state``（它首先是个 State 键），这里只是个别名 ——
# 各节点要从 ``state`` 引，从本模块引会成环。
FOCUS_KEY = A2A_FOCUS_KEY

# 委派的三个上限**已提到 settings**（2026-10-07）：``DELEGATION_MAX_IN_ROUTE`` /
# ``DELEGATION_MAX_EXPANSION`` / ``DELEGATION_MAX_DEPTH``，默认值与原先的模块常量
# 逐字一致，行为不变。提上来只为让它可调、可测。
#
# ⚠ **别在这里再定义一套同名常量。** 那正是 ``graph.MAX_LOOP`` 踩过的坑：
# 模块常量与 settings 各说各话，管理后台改的是 settings，而生效的是常量 ——
# 诊断接口报的数与实际拦住的不是一回事，且没有任何一处会报错。
# 所以这里只留注释，不留值；读的时候一律 ``_caps()``。
#
# ⚠ 也**没有**做"按 from_node 分桶"。曾计划过（怕 4 个 Agent 里先跑的那个把额度吃光），
# 但盘查后发现全项目**只有 AnalysisNode 一处**发起委派（``_collaborate`` 里两条 +
# ``_ensure_statute`` 里按条文逐条），单方分桶是纯粹的空转；更要紧的是它**会误伤**：
# AnalysisNode 合法的用量就是 2（类案 + 指引）再加最多 3 条缺条文的 Statute，
# 分桶一压就把它自己的正当委派挡在门外。哪天真出现第二个委派方，再按需要分桶。

# 写回时**不**走 merge 的字段。
# merge 是"去重追加列表"，对报告/置信度/草稿这类整体性结论没有意义 ——
# 它们是"被调方的最终话"，不是"新增的证据条目"。
# evidence_gap 也排除：缺口走 art.gaps 那条路，混进来会重复登记。
_NO_MERGE = frozenset({
    "report", "confidence", "draft", "draft_type", "revision_draft",
    "intent", "doc_type", "citation_check", "citation_summary",
    "limitation_check", "evidence_gap", "memory_written",
})

# **会读** ``focus_of(state)`` 的被调方 —— 就是这三个（各节点里那句
# ``focus = focus_of(state)``）。给它们委派时专项检索式是必填的。
#
# 反过来，Draft / Review / Validator / Report 这些压根不读它：对它们一律要求填，
# 等于逼调用方现编一句没有去处的话 —— 而那句话照样会进 Trace 的 detail 与 HTTP
# payload，看起来像"委派方说清楚了要什么"，实际被调方一个字没用上。
_FOCUS_READERS = frozenset({"StatuteNode", "CaseNode", "KnowledgeNode"})


def _caps() -> dict[str, int]:
    """当前生效的委派上限。**每次现读 settings**，不缓存。

    缓存（模块级常量、``lru_cache``、导入时快照）会让管理后台改了配置而这里不生效 ——
    而它没有任何症状，只有"把上限调到 1 想演示拦截"时才发现拦不住。
    """
    from backend.app.config import settings

    return {
        "in_route": int(getattr(settings, "DELEGATION_MAX_IN_ROUTE", 4)),
        "expansion": int(getattr(settings, "DELEGATION_MAX_EXPANSION", 1)),
        "depth": int(getattr(settings, "DELEGATION_MAX_DEPTH", 2)),
    }


@dataclass
class Delegation:
    """一次自主委派的意图。**由调用方构造，本层只负责执行**。

    ``from_node`` 必须显式给：委派发生在某个节点**执行途中**，而
    ``harness.executed`` 是在节点跑完**之后**才 append 的（``graph._execute``），
    所以从 ``executed[-1]`` 推断"当前是谁"会取到上一个节点，是错的。
    """

    target: str                      # Agent 名或节点名，如 "case" / "CaseNode"
    focus: str                       # 检索式/核对目标。**必填**，见 delegate()
    reason: str                      # 一句话说明"为什么我需要它"，进 Trace 给律师看
    from_node: str = "Harness"       # 委派方节点名（用于 Trace 与来源标注）
    action: A2AAction = A2AAction.RUN
    expansion: bool = False          # True = 越出起手范围（档二）
    extra_payload: dict[str, Any] = field(default_factory=dict)

    def dedup_key(self) -> tuple[str, str]:
        """去重键：同一个 Agent + 同一件事只委派一次。

        不含 ``action``：对同一个目标重复发 RUN 与 VERIFY 是没有意义的，
        两者都要落回"这件事已经委托过了"。
        """
        return (self.target, self.focus.strip())


class DelegationBudget:
    """本次任务的委派记账。

    **挂在 Harness 对象上，不放 State** —— 依据是 ``graph._executed`` 已经为
    同类问题给出的结论：State 在 LangGraph 里每个 superstep 都会被复制，
    计数一旦丢失，去重和上限就同时失效（前者导致重复劳动，后者导致失控）。
    """

    def __init__(self) -> None:
        self.sent: dict[tuple[str, str], A2AArtifact] = {}
        self.in_route: int = 0
        self.expansion: int = 0
        self.depth: int = 0

    def key(self, d: Delegation) -> tuple[str, str]:
        return d.dedup_key()


def budget_of(harness: Any) -> DelegationBudget:
    """取（或惰性建）该 Harness 的委派记账。

    惰性建是为了兼容旧调用点与测试里手搓的 Harness 替身 ——
    在 ``reset_runtime`` 里初始化会要求所有替身同步跟进。
    """
    budget = getattr(harness, "a2a_budget", None)
    if not isinstance(budget, DelegationBudget):
        budget = DelegationBudget()
        try:
            harness.a2a_budget = budget
        except Exception:  # noqa: BLE001 - 只读替身：退化成"每次新建"，不阻断委派
            logger.debug("Harness 不支持挂载委派记账，本次不设限")
    return budget


def in_start_scope(harness: Any, target: str) -> bool:
    """``target`` 是否在本次任务的**起手范围**内（供调用方在委派前自查）。

    调用方先自查一次，是为了避免"明知越界还发一次、被守卫拒掉并留一条告警"
    —— 那会在每次正常运行时都刷一条没有信息量的 Trace。

    ``target`` 可能是 Agent 名也可能 是节点名，两者都要认。
    """
    card = card_for(target)
    return (card.node if card else target) in _start_scope(harness)


def _intent_route(state: dict[str, Any]) -> set[str]:
    """本次意图的**静态路由**（按意图定的那套节点）。

    惰性导入 ``router``：``delegation`` 在 ``harness/__init__`` 的
    ``reset_runtime`` 里被间接引入，模块级再引 router 会把这条链绕成环。
    """
    intent = state.get("intent") or ""
    if not intent:
        return set()
    try:
        from backend.agents.harness.router import route_for_intent

        return set(route_for_intent(intent)["nodes"])
    except Exception:  # noqa: BLE001 - 意图没登记时退回"只认已执行的 route"
        logger.debug("无法解析意图 %s 的静态路由，起手范围退化为 state['route']", intent)
        return set()


def _start_scope(harness: Any) -> set[str]:
    """本次任务的**起手范围**：该意图的静态路由 ∪ 实际执行的 route ∪ 被预算裁掉的节点。

    ⚠️ 这里**不能**只看 ``state["route"]``。``intent_node`` 写的是
    ``state["route"] = plan.nodes()``，那是**规划裁剪之后**的序列 ——
    被砍掉的 CaseNode/KnowledgeNode 根本不在里面，而它们恰恰是最该被
    "叫回来"的那两个（``planner`` 里 ``required=node not in ("KnowledgeNode","CaseNode")``）。
    只认 route 会把最有价值的委派全部挡掉，改造就白做了。

    ⚠️ 也**不能**只看 ``route ∪ plan.trimmed``：``trimmed`` 只记**步数预算**砍掉的那些，
    而动态规划（``planning.draft_plan`` / ``refine_with_llm``）按问题裁剪节点时
    **不留痕**。实测过一次 ``case_analysis``：模型嫌"多跑一个检索节点就是多等几十秒"，
    把 KnowledgeNode 整个裁掉，``plan.trimmed`` 是空的、route 里也没有它 ——
    于是"本所指引池为空，Analysis 想找 Knowledge Agent 问问"这件事永远发不出去。

    把静态路由并回来，是为了让边界**由意图决定、而不是由模型这一轮的心情决定**：
    同一个「案件分析」，模型裁与不裁，"这个 Agent 算不算本次该找的人"答案一致。
    越出静态路由的才是档二（扩容），那才是真正该被质疑的一类。

    被 trimmed 是**因为预算**，不是因为不需要，所以它当然也在"起手范围"里。
    """
    state = getattr(harness, "state", None) or {}
    plan = getattr(harness, "last_plan", None)
    scope = set(state.get("route") or [])
    scope.update(getattr(plan, "trimmed", None) or [])
    scope |= _intent_route(state)
    return scope


def _apply_artifact(state: dict[str, Any], harness: Any, art: A2AArtifact,
                    target_node: str) -> int:
    """把回执里的产出并回调用方的 State。返回新增条目数。

    **进程内通道下这一步大多是空操作**（被调方写的就是这个 dict），
    但 HTTP 通道下它是唯一能把结果带回来的路。两条通道共用一份代码，
    是为了避免"进程内能用、拆出去就静默失效"这类只在部署形态切换时才暴露的缺陷。
    """
    if not art.ok:
        return 0
    added = 0
    for key, value in (art.fields or {}).items():
        if value is None or key in _NO_MERGE:
            continue
        if isinstance(value, list):
            if key in ("statutes", "cases", "knowledges"):
                added += harness.merge(state, key, value)
            else:
                # 其余列表字段（parties/facts/issues/…）没有统一的去重键，
                # 覆写式合并：只在本地为空时接管，绝不冲掉调用方已有的结论。
                if not state.get(key):
                    state[key] = value
                    added += len(value)
        elif not state.get(key):
            state[key] = value
    for gap in art.gaps or []:
        if isinstance(gap, dict):
            harness.gaps(gap.get("kind") or "a2a",
                         gap.get("reason") or f"{target_node} 未产出",
                         gap.get("impact") or "")
    return added


def _node_of(target: str) -> str:
    """``target`` 的标准**节点名**。

    它可能写成 Agent 名（"case"）也可能写成节点名（"CaseNode"）——
    卡片是唯一能统一两者的地方，卡片上就带着 node 字段。
    （原先这段表达式在 ``_room_for`` 与 ``delegate`` 里各抄了一份，
    而"哪个 target 算会读 focus 的"正是按节点名判的，抄漏一处就会静默判错。）
    """
    card = card_for(target)
    return card.node if card else target


def _pending_after(harness: Any) -> int:
    """本节点之后**还排着队**的节点数（``next_node`` 那句 pending 的同一个算式）。

    算的是 ``route`` 里还没进执行游标的那些。委派时当前节点**已经在**
    ``harness.executed`` 里（``graph._execute`` 在调节点函数**之前**就标记了），
    所以这里不会把自己算进去。
    """
    executed = set(getattr(harness, "executed", None) or [])
    route = (getattr(harness, "state", None) or {}).get("route") or []
    return len([n for n in route if n not in executed])


def _room_for(harness: Any, d: Delegation, budget: DelegationBudget) -> str:
    """还能不能委派。返回空串=可以，否则返回拒绝理由。"""
    caps = _caps()

    if budget.depth >= caps["depth"]:
        return f"委派链深度已达上限（{caps['depth']} 层）"

    node = _node_of(d.target)

    if node in _FOCUS_READERS and not (d.focus or "").strip():
        # 没有检索式的委派 = 让被调方重跑一遍全局检索，既贵又和调用方的意图无关。
        # 直接拒绝，且**不登记证据缺口**：这是调用方的判据写错了，不是证据链的缺陷。
        # ⚠ 只管**真的会读它**的那三个：对 Draft/Review/Validator 要求填 focus，
        #   是逼调用方现编一句没人看的话（见 _FOCUS_READERS）。
        return f"无检索式（focus 为空），已跳过委派"

    if node in _start_scope(harness):
        if budget.in_route >= caps["in_route"]:
            return f"起手范围内的委派次数已达上限（{caps['in_route']} 次）"
    elif not d.expansion:
        # 想越出起手范围却没显式声明 —— 多半是判据写错了，说清楚而不是默默放行。
        return f"{d.target} 不在本次起手范围内；确实需要请显式声明 expansion=True"
    elif budget.expansion >= caps["expansion"]:
        return f"扩容委派次数已达上限（{caps['expansion']} 次）"

    if harness.exhausted():
        # 被调节点自己会 harness.step() 记一步，所以步数将满时必须停手 ——
        # 否则委派会把预算吃光，后面排程的节点反而没得跑。
        return "步数预算已耗尽，不再发起委派"

    # ---- 预算检查：委派要**装得下它自己 + 后面还排着队的节点**
    # 只查 ``exhausted()`` 是不够的：它只说"已经用光了"，而这里要问的是
    # "被调方那一步跑完之后，排队的节点还有没有步数"。差一步的后果不是少跑一个节点，
    # 而是流程停在中途 —— 跑到 Validator 就没步数了、Report 不生成，
    # 交付物比不委派更差。与 ``graph._should_loop`` 的预算闸同一算式、同一理由。
    steps = int(dict(getattr(harness, "state", None) or {}).get("steps") or 0)
    pending = _pending_after(harness)
    from backend.app.config import settings

    if steps + 1 + pending > int(settings.MAX_STEPS):
        return (f"剩余步数不足以跑完后续排程（已用 {steps}/{settings.MAX_STEPS} 步，"
                f"后面还有 {pending} 个节点等着跑），不再发起委派")
    return ""


def _focus_for(state: dict[str, Any], d: Delegation, node: str) -> str:
    """本次委派实际用的专项检索式/核对目标。

    不读 ``focus`` 的被调方（Draft / Review / Validator …）允许留空，这时用
    ``state["query"]`` 兜底 —— 律师原话至少是有信息量的，比在 Trace 的 detail 和
    HTTP payload 里留一段空白强。Trace 的去重键是 ``(node, event, detail, ts)``，
    detail 里那一截正是它。
    """
    focus = (d.focus or "").strip()
    if focus:
        return focus
    if node in _FOCUS_READERS:
        return ""            # 不该走到这儿：``_room_for`` 已经把它拦下了
    return str(state.get("query") or "").strip()


async def delegate(harness: Any, d: Delegation,
                   *, landed: Callable[[dict[str, Any]], bool] | None = None
                   ) -> A2AArtifact | None:
    """执行一次自主委派。**永不抛异常** —— 失败返回 ``None``，由调用方决定怎么兜底。

    返回 ``None`` 的两种情形：被守卫拦下（无检索式/超预算/超范围），
    或通道失败。两者都会留下 Trace，调用方不必区分，"落回直调"对两者都成立。

    ``landed`` 是**结果验收**回调：A2A 回执 ``ok=True`` 只说明"被调方跑完了"，
    不等于"我要的那条法条真的进池子了"。这是本项目已踩过的坑
    （见 ``analysis_node._pool_has_statute``），所以验收口径由调用方给。
    """
    budget = budget_of(harness)
    state = getattr(harness, "state", None)
    if not isinstance(state, dict):
        logger.debug("无 State，跳过委派：%s", d.target)
        return None

    # 被调节点的标准名 + 实际要发出去的检索式。**后面每一处都用这两个**，
    # 不再回头用 ``d.target`` / ``d.focus``：留空的 focus 会被 query 兜底，
    # 一处用兜底值、一处用原值的话，Trace 里写着 A 而被调方按 B 干活，
    # 排障的人照着 Trace 核对，越核越不对。
    node = _node_of(d.target)
    focus = _focus_for(state, d, node)

    key = budget.key(d)
    if key in budget.sent:
        harness.tracer.info(
            d.from_node, "a2a_delegated",
            f"委派 {d.target} 已在本次任务中执行过（{focus[:40]}），跳过重复委派")
        return budget.sent[key]

    if (why := _room_for(harness, d, budget)):
        harness.tracer.warn(d.from_node, "a2a_delegation_fallback",
                            f"未委派 {d.target}：{why}")
        return None

    # ⛔ 委派会把被调节点的名字 append 进 route（mark_route），
    #    必须原样恢复，否则 graph.next_node 会把它当成还没跑的节点再调度一遍。
    route_before = list(state.get("route") or [])
    focus_before = state.get(FOCUS_KEY, None)
    had_focus = FOCUS_KEY in state

    budget.depth += 1
    try:
        state[FOCUS_KEY] = focus
        payload = {FOCUS_KEY: focus, **d.extra_payload}
        # 走 client() 工厂而不是 A2AClient(harness)：只有工厂会读 A2A_TRANSPORT，
        # 硬编码构造等于把 .env 里的通道配置变成一句空话。
        art = await client(harness).call(d.target, action=d.action, payload=payload)
    except Exception as exc:  # noqa: BLE001 - 委派失败不该让调用方的节点崩掉
        harness.tracer.warn(d.from_node, "a2a_delegation_fallback",
                            f"委派 {d.target} 异常，回退直调：{exc}")
        return None
    finally:
        budget.depth -= 1
        state["route"] = route_before
        if had_focus:
            state[FOCUS_KEY] = focus_before
        else:
            state.pop(FOCUS_KEY, None)

    if not art.ok:
        harness.tracer.warn(
            d.from_node, "a2a_delegation_fallback",
            f"委派 {d.target} 未完成（{art.transport} 通道），回退直调：{art.error or art.summary}")
        return None

    if landed is not None and not landed(state):
        harness.tracer.warn(
            d.from_node, "a2a_delegation_fallback",
            f"委派 {d.target} 回执 ok 但目标产出未落地，回退直调")
        return None

    added = _apply_artifact(state, harness, art, node)
    budget.sent[key] = art
    if d.expansion:
        budget.expansion += 1
        event = "a2a_expansion_delegated"
    else:
        budget.in_route += 1
        event = "a2a_delegated"

    # Trace 的 node 用**被调节点名**而不是委派方：任务详情（``/tasks/{id}``）
    # 与桌面端都直接展示这个 node，写委派方的话看不出"找了谁"。
    # ⚠️ detail 必须带上 target 与 focus —— merge_trace 的去重键是
    #    (node, event, detail, ts)，detail 不区分会让同一对 Agent 的
    #    两次不同委派被折叠成一条。
    harness.tracer.info(
        node, event,
        f"{d.from_node} → {d.target}｜{d.reason}｜检索式：{focus[:60]}"
        f"｜新增 {added} 条｜{art.transport} 通道 {art.elapsed_ms}ms")
    harness.flush_trace()
    return art


__all__ = [
    "FOCUS_KEY",
    "Delegation",
    "DelegationBudget",
    "budget_of",
    "delegate",
    "in_start_scope",
]
