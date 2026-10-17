"""LangGraph 状态机：把 11 个节点按意图路由串成可执行的图（方案 4.2）。

方案 4.2 的节点流转：
``IntentNode → IntakeNode → StatuteNode → CaseNode → RAGNode → Review/AnalysisNode
→ ValidatorNode → DraftNode → ReportNode``，**Validator 失败回环**。

实现上有一个必须交代的取舍：**图是"动态"的，不是一张固定的边表**。
Router 已经按意图给出了节点序列（``INTENT_ROUTES``），所以这里用
LangGraph 的 ``add_conditional_edges`` 在运行时按 ``state["route"]`` 决定下一个节点。
理由：

1. **简单任务不该跑完整状态机**（方案 4.1 原话）。法规查询的路径是
   ``Statute → Validator → Report``，只有 3 个节点；若落成固定边表，
   就得为每种意图各建一张图并在入口做分发，反而更难看清"哪条路径漏了节点"。
2. **裁剪节点是常态**。Planner 在节点数超出 ``max_steps`` 时会裁掉可选节点，
   固定边表无法表达"这次不跑 CaseNode"。
3. 一张图 + 一个 ``next_node`` 函数，路径正确性只需验证一个函数，
   而路径就是 ``state["route"]``，前端可以直接把它画出来给律师看。

---------------------------------------------------------------------------
与 LangGraph 的边界：三条实测结论

接 LangGraph 时踩了三个坑，都会造成"看着在跑、其实没生效"，所以记在这里：

1. **未在 ``LegalState`` 里声明的键会被丢掉**。``StateGraph(LegalState)`` 只按
   TypedDict 的注解建 channel，输入里的额外键不会进图。所以
   "把 Harness 塞进 ``state["_runtime"]`` 传下去"是行不通的 ——
   改用 :data:`_CURRENT` 这个 ContextVar（实测 async 节点可正常取到）。
2. **节点函数只接收 ``state`` 一个参数**。原先写成 ``_node(state, harness)``
   会直接抛 ``TypeError: missing 1 required positional argument: 'harness'``。
   所以图节点与顺序执行器各用一个包装器（:func:`_graph_node` / :func:`_exec_node`），
   共用同一个 :func:`_execute` 内核。
3. **LangGraph 每个 superstep 会复制 State，且条件边函数里对 State 的写入不持久化**
   （实测：写在条件边里的 trace 事件全部丢失；而写普通 Python 对象则一定生效）。
   这条有两个后果，都是要命的：
   - ``Harness.state`` 必须**在每个节点执行前重新绑到该节点的 State 对象**上。
     否则 Harness 写的 trace / steps / evidence_gap 全落在上一轮的副本里，
     节点返回后就丢了（这些函数都是给键重新赋值，不是原地改列表，所以不会外泄）。
   - 回环计数、执行游标必须挂在 **Harness 对象**上而不是 State 上。
     回环计数一丢就是死循环（只能靠 ``max_steps`` 兜底）。

回环控制（方案 4.3 四层防死循环）：
- 第一层 ``max_steps``：``Harness.step()`` 每次 +1，超限即终止，由 ``_should_loop`` 检查；
- 第二层工具配额：``statute_search ≤3 / case_search ≤2``，在 ToolExecutor 强制；
- 第三层状态增量判断：``has_new_info`` 比对语义键集合，回环一次没拿到新证据就不再回环；
- 第四层 Validator 终检：拦截后发现"回环也无新证据"时停止回环，带缺口交付。

**回环最多一次**（``MAX_LOOP``）。这不是偷懒：回环的目的是"引用没核过，回去再检一轮"，
而配额只有 3+2 次检索机会，第二轮如果还没拿到新证据，再转下去只会耗光预算。
与其无限转，不如带证据缺口交付 —— 方案第十三章的原则是"宁可缩小结论范围，
也不假装有证据"。
"""

from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar
from typing import Any, Awaitable, Callable

from backend.agents.harness import Harness
from backend.agents.harness.error_handler import ErrorHandler
from backend.agents.harness.state_manager import has_new_info
from backend.agents.nodes.analysis_node import analysis_node
from backend.agents.nodes.case_node import case_node
from backend.agents.nodes.draft_node import draft_node
from backend.agents.nodes.intake_node import intake_node
from backend.agents.nodes.intent_node import intent_node
from backend.agents.nodes.knowledge_node import knowledge_node
from backend.agents.nodes.memory_node import memory_node
from backend.agents.nodes.report_node import report_node
from backend.agents.nodes.review_node import review_node
from backend.agents.nodes.statute_node import statute_node
from backend.agents.nodes.validator_node import validator_node
from backend.agents.state import LegalState

logger = logging.getLogger("legal_agent.graph")

