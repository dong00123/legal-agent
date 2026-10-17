"""Harness：Agent Runtime（方案 4.4）。

```
Harness
├── Planner        任务拆解            planner.Planner
├── Router         Agent/Tool 路由     router
├── Tool Executor  MCP / API / Function executor.ToolInvoker
├── Memory         短期 + 长期          memory.HarnessMemory
├── State Manager  当前任务状态         state_manager
├── Validator      引用校验 / 时效性校验 validator.CitationValidator
├── Error Handler  Timeout/Retry/Fallback error_handler.ErrorHandler
├── Audit          全链路审计日志       audit.audit_writer
└── Trace          全链路 Trace        trace.Tracer
```

与 LangGraph 的关系（方案 4.4 原文）：**LangGraph 是具体实现状态图与流程控制的基础框架，
Harness 是更上层的运行时抽象**，统一管理模型、工具、Memory、权限、异常、审计与 Trace。

所以：节点函数只接收 ``(state, harness)``，从 harness 上取它需要的能力，
而不是各自 import 一堆模块。这也让单元测试可以注入假 harness。
"""

from __future__ import annotations

import logging
from typing import Any

from backend.agents.harness.audit import AuditWriter, audit_writer
from backend.agents.harness.context import RunContext, make_context
# 模块级导入是安全的：controller 只依赖 agents.state，不回头依赖本包。
# （它内部对 router / nodes / llm 的引用都是惰性的，正是为了不制造这条环。）
from backend.agents.harness.controller import Controller
from backend.agents.harness.error_handler import ErrorHandler, error_handler
from backend.agents.harness.executor import ToolInvoker
from backend.agents.harness.memory import HarnessMemory
from backend.agents.harness.planner import Planner, TaskPlan, planner
from backend.agents.harness.router import NODE_SPECS, NodeSpec, all_routes, can_call, route_for_intent
from backend.agents.harness.state_manager import (
    add_evidence_gap,
    bump_step,
    compute_confidence,
    finalize,
    has_new_info,
    mark_route,
    merge_list,
    merge_trace,
    record_tool_call,
    snapshot,
    state_digest,
    steps_exhausted,
)
from backend.agents.harness.trace import Tracer
from backend.agents.harness.validator import CitationValidator, ValidationResult
from backend.agents.state import LegalState

logger = logging.getLogger("legal_agent.harness")