# 回环上限已提到 ``settings.MAX_LOOP``（2026-10-07），默认 1、语义不变。
# 原先硬编码在这里，缺点是"要改它得动代码"——而调它必须同步调 ``MAX_STEPS``
# （见 config.py 里那条注释），硬编码使得这个联动只存在于注释里、没法在部署时调。
# ⚠ 别在这里再定义一个同名的模块常量：它会和 settings 各说各话，
# ``graph_status()["max_loop"]`` 报的数与实际生效的不是一回事。
LOOP_BACK_NODES = ("StatuteNode", "CaseNode")   # 回环时重新检索的节点
EVIDENCE_FIELDS = ("statutes", "cases", "knowledges")

# 回环时必须**跟着重跑**的节点（在 route 里位于回检点之后的）。
# 只重跑检索节点是不够的：不重跑 ValidatorNode，新检索到的法条永远不会被重新核验，
# 引用状态还是 fail；不重跑 ReportNode，交付物还是旧的结论。
# 这两步正是回环的目的（"补证据后重新终审"），缺一不可。
LOOP_RERUN_TAIL = ("ValidatorNode", "ReportNode")
# 回环重跑不需要的节点：知识库与材料在两轮之间不会变化
LOOP_SKIP = ("KnowledgeNode", "IntakeNode", "MemoryNode")

# 图递归上限：11 个节点 + 一次回环最多约 16 个 superstep，50 是安全余量。
# 真正的死循环防护是 MAX_STEPS 与 _should_loop，不靠这个值兜底。
RECURSION_LIMIT = 50

# 跨节点并行组（2026-10-07，第三轮耗时优化）。
# 每组内的节点**互相独立**（各自写不同的证据字段：法条/类案/知识、扫描/分析），
# 可以并发执行；但每组在 route 中必须**相邻**（调度器按 pending 连续段识别）。
# 关键约束：并行不得破坏前端 SSE 执行轨迹 —— 做法是批启动时**按 route 顺序
# 预发 step 事件与执行游标**，节点并发执行时经 Harness._prestepped 消费式跳过
# 重复 step；批内节点的 executed 顺序由预发阶段保序，used_nodes 展示不乱。
PARALLEL_GROUPS: tuple[tuple[str, ...], ...] = (
    ("StatuteNode", "CaseNode", "KnowledgeNode"),
    ("ReviewNode", "AnalysisNode"),
)


def _batch_name(nodes: list[str] | tuple[str, ...]) -> str:
    # 分隔符不能用 ``:`` —— LangGraph 的 StateGraph.add_node 明确拒绝含 ':' 的
    # 节点名（"':' is a reserved character"），会在 build_graph() 阶段直接抛错。
    # 而 build_graph() 的调用点没有 try 兜底，异常会被 orchestrator 吞成
    # "证据缺口"、接口照样返 200 但 counts 全 0 —— 整条链路静默失效。
    # 批名只作 NODES / BATCH_NODES 的 key，harness.executed 不记批名，
    # 因此换前缀不会影响前端执行轨迹。
    return "BATCH__" + ",".join(nodes)


NodeFn = Callable[[LegalState, Harness], Awaitable[LegalState]]

# 节点名 → 节点函数。与 harness.router.NODE_AGENT 的键必须一致。
NODES: dict[str, NodeFn] = {
    "IntentNode": intent_node,
    "IntakeNode": intake_node,
    "StatuteNode": statute_node,
    "CaseNode": case_node,
    "KnowledgeNode": knowledge_node,
    "ReviewNode": review_node,
    "AnalysisNode": analysis_node,
    "ValidatorNode": validator_node,
    "DraftNode": draft_node,
    "MemoryNode": memory_node,
    "ReportNode": report_node,
}

# 并行批节点名 → 批内成员（按 route 顺序）。
# 注册进 NODES 后，顺序执行器与 LangGraph 图都能像普通节点一样调度它。
BATCH_NODES: dict[str, tuple[str, ...]] = {}


def _batch_fn(group: tuple[str, ...]) -> NodeFn:
    """顺序执行器用的并行批节点：显式接收 harness，gather 并发跑批内成员。"""

    async def _node(state: LegalState, harness: Harness) -> LegalState:
        return await _parallel_batch(state, harness, group)

    _node.__name__ = f"batch_{_batch_name(group)}"
    return _node


for _group in PARALLEL_GROUPS:
    _bname = _batch_name(_group)
    BATCH_NODES[_bname] = _group
    NODES[_bname] = _batch_fn(_group)

# 图入口节点。**入口写在这里而不是只写在 build_graph 里**：
# 顺序执行器没有 LangGraph 的 set_entry_point，如果入口只存在于图的构造代码里，
# 顺序执行器第一次调 next_node 时 route 还是空的（route 由 IntentNode 写入），
# 于是 pending 为空、直接返回 END —— 整条流程一步都不跑。
# 表现是"顺序执行器 0 步、图 8 步"，两者对拍全不一致，
# 而"对拍"正是判断图有没有写错的唯一手段，失真后等于没有回归保障。
ENTRY = "IntentNode"

# 图终止节点（本模块自己的哨兵值，随后映射到 LangGraph 的 END）
END = "__end__"

# 当前任务的 Harness。
# 为什么用 ContextVar 而不是全局变量：并发任务会互相污染（一个任务的节点
# 拿到另一个任务的 Harness，Trace 与工具配额全乱）。ContextVar 按 asyncio
# 任务隔离，正是这个场景需要的语义。
_CURRENT: ContextVar[Harness | None] = ContextVar("legal_agent_harness", default=None)


def current_harness() -> Harness | None:
    """取当前 asyncio 任务绑定的 Harness（未绑定时返回 None）。"""
    return _CURRENT.get()


# --------------------------------------------------------------------------- 路由
def next_node(state: LegalState, harness: Harness | None = None) -> str:
    """决定下一个节点。图的所有分支逻辑集中在这一个函数里。

    返回 :data:`END` 表示流程结束。

    **撤回（rewind）就写在决策里**，不交给调用方。原因：回环要不要撤、
    撤哪些节点，和"下一步去哪"是同一个判断；拆成两处调用，
    早晚会有一条执行通道忘了撤，表现为"回环转了一圈但结论没变"。
    """
    route = list(state.get("route") or [])
    done = _executed(state, harness)

    # 第一步永远是 IntentNode：它负责判定意图、生成 route（后续所有分支都依赖 route）。
    # 这一步不能省，也不能让 route 为空时就判"跑完了"。
    if not done:
        return ENTRY

    # 当前该执行的节点 = route 里第一个还没执行过的
    pending = [n for n in route if n not in done]
    if pending:
        # 预算耗尽而仍有待执行节点 —— 这是**唯一**的真正截断点，
        # 也是唯一该登记"步数不够"缺口的地方（见 state_manager.bump_step 的说明）。
        if harness is not None and harness.exhausted():
            from backend.app.config import settings

            note = (f"步数预算（{settings.MAX_STEPS} 步）已耗尽，"
                    f"{'、'.join(pending)} 未执行")
            harness.tracer.warn("Graph", "budget_stop", note)
            harness.gaps("tool", note,
                         "该部分证据未采集，本次结论的范围相应收窄，需律师补充核查")
            return END
        first = pending[0]
        # 跨节点并行：pending 开头是某个并行组的**连续段**且至少 2 个成员时，
        # 返回批节点名（内部按 route 顺序预发 step 事件再并发执行）。
        # 只有连续段才合并：中间隔着别的节点会破坏前端"做完一批再走下一批"的轨迹。
        if harness is not None:
            for group in PARALLEL_GROUPS:
                if first not in group:
                    continue
                seg: list[str] = []
                for n in pending:
                    if n in group:
                        seg.append(n)
                    else:
                        break
                # 只有**完整组**才合并成批（批名按完整组注册进了 NODES）：
                # 部分成员已执行时返回未注册的"子组批名"会让执行器撞 unknown_node。
                if len(seg) >= 2 and seg == list(group):
                    return _batch_name(seg)
        return first

    # route 全部执行完 → 检查是否需要因引用终审失败而回环
    if harness is not None and _should_loop(state, harness):
        rerun = _rerun_nodes(state)
        if rerun:
            _rewind(harness, rerun)
            return rerun[0]
    return END


def _visited_from_trace(state: LegalState) -> list[str]:
    """从 Trace 的 step 事件还原执行轨迹（按首次出现顺序）。

    这是**无运行时**时的退路：Trace 是流程的真实记录，而它落在 State 里，
    所以在"只验证图结构、不建 Harness"的测试里仍然可用。
    有运行时的时候不用它 —— 回环会从 Trace 里删掉事件，而删改 State
    在 LangGraph 里是不保证的（见模块 docstring 第 3 条）。
    """
    out: list[str] = []
    for e in state.get("trace") or []:
        if (e.get("event") or "") == "step":
            node = e.get("node") or ""
            if node and node not in out:
                out.append(node)
    return out


def _executed(state: LegalState, harness: Harness | None) -> list[str]:
    """已执行节点序列（调度游标）。

    优先用 Harness 上的 ``executed``：它是普通 Python 对象上的列表，
    改了必然生效；而 State 在 LangGraph 里每个 superstep 都会被复制。
    """
    if harness is not None:
        return list(harness.executed)
    return _visited_from_trace(state)