class Harness:
    """一次任务运行的运行时。"""

    def __init__(self, ctx: RunContext, state: LegalState,
                 *, error: ErrorHandler | None = None,
                 audit: AuditWriter | None = None) -> None:
        self.ctx = ctx
        self.state = state
        self.tracer: Tracer = ctx.tracer
        self.error = error or ErrorHandler()
        self.audit = audit or audit_writer
        self.planner: Planner = planner

        self.invoker = ToolInvoker(ctx, state, self.error)
        self.memory = HarnessMemory(ctx)
        self._validator: CitationValidator | None = None
        # 最近一次规划结果，供各节点取自己那一步的检索式（见 plan()）
        self.last_plan: TaskPlan | None = None

        # ---- 产品级 Agent 运行时绑定（2026-10-07，见 backend/agents/products/）
        # 4 个产品入口从"共享固定管线的门面"改造为"各自独立的真正 Agent"后，
        # 每次任务绑定**该产品自己的运行时**：
        # - ``product``：本任务所属产品（ProductRuntime：独立节点池/工具池/必需节点/
        #   自主规划与裁决提示词）。``assistant`` 为全功能通用运行时，行为与改造前一致。
        # - ``product_plan``：产品 Agent 自主规划的本次节点序列（LLM 决策 + 规则兜底），
        #   由 orchestrator 在图运行前生成，IntentNode 钉意图分支据此覆盖静态路由；
        #   未启用/规划失败时为 ``None``（用原静态路由，行为与改造前一致）。
        # 刻意挂在 Harness 上而不是 State：产品绑定与规划结果是**执行事实**，
        # 不是证据，不需要随 LangGraph superstep 复制（与 controller/a2a_budget 同理）。
        self.product: Any | None = None
        self.product_plan: list[str] | None = None

        # ---- 图流程控制状态（LangGraph 专用，见 agents/graph.py 模块 docstring）
        # **为什么不放进 State**：LangGraph 每个 superstep 会复制 State，
        # 且条件边函数里对 State 的写入不持久化（实测）。回环计数一旦丢失就是死循环，
        # 所以它必须挂在普通 Python 对象上 —— 对象引用共享，改了一定生效。
        self.reset_runtime()

    # ------------------------------------------------------------------ 重绑定
    def rebind(self, state: LegalState) -> None:
        """把 Harness **及其下游持有 State 的对象**重新绑到当前 State。

        ⚠ 只改 ``self.state`` 是不够的：``ToolInvoker``（写 ``tool_calls`` /
        ``evidence_gap`` / ``error``）与 ``CitationValidator`` 在构造时各自拷了
        一份 State 引用，它们不会跟着 Harness 变。LangGraph 每个 superstep
        都换一个新 dict，于是工具层写进的是上一轮那份过期的副本 ——
        任务跑完后被图的最终 channel 值整行覆盖，表现为：
        ``tool_calls`` 恒为 0、工具降级不进证据缺口、
        越权拒绝写不进 ``state["error"]``（一次越权被记成成功完成的任务）。
        """
        self.state = state
        self.invoker.state = state
        if self._validator is not None:
            self._validator.state = state

    # ------------------------------------------------------------------ 运行时
    def reset_runtime(self) -> None:
        """复位图流程控制状态（每次 run() 开始时调用）。"""
        self.executed: list[str] = []           # 执行游标（回环会撤回到回检点）
        self.node_runs: dict[str, int] = {}     # 各节点累计执行次数（回环不撤回）
        self.loop_count: int = 0                # 已发生的回环次数
        self.loop_snapshot: dict[str, list] = {}  # 回环前的证据快照（增量判断基线）
        self._finalized: bool = False           # 是否已收尾（置信度/快照已落）
        # 跨节点并行批：调度器已按 route 顺序预发过 step 事件的节点集合。
        # 批内节点并发执行时会再次调用 step()，命中该集合则消费式跳过，
        # 避免 steps 重复累加与 SSE step 事件重复发射（见 graph._parallel_batch）。
        self._prestepped: set[str] = set()
        # A2A 自主委派的记账（已发出的委派 / 两档计数 / 链深）。
        # 与上面几项同理挂在对象上而不是 State 里 —— State 每个 superstep 会被复制，
        # 计数一丢，去重和上限就同时失效。惰性导入是为了断开
        # ``harness → delegation → client → nodes → harness`` 这个环。
        from backend.agents.protocol.delegation import DelegationBudget
        self.a2a_budget: DelegationBudget = DelegationBudget()

        # Agent 自主裁决器（2026-10-07，见 controller 模块 docstring）。
        # 与上面几项**同一个理由**挂在对象上而不是 State 里：它的轮数与调用计数
        # 一旦写进 State 就会被 LangGraph 静默丢弃 —— 而丢掉的后果正好是死循环。
        # 每次 begin_task 都新建一个：上一个任务的轮数与调用计数绝不能带进来，
        # 否则第 2 个任务会一开始就"已达轮数上限"，而且不报任何错。
        self.controller: Controller = Controller()

    def begin_task(self) -> None:
        """开始一次任务运行：复位运行态 + 清零本任务的工具配额。

        工具配额按任务隔离（见 :class:`~backend.tools.base.ToolExecutor`）。
        不复位的话，同一进程里第 4 个任务的检索会被前几个任务的计数拦死 ——
        而错误信息看起来像防死循环正常生效，非常难查。
        这里不提供 end_task：配额桶由 ``begin_task`` 幂等清零 + 上限淘汰兜底，
        提前回收会让接口层读不到"本任务用了几次检索"这个诊断信息。
        """
        self.reset_runtime()
        from backend.tools.base import tool_executor

        tool_executor.begin_task(self.ctx.task_id)

    @property
    def finalized(self) -> bool:
        """是否已收尾。图执行器据此决定要不要兜底调 finish()。"""
        return self._finalized

    def loop_state(self) -> dict[str, Any]:
        """回环运行态（供诊断与测试断言）。"""
        return {
            "executed": list(self.executed),
            "loop_count": self.loop_count,
            "loop_snapshot_fields": sorted(self.loop_snapshot),
            "node_runs": dict(self.node_runs),
        }

    # ------------------------------------------------------------------ 便捷
    @classmethod
    def start(cls, state: LegalState, **overrides: Any) -> Harness:
        """从 State 启动 Harness。"""
        ctx = make_context(state, **overrides)
        h = cls(ctx, state)
        h.begin_task()
        h.tracer.info("Harness", "start", f"任务 {ctx.task_id} 已启动")
        return h

    def plan(self, *, intent: str | None = None, **kw: Any) -> TaskPlan:
        """任务拆解。意图未定时先用 state 里的值。结果缓存到 ``last_plan``。

        ``material_text`` 必须一起传：合同审查的检索式由**合同子类型**决定，
        而子类型只能从材料正文里判（``doc_type`` 只说"这是合同"）。
        不传的后果是 Planner 只能拿用户提问去猜 —— 一句"帮我审一下这份合同"
        猜不出是劳动合同还是房屋租赁合同，检索式就退化成无区分度的泛化关键词。
        """
        plan = self.planner.plan(
            intent=intent or self.state.get("intent") or "consult",
            query=self.state.get("query") or "",
            doc_type=self.state.get("doc_type") or "unknown",
            has_material=bool(self.state.get("material_text") or self.state.get("material_paths")),
            case_id=self.state.get("case_id") or "",
            material_text=self.state.get("material_text") or "",
            **kw,
        )
        self.last_plan = plan
        return plan

    def queries_for(self, node: str) -> list[str]:
        """取某个节点该用的检索式（Planner 生成，保证各节点检索式一致可测）。"""
        if self.last_plan is None:
            return [(self.state.get("query") or "").strip()] if self.state.get("query") else []
        return self.last_plan.queries_for(node)

    def quota_used(self, tool: str) -> int:
        """某工具在**本次任务**已用掉的调用次数（进度展示与配额判断用）。

        必须带上 task_id：不带就会读到空桶、恒返回 0，
        于是回环前的"配额是否已用尽"判断永远认为还有余额，
        启动一次注定失败的检索，白耗一轮步数预算。
        """
        from backend.tools.base import tool_executor

        return tool_executor.count(tool, self.ctx.task_id)

    # ------------------------------------------------------------------ 工具
    async def tool(self, name: str, agent: str, **kwargs: Any) -> Any:
        """调用 MCP 工具（带任务内缓存）。"""
        from backend.tools.base import ToolResult

        # 写类工具不缓存（重复执行有副作用）
        from backend.tools.registry import get_tool
        try:
            spec = get_tool(name)
        except KeyError:
            return await self.invoker.call(name, agent, **kwargs)

        if spec.idempotent:
            cached = self.memory.cached_tool(name, kwargs)
            if cached is not None:
                # 缓存命中同样要记一笔调用（cached=True），否则 Trace 看不出真实执行次数
                record_tool_call(self.ctx, self.state, tool=name, agent=agent, args=kwargs,
                                 ok=True, note="cached=True")
                calls = self.state.get("tool_calls") or []
                if calls:
                    calls[-1]["cached"] = True
                return ToolResult(ok=True, tool=name, data=cached, elapsed_ms=0)

        if not can_call(agent, name):
            note = f"Agent {agent} 无权调用工具 {name}"
            self.tracer.error(agent, "route_denied", note)
            self.audit.record("access.denied", trace_id=self.ctx.trace_id,
                              task_id=self.ctx.task_id, resource=name,
                              resource_id=agent, result="denied", detail=note)
            add_evidence_gap(self.state, "tool", note, "该步骤被跳过")
            return ToolResult(ok=False, tool=name, error=note, degraded=True,
                              fallback_note=note)

        result = await self.invoker.call(name, agent, **kwargs)
        if spec.idempotent and result.ok:
            self.memory.cache_tool(name, kwargs, result.data)
        return result

    # ------------------------------------------------------------------ 校验
    async def validate_citations(self) -> ValidationResult:
        """引用终审。"""
        self._validator = CitationValidator(self.ctx, self.state, self.invoker)
        return await self._validator.validate()

    # ------------------------------------------------------------------ 状态
    def step(self, node: str) -> int:
        # 跨节点并行批：调度器已预发过该节点的 step 事件与计数，
        # 节点并发执行时的重复 step 只补记 route 标记，不再累加步数。
        if node in self._prestepped:
            self._prestepped.discard(node)
            mark_route(self.state, node)
            return 0
        mark_route(self.state, node)
        return bump_step(self.state, self.ctx, node)

    def exhausted(self) -> bool:
        return steps_exhausted(self.state)

    def gaps(self, kind: str, reason: str, impact: str = "") -> None:
        add_evidence_gap(self.state, kind, reason, impact)

    def merge(self, state: LegalState, field: str, items: list[dict[str, Any]]) -> int:
        """去重追加证据/要素，返回实际新增条数。

        节点统一走这个方法而不是直接 ``state[field].extend(...)``：
        Validator 回环补充检索时会把**同一批法条**再拿一遍，
        直接 extend 会让 State 里出现重复条目，引用清单也会重复列出同一条法条。
        """
        return merge_list(state, field, items)

    def new_info(self, before: LegalState,
                 fields: tuple[str, ...] = ("statutes", "cases", "knowledges")) -> bool:
        return has_new_info(before, self.state, fields)

    def finish(self) -> LegalState:
        """收尾：置信度、免责声明、Trace 落 State。

        幂等：重复调用只是重算一遍（置信度由 State 决定，没有随机项）。
        """
        finalize(self.state, self.ctx)
        snapshot(self.ctx, self.state, status=_status_of(self.state))
        self.tracer.info("Harness", "finish",
                         f"任务 {self.ctx.task_id} 结束，置信度 {self.state.get('confidence')}")
        self._finalized = True
        return self.state

    def snapshot(self, status: str = "running") -> None:
        self.flush_trace()
        snapshot(self.ctx, self.state, status=status)

    def flush_trace(self) -> None:
        """把 Tracer 的事件**合并**进 ``state["trace"]``（只增不减）。

        为什么需要这个：原先只有 ``finish()`` 会把 Trace 落进 State，
        于是**流程中途**读 ``state["trace"]`` 一律是空的。而 Skill 的步骤判据
        （如合同审查第 5 步"逐条风险扫描是否真的跑过"）正是靠 Trace 事件
        区分"扫了但没发现问题"与"根本没扫"——中途读不到就会把前者误判成后者。
        每个节点收尾时都会调用 snapshot()，所以挂在这里能保证 State 里的 Trace
        始终是最新的，不需要各节点自己同步。

        **必须是合并不是覆盖**。收尾归档（MemoryNode）会用一个新 Harness 跑，
        新 Harness 的 Tracer 里只有它自己那几条事件；覆盖式写入会把整条任务的
        Trace 从几十条冲成几条 —— 而落库、审计追溯、管理后台的耗时分布全都读它。
        这类"跑完才发生的静默数据丢失"比运行中报错难查得多：
        任务状态是成功的，只有 Trace 莫名其妙变短了。

        合并逻辑统一走 ``state_manager.merge_trace``（``finalize`` 也用同一个，
        那里原先也有同样一行赋值，是本项目踩过两次的同一个坑）。
        """
        merge_trace(self.state, self.tracer.to_list())

    # ------------------------------------------------------------------ 诊断
    def diagnostics(self) -> dict[str, Any]:
        return {
            "context": self.ctx.identity(),
            "trace": self.tracer.summary(),
            "memory": self.memory.stats(),
            "errors": self.error.summary(),
            "tools": self.state.get("tool_calls") and len(self.state["tool_calls"]) or 0,
            "state": state_digest(self.state),
            "loop": self.loop_state(),
            # 自主裁决的轮数/调用数/被哪一层闸止住 —— 排障"这次为什么没补证据"要看它
            "controller": self.controller.status(),
        }