def _should_loop(state: LegalState, harness: Harness) -> bool:
    """是否需要因引用终审未通过而回环重检。

    这个函数把"想回环"和"**回得起**"分开判断 —— 后者常被忽略，但更要紧：
    步数预算不足时启动回环会把流程停在中途（跑到 Validator 就没步数了，Report 不生成），
    交付物反而比不回环更差。所以宁可明确报告"预算不足，带缺口交付"。
    """
    if (state.get("citation_check") or "") != "failed":
        return False

    from backend.app.config import settings

    max_loop = int(settings.MAX_LOOP)
    if harness.loop_count >= max_loop:
        logger.info("引用终审未通过，但回环次数已达上限（%d），带证据缺口交付", max_loop)
        harness.tracer.warn("Graph", "loop_abort",
                            f"引用终审未通过，但回环次数已达上限（{max_loop} 次），"
                            "带证据缺口交付")
        return False

    if harness.exhausted():
        harness.tracer.warn("Graph", "loop_abort", "引用终审未通过，但步数预算已耗尽，带证据缺口交付")
        return False

    if (harness.quota_used("statute_search") >= settings.STATUTE_SEARCH_MAX_CALLS
            and harness.quota_used("case_search") >= settings.CASE_SEARCH_MAX_CALLS):
        harness.tracer.warn("Graph", "loop_abort",
                            "引用终审未通过，但检索配额已用尽，带证据缺口交付")
        return False

    # ---- 预算检查：回环要重跑的几个节点必须装得下
    rerun = _rerun_nodes(state)
    if not rerun:
        return False
    steps = int(state.get("steps") or 0)
    if steps + len(rerun) > settings.MAX_STEPS:
        harness.tracer.warn(
            "Graph", "loop_abort",
            f"引用终审未通过，但剩余步数不足以重跑 {'、'.join(rerun)}"
            f"（已用 {steps}/{settings.MAX_STEPS} 步），带证据缺口交付",
        )
        return False

    # ---- 第三层：回环必须能带来新证据。没有新证据就不要白转一圈
    before = harness.loop_snapshot or {}
    if before and not has_new_info(before, state, EVIDENCE_FIELDS):
        logger.info("引用终审未通过，但本次检索未带来新证据，停止回环")
        harness.tracer.warn(
            "Graph", "loop_abort",
            "引用终审未通过，但本轮未检索到新的法条/类案，继续回环不会改变结论，"
            "已停止回环并带证据缺口交付",
        )
        return False

    harness.tracer.info("Graph", "loop_plan",
                        f"引用终审未通过，计划重跑：{'、'.join(rerun)}")
    return True


def _rerun_nodes(state: LegalState) -> list[str]:
    """回环需要重跑的节点序列（检索节点 + 后续必须重跑的收尾节点）。

    按 route 顺序取"位于回检点之后"的节点，再排除不需要重跑的
    （知识库、材料解析在两轮之间不会变，重跑只是浪费步数）。
    """
    route = list(state.get("route") or [])
    first = next((n for n in route if n in LOOP_BACK_NODES), "")
    if not first:
        return []   # 本次路径里没有检索类节点，回环无从下手
    idx = route.index(first)
    tail = [n for n in route[idx:] if n not in LOOP_SKIP]

    # 不变式：回环必须重新核验、重新交付。
    # 把 LOOP_RERUN_TAIL 从"注释里的约定"变成可检查的约束 ——
    # 否则哪天有人把 Validator 加进 LOOP_SKIP，回环就会退化成"转一圈但结论没变"，
    # 而这种退化在日志里看不出任何异常。
    missing = [n for n in LOOP_RERUN_TAIL if n in route and n not in tail]
    if missing:  # pragma: no cover - 配置写错才会走到
        logger.error("回环重跑序列缺少收尾节点：%s（回环将不会重新核验引用）", missing)
    return tail


# --------------------------------------------------------------------------- 图
def build_graph(*, checkpointer: Any = None):
    """构造 LangGraph 状态图。

    使用 ``add_conditional_edges`` 而非固定边表，理由见模块 docstring。
    图结构为：每个节点都按 ``next_node`` 决定去向，因此新增/裁剪节点
    不需要改图的构造代码。
    """
    from langgraph.graph import END as LG_END
    from langgraph.graph import StateGraph

    graph = StateGraph(LegalState)

    for name, fn in NODES.items():
        graph.add_node(name, _graph_node(name, fn))

    graph.set_entry_point(ENTRY)

    # 所有节点共用同一个分支函数：下一步由 route + 执行游标决定。
    # 必须先注册 start 节点再无条件边；LangGraph 要求每个节点显式声明出口。
    path_map = {**{n: n for n in NODES}, END: LG_END}
    for name in NODES:
        graph.add_conditional_edges(name, _branch, path_map)

    return graph.compile(checkpointer=checkpointer) if checkpointer else graph.compile()


def _branch(state: LegalState) -> str:
    """LangGraph 分支函数：只**读** State 与运行时决定去向。

    **刻意不在这里写 State**：条件边函数里对 State 的写入不会持久化（实测），
    写了也是白写，反而容易让人以为"我在分支里记了一笔"。
    需要改的东西全部走 Harness（普通对象，改了必然生效）。
    """
    h = current_harness()
    if h is None:
        # 没有运行时（例如只验证图能不能编译、路径通不通）：按 route 顺序推进，
        # 回环与撤回都不做 —— 没有 Harness 就没有地方可靠地记账。
        done = _visited_from_trace(state)
        if not done:
            return ENTRY
        route = list(state.get("route") or [])
        pending = [n for n in route if n not in done]
        return pending[0] if pending else END
    return next_node(state, h)


def _graph_node(name: str, fn: NodeFn) -> Callable[[LegalState], Awaitable[LegalState]]:
    """包装成 LangGraph 节点：**只接收 state 一个参数**。

    LangGraph 按函数签名决定传什么。多一个必填参数就会在第一个节点上抛
    ``TypeError: missing 1 required positional argument`` —— 而且报错发生在
    LangGraph 内部的任务包装里，堆栈指向 ``During task with name 'IntentNode'``，
    不容易一眼看出是签名问题。所以这里刻意只留一个参数，Harness 走 ContextVar。
    """

    async def _node(state: LegalState) -> LegalState:
        h = current_harness()
        if h is None:
            raise RuntimeError(
                f"节点 {name} 未绑定 Harness 运行时：请通过 graph.run() 执行"
                f"（或测试里用 graph._run_sequential 显式传 harness）"
            )
        return await _execute(name, fn, state, h)

    _node.__name__ = f"node_{name}"
    return _node


def _exec_node(name: str, fn: NodeFn) -> NodeFn:
    """顺序执行器用的包装器：显式接收 harness（不依赖 ContextVar）。"""

    async def _node(state: LegalState, harness: Harness) -> LegalState:
        return await _execute(name, fn, state, harness)

    _node.__name__ = f"seq_node_{name}"
    return _node


async def _execute(name: str, fn: NodeFn, state: LegalState, harness: Harness) -> LegalState:
    """节点执行内核：统一异常处理 + 执行游标 + 回环记账。

    顺序执行器与 LangGraph 图共用这一个内核 —— 如果两条路径各写一份，
    测试里对拍就失去意义了（对拍的前提是"只有一处逻辑，两条执行通道"）。

    **Harness 重新绑定 State 是这里最要紧的一步**：LangGraph 每个 superstep
    交给节点的都是新的 dict，而 Harness 上还指着上一次的那个。不重绑的话
    ``flush_trace()`` / ``add_evidence_gap()`` / ``bump_step()`` 写的全是
    上一轮的副本，节点返回后就丢了 —— 表现为 Trace 只剩最后一条、
    证据缺口清单空着、步数永远是 0，而流程照跑不误。
    """
    harness.rebind(state)
    _enter_loop_if_rerun(state, harness, name)
    _mark_executed(harness, name)

    try:
        out = await fn(state, harness)
    except Exception as exc:  # noqa: BLE001
        verdict = harness.error.handle(
            harness.ctx, state, tool=name, agent=name, exc=exc,
        )
        action = (verdict or {}).get("action") or "continue"
        detail = ErrorHandler.explain(name, f"{type(exc).__name__}: {exc}")
        if action == "abort":
            # FATAL_ERRORS（权限不足/文件不存在/参数非法）：这类错误继续跑
            # 只会产出更离谱的结论，所以必须终止，由接口层把 failed 状态
            # 连同成因一起返回给律师。
            harness.tracer.error(name, "aborted", detail)
            harness.flush_trace()
            raise
        harness.tracer.error(name, "node_failed", f"{detail}｜处理方式：{action}")
        harness.gaps("tool", detail, "该步骤未能完成，相关证据缺失")
        harness.flush_trace()
        out = state

    # 节点跑完（或软失败）之后的一轮**自主裁决**。见 _controller_review 的 docstring：
    # 放在这里而不是 next_node，是因为"思考"要发 HTTP、"调工具"是 async，
    # 而 next_node 是 LangGraph 的条件边、必须是同步函数。
    # ⚠ 并行批内的节点**跳过**这里的裁决：批里先完成的节点不能因为
    # "游标里大家都已预发"而抢在批结束前裁决 —— 那会让未跑完的批成员被误判成
    # "已全部执行"，控制器在证据不全时提前介入。批内成员全部跑完后，
    # _parallel_batch 会统一调一次（时机与串行"最后一个节点跑完"等价）。
    # 批节点自身（BATCH__*，LangGraph 外层包装会再进一次 _execute）也跳过，
    # 否则一次批执行会触发两轮裁决。
    if name not in BATCH_NODES and not harness._prestepped:
        await _controller_review(out, harness)
    # 裁决写完的事件**立刻合并进 State**。上面两条 ``flush_trace()`` 都在
    # 节点体内，而控制器活在节点之外 —— 它写的事件要等下一个节点的收尾才会被合并。
    # 而"裁决之后直接收尾"恰恰是最常见的一条路（补检无新增、预算不足、没有候选动作），
    # 那时最后一个节点已经收过尾了，这些事件就永远进不了 ``state["trace"]``：
    # 表现为 ``GET /tasks/{id}/trace`` 里看不到"是哪一层闸止住的"，
    # 而实时 SSE 上明明推过一句 —— 两条路径对不上，且都不报错。
    # ``flush_trace`` 是**合并**（只增不减），多调一次不会有副作用。
    harness.flush_trace()
    return out