def _status_of(state: LegalState) -> str:
    if state.get("error"):
        return "failed"
    if state.get("evidence_gap"):
        return "degraded"
    return "succeeded"


def harness_status() -> dict[str, Any]:
    """Harness 运行态（供 /health 与管理后台）。"""
    from backend.memory import memory as memory_facade
    from backend.tools.registry import registry_status

    return {
        # ⚠ 键名是管理后台在读的响应键，保留原样不动。它的值是**节点声明的条数**，
        # 恒等于 ``graph.NODES`` 的节点数，不是"有多少个 Agent"——
        # 对外的 Agent 只有 ``router.PRODUCT_AGENTS`` 那 4 个。
        "agents": len(NODE_SPECS),
        "routes": {k: v["label"] for k, v in all_routes()["intents"].items()},
        "tools": registry_status()["total"],
        "memory": memory_facade.status(),
        "audit": audit_writer.stats(),
    }


__all__ = [
    "Harness", "RunContext", "make_context",
    "Tracer", "AuditWriter", "audit_writer",
    "ErrorHandler", "error_handler",
    "ToolInvoker", "HarnessMemory",
    "Planner", "TaskPlan", "planner",
    "NodeSpec", "NODE_SPECS", "route_for_intent", "can_call", "all_routes",
    "Controller",
    "CitationValidator", "ValidationResult",
    "merge_list", "add_evidence_gap", "record_tool_call", "bump_step",
    "has_new_info", "compute_confidence", "finalize", "snapshot", "state_digest",
    "mark_route", "steps_exhausted",
    "harness_status",
]