async def _parallel_batch(state: LegalState, harness: Harness,
                          group: tuple[str, ...]) -> LegalState:
    """并行执行一组互相独立的节点，同时保持前端 SSE 轨迹的 step 顺序。

    做法分两段：

    1. **预发阶段**（同步、按 route 顺序）：对批内每个成员先调 ``harness.step``
       （SSE step 事件 + 步数计数 + route 标记），再推进执行游标 ``executed``。
       于是前端实时轨迹与最终 ``used_nodes`` 都保持 route 原序，
       与串行时代完全一致 —— 并行只发生在证据采集的内部，不改变展示。
    2. **执行阶段**：``asyncio.gather`` 并发跑批内成员。成员执行时再调
       ``harness.step`` 会命中 ``harness._prestepped`` 被消费式跳过，
       不会重复发事件/重复计数。批内全部跑完后统一做一次自主裁决
       （与串行"最后一个节点跑完"时机等价）。

    批内成员写不同的证据字段（法条/类案/知识、扫描/分析），
    并发写 State 不会互相覆盖；Trace 合并与配额记账都是线程安全的
    （asyncio 单线程事件循环 + 只增不减的合并语义）。
    """
    route = list(state.get("route") or [])
    names = tuple(n for n in group if n in route)
    if not names:
        return state

    # ---- 预发：按 route 顺序发 step 事件、推执行游标（保序）----
    for n in names:
        harness.step(n)
        _mark_executed(harness, n)
    harness._prestepped = set(names)
    try:
        await asyncio.gather(*(_execute(n, NODES[n], state, harness) for n in names))
    finally:
        harness._prestepped = set()
    # 批内全部完成后，统一触发一次自主裁决（时机 = 串行最后一个节点跑完）
    await _controller_review(state, harness)
    return state


async def _controller_review(state: LegalState, harness: Harness) -> None:
    """一轮跑完后的自主裁决，并把裁决结果落到流程上（2026-10-07 加）。

    **触发点**：``route`` 上**最后一个待跑节点**执行完时。一次裁决 = 一次
    ``chat_json``（本地模型 2~8s），每个节点后都问一遍的话，8 节点链路要多等
    16~64s，而多数节点之后证据状态并不会改变结论 —— 律师等不起，而他下次就不问了。
    只在"这一轮跑完了"这个点上问一次，典型新增 1 次调用。

    **决策与执行分开**：想不想、调不调工具，在
    :class:`~backend.agents.harness.controller.Controller` 里；
    "把谁排回路线"在这里。理由是 ``_rewind`` 的既有不变式 ——
    "撤回与调度是同一个判断"（见 :func:`next_node` 的 docstring），
    所以撤回必须留在本模块，不能让控制器隔着模块去调它。

    **约束 2 的闭合点就在下面 ``_rewind(harness, tail)`` 那一行。**
    控制器直调检索工具拿到的法条/案号，光并进证据池是不够的：终审必须**再跑一遍**，
    否则那批新证据是"没经过 ``ValidatorNode`` 就进了交付物"。这里撤掉
    :data:`LOOP_RERUN_TAIL`（ValidatorNode + ReportNode）的执行游标，
    ``next_node`` 就会重新调度它们 —— 新版报告里的每一条引用都会重新过终审。
    """
    if harness is None or not state.get("route"):
        return
    route = list(state.get("route") or [])
    if [n for n in route if n not in harness.executed]:
        return  # 还有排队的节点，没到裁决点

    try:
        decision = await harness.controller.review(state, harness)
    except Exception as exc:  # noqa: BLE001 - 可选增强绝不能带崩已跑完的流程
        logger.warning("自主裁决异常（按不介入处理）：%s", exc)
        return

    if decision is None or decision.kind != "call_tool" or decision.added <= 0:
        return

    # ---- 约束 2：补进来的证据必须重新过终审 ----
    # 依次满足三个条件才排重跑，任一不成立就"带证据缺口交付"：
    tail = [n for n in LOOP_RERUN_TAIL if n in route]
    if not tail:
        # 路线里压根没有终审段（如 off_topic 只有 ReportNode）。
        # 那种意图本来就不检索、不终审，控制器也不该介入到这一步。
        return
    if harness.exhausted():
        logger.info("自主补证据后步数已尽，不排重跑（带缺口交付）")
        harness.tracer.warn("Controller", "no_budget",
                            "补检到了新证据，但步数预算已耗尽，未能重跑引用终审")
        return

    from backend.app.config import settings

    steps = int(state.get("steps") or 0)
    if steps + len(tail) > settings.MAX_STEPS:
        # 与 _should_loop 的预算检查同一算式、同一理由：宁可明确报告"预算不足，
        # 带缺口交付"，也不要启动一次必定卡在中途的重跑 —— 跑到 Validator 就没步数了，
        # Report 不生成，交付物比不重跑更差。
        note = (f"补检到了新证据，但剩余步数不足以重跑 {'、'.join(tail)}"
                f"（已用 {steps}/{settings.MAX_STEPS} 步），带证据缺口交付")
        logger.info(note)
        harness.tracer.warn("Controller", "no_budget", note)
        return

    _rewind(harness, tail)
    harness.tracer.info(
        "Controller", "rerun",
        f"补检后重跑 {'、'.join(tail)}，让新证据重新过引用终审",
    )


def _mark_executed(harness: Harness, name: str) -> None:
    """推进执行游标；重复执行即回环，此时把计数记账在 Harness 上。

    并行批的预发阶段已按 route 顺序把批内成员全部记入游标，
    成员并发执行时这里**去重追加**，保证 used_nodes 顺序不乱。
    ``BATCH__*`` 是调度壳：游标只记真实成员（预发阶段已记），
    批名本身不进 ``harness.executed`` —— 否则前端执行轨迹会冒出伪节点名。
    """
    if name in BATCH_NODES:
        return
    if name not in harness.executed:
        harness.executed.append(name)


def _enter_loop_if_rerun(state: LegalState, harness: Harness, name: str) -> None:
    """判断本次执行是不是回环重跑；是则维护回环计数与证据快照。

    判据是 ``harness.node_runs`` 里该节点的累计执行次数 —— 同一个节点
    在一次任务里只应执行一次，出现第二次就是回环重跑。
    为什么不用"Trace 里是否已有 step 事件"：撤回会删 Trace 事件
    （见 :func:`_rewind`），删完就分不清"首轮"和"第二轮"，回环计数会永远停在 0，
    直接死循环。所以"是否重跑"和"调度到了哪一步"必须用两套记录。
    """
    runs = int(harness.node_runs.get(name, 0))
    harness.node_runs[name] = runs + 1

    if name not in LOOP_BACK_NODES or runs < 1:
        return  # 首轮执行

    harness.tracer.warn("Graph", "loop_back",
                        f"引用终审未通过，回到 {name} 补充检索（第 {runs} 次回环）")
    harness.loop_count = max(harness.loop_count, runs)

    # 证据快照只在回环的**第一个**检索节点前拍一次：
    # 拍两次会把"本轮新增的证据"算进基线，第三层的增量判断就永远判不出新增。
    route = state.get("route") or []
    first_lb = next((n for n in route if n in LOOP_BACK_NODES), "")
    if name == first_lb:
        harness.loop_snapshot = {f: list(state.get(f) or []) for f in EVIDENCE_FIELDS}


def _rewind(harness: Harness, nodes: list[str] | tuple[str, ...]) -> None:
    """把指定节点从执行游标里撤回，使 :func:`next_node` 会重新调度它们。

    为什么不改 ``state["route"]``：route 是**计划**（IntentNode 依 Planner 写入），
    计划不该被回环改写 —— 否则前端展示的"本次打算做什么"会变成一串重复节点，
    律师看到的路径与实际执行顺序对不上。
    真实执行轨迹由 Trace 与 ``harness.executed`` 承载。
    """
    drop = set(nodes)
    harness.executed = [n for n in harness.executed if n not in drop]
    logger.info("回环撤回节点：%s", "、".join(nodes))


# --------------------------------------------------------------------------- 运行
async def run(
    state: LegalState,
    *,
    harness: Harness | None = None,
    use_graph: bool = True,
    **overrides: Any,
) -> LegalState:
    """执行一次完整流程。

    ``use_graph=False`` 时用内置的顺序执行器（不依赖 langgraph）。
    保留这条路径的原因不是"降级"，而是**可测试性**：
    顺序执行器把图的动态分支逻辑用最直白的 Python 复现一遍，两者结果一致
    才能说明"图的分支没写错"。测试里对拍两条路径是很便宜的回归保障。
    """
    h = harness or Harness.start(state, **overrides)
    h.begin_task()
    h.rebind(state)
    token = _CURRENT.set(h)
    try:
        if use_graph and _graph_available():
            compiled = build_graph()
            result = await compiled.ainvoke(state, config={"recursion_limit": RECURSION_LIMIT})
            # LangGraph 返回的是各 channel 的最终值：未写入过的 channel 是 None，
            # 不能拿它去覆盖调用方已有的默认值（会被清成 None）。
            for k, v in result.items():
                if v is not None:
                    state[k] = v   # type: ignore[literal-required]
        else:
            if use_graph:
                logger.warning("langgraph 不可用，已退回内置顺序执行器（流程语义一致）")
            await _run_sequential(state, h)
        return state
    finally:
        _CURRENT.reset(token)
        h.rebind(state)
        # 回环次数写在 Harness 上，落回 State 供前端/测试查看（图模式下落不进图，
        # 因为它不是 LegalState 声明的字段）
        state["_loop"] = h.loop_count  # type: ignore[typeddict-unknown-key]
        # 本次实际跑过哪些节点。**去重且保序** —— 它回答的是"这次用了哪几个节点"，
        # 不是"一共跑了几步"：回环会把同一个节点跑第二遍（``_rewind`` 先从游标里
        # 撤掉再重新追加），那时 ``len(executed)`` 不变而真实步数已 +N。
        # 步数的真源是 ``state["steps"]``（``Harness.step()`` 累加，MAX_STEPS 读它），
        # 两个数不一样，别互相替代 —— 前端 done 帧那行摘要是拿这个的条数说的。
        state["used_nodes"] = list(dict.fromkeys(h.executed))  # type: ignore[typeddict-unknown-key]
        # 兜底收尾。正常路径上 ReportNode 已经调过 finish()，这里只在
        # "ReportNode 被裁剪/执行失败/流程中途 abort"时补一次 ——
        # 那种情况下如果放任不管，任务会带着 confidence=0.0、status 未落库的状态返回，
        # 前端看到的就是一个"跑完了但什么都没说"的任务。
        if not h.finalized:
            try:
                h.finish()
            except Exception as exc:  # noqa: BLE001 - 收尾失败不能吃掉已有的结果
                logger.warning("收尾（置信度/快照）失败，已保留既有结果：%s", exc)


def _graph_available() -> bool:
    try:
        import langgraph.graph  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


async def _run_sequential(state: LegalState, harness: Harness) -> LegalState:
    """顺序执行器：按 route 推进，复现图的分支与回环逻辑。

    刻意复用与图**同一套** ``next_node`` / :func:`_execute`，
    而不是另写一遍循环：如果两条路径各写一份，测试对拍就失去意义了
    （对拍的前提是"只有一处逻辑，两条执行通道"）。
    """
    guard = 0
    while True:
        guard += 1
        if guard > 64:  # 硬保险：逻辑写错时不要变成死循环
            harness.tracer.error("Graph", "guard", "顺序执行器超过 64 步，已强制终止")
            harness.flush_trace()
            break

        target = next_node(state, harness)
        if target == END:
            break

        fn = NODES.get(target)
        if fn is None:
            harness.tracer.error("Graph", "unknown_node", f"未注册的节点：{target}")
            harness.flush_trace()
            break
        await _exec_node(target, fn)(state, harness)

    return state


# --------------------------------------------------------------------------- 状态
def graph_status() -> dict[str, Any]:
    """图与节点的注册状态，供 /health 与管理后台展示。"""
    from backend.agents.harness.planner import NODE_ACTION, NODE_AGENT
    from backend.agents.harness.router import all_routes
    from backend.app.config import settings

    missing = [n for n in NODE_AGENT if n not in NODES]
    # BATCH__* 是并行批的调度节点（内部跑真实节点），不算"实现了但路由里没声明"。
    orphan = [n for n in NODES if n not in NODE_AGENT and n not in BATCH_NODES]
    return {
        "nodes": list(NODES),
        "entry": "IntentNode",
        "end": END,
        "runtime": "langgraph" if _graph_available() else "sequential",
        "recursion_limit": RECURSION_LIMIT,
        # 读 settings 而不是模块常量：管理后台改的就是 settings，
        # 这里再报一份模块级的旧值会让人以为改配置没生效。
        "max_loop": int(settings.MAX_LOOP),
        "loop_back_nodes": list(LOOP_BACK_NODES),
        "loop_rerun_tail": list(LOOP_RERUN_TAIL),
        "node_actions": {n: NODE_ACTION.get(n, "") for n in NODES},
        # 声明了但没实现的节点（应为空；非空说明路由会指向不存在的节点）
        "missing_nodes": missing,
        # 实现了但路由里没声明的节点（应为空；非空说明这个节点永远不会被执行）
        "orphan_nodes": orphan,
        "routes": all_routes()["intents"],
    }


__all__ = [
    "LOOP_BACK_NODES", "LOOP_RERUN_TAIL", "LOOP_SKIP",
    "EVIDENCE_FIELDS", "NODES", "END",
    "PARALLEL_GROUPS", "BATCH_NODES", "_batch_name",
    "next_node", "build_graph", "run", "graph_status",
    "current_harness",
]
