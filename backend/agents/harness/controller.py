"""Controller：产品 Agent 的**轮后裁决**（2026-10-07 加）。

## 它解决什么

在此之前，4 个产品入口里只有 AI 助手真的会"思考"（不钉意图 + 让模型复核节点集），
另外 3 个是"钉死意图 → 静态路线 → 顺序跑完"；而全项目**只有 ``AnalysisNode``
一处**在运行时自主委派（``analysis_node._collaborate``）。
所以"4 个 Agent"当时是产品口径，不是执行事实。

这个类补的就是那一步：**一轮跑完之后，agent 看一眼证据够不够，不够就自己调工具补，
并把终审段排回路线重跑。**

## 为什么它不是第 12 个节点

``graph.NODES`` / ``nodes.NODE_FUNCS`` / ``router.NODE_SPECS`` / ``registry.CARDS``
四条表都是 11 条，而且互相断言（``test_a2a.py`` 断言
``set(CARDS) == set(NODE_FUNCS)``；``product_health()`` 查 ``missing_nodes``）。
控制器一旦进了其中任何一条，它就会出现在 ``next_node`` 的调度里、出现在 A2A 能力卡里、
出现在 ``_visited_from_trace`` 的游标里 —— 全是副作用。

所以它挂在 **Harness 上**（与 ``a2a_budget`` / ``error`` 同级），由
``graph._execute`` 调用。它像 ``_should_loop`` 一样活在流程控制层，不是节点。

## 为什么计数不写 State

``graph.py`` 模块 docstring 第 3 条是实测结论：**LangGraph 每个 superstep 会复制
State，且条件边函数里对 State 的写入不持久化**。轮数与调用计数写进 State 会被静默
丢弃 —— 丢掉的后果反过来就是死循环。所以全部计数放在 :class:`ControllerState`
这个普通 Python 对象上（改了一定生效），与 ``harness.loop_count`` 同一个理由。

## 它只做两件事

``call_tool``（自己调检索工具补证据）与 ``finish``（不动）。

**刻意没有 ``run_node``。** 那需要"运行中改 route"，而 route 在本项目里是**计划**、
由 IntentNode 利用 Planner 一次性写定；``graph._rewind`` 的 docstring 写得很清楚——
"计划不该被回环改写"。而补证据这件事 ``call_tool`` + 重跑终审已经覆盖，不必动 route：
终审段本来就在 route 里，**撤回执行游标**就能重新调度（见
``graph._controller_review``）。

## 约束：这是预算内的自由

每一次调用都先过闸，超了就地降级成"带证据缺口交付"，绝不因为"想再试一次"就多跑一轮：

- ``CONTROLLER_MAX_ROUNDS``      —— 一个任务最多思考几轮
- ``CONTROLLER_LLM_MAX_CALLS``   —— 控制器自身的模型调用上限
- ``CONTROLLER_MAX_TOOL_CALLS``  —— 直调工具条数上限（``knowledge_search``
  等 13 个工具**没有配额**，只能靠这一条兜住）
- ``settings.MAX_STEPS`` / 工具配额 / 回环上限 —— 沿用 Harness 原有的语义，一行不改
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from backend.agents.state import LegalState

logger = logging.getLogger("legal_agent.harness.controller")


# --------------------------------------------------------------------------- 候选动作
# 控制器能直调的**全部**工具。只有三个，而且都是纯检索。
#
# 为什么是这三个：它们正是 ``router.can_call`` 里"只读通配"放行的那一组中
# 真正"取证据"的（另外两个 ``statute_validity_check`` / ``case_summarize``
# 是**加工与核验**，不是取证据 —— 核验属于 ValidatorNode 的活，
# 让控制器自己核验等于开了第二条终审通道，直接违反"终审关卡不可绕过"）。
#
# ``pool``  该工具的结果该并进哪个证据池（**必须经 harness.merge 进池**，
#           否则终审盖不住 —— 见 graph._controller_review 的注释）
# ``quota`` 该工具在 settings 里的配额字段名；空串 = 没有配额
# ``node``  该工具对应哪个节点 —— 检索式复用 Planner 为这个节点生成的那一组，
#           保证控制器与节点检索的是同一套式子（否则同一个任务里两种检索式打架）
_TOOLS: dict[str, dict[str, str]] = {
    "statute_search": {
        "pool": "statutes", "label": "法条", "quota": "STATUTE_SEARCH_MAX_CALLS",
        "node": "StatuteNode",
    },
    "case_search": {
        "pool": "cases", "label": "类案", "quota": "CASE_SEARCH_MAX_CALLS",
        "node": "CaseNode",
    },
    "knowledge_search": {
        "pool": "knowledges", "label": "本所办案指引", "quota": "",
        "node": "KnowledgeNode",
    },
}

# 结果里的明细字段名（工具返回 ``{"<pool>": [...]}``）
_LLM_SYSTEM = """你是法律 Agent 的**补证据**裁决器。上一轮的节点已经跑完，
你的唯一任务是判断：现有证据是否足以支撑结论；不足的话，从**候选动作**里选一个来补。

规则：
1. **只能从候选动作里选**，不得新增、不得改写动作名。候选集之外的一律无效。
2. **宁少勿多**：补一次检索要多等几十秒。证据已经够用就选 finish。
3. 不判断案情、不写结论、不回答律师，只决定"要不要再检一次、检什么"。
4. 只输出 JSON：{"action": "<候选动作之一>", "why": "一句话说明缺什么证据"}"""


@dataclass
class ControllerState:
    """控制器的全部可变状态。**一个字段都不写 State**（见模块 docstring）。"""

    rounds: int = 0                  # 已思考轮数
    llm_calls: int = 0               # 控制器自身发起的模型调用次数
    tool_calls: int = 0              # 控制器直调工具的条数
    acted: list[str] = field(default_factory=list)   # 已采取过的动作（去重，防反复检同一个池）
    stopped_by: str = ""             # 哪一层闸止住的（诊断；空 = 没被闸住）
    # 裁决留痕（**全量**，含诊断）。给律师看的那一份见 ``Controller.visible_decisions``：
    # 两者刻意不是一回事 —— 这里面有"模型不可用""裁决超时""无新增"这类排障用语，
    # 摆到律师面前是噪声（2026-09-29 摘掉执行轨迹面板就是同一个理由）。
    decisions: list[dict[str, Any]] = field(default_factory=list)

    def note(self, kind: str, text: str) -> None:
        self.decisions.append({"kind": kind, "text": text})


@dataclass
class Decision:
    """一次裁决的结果。由 :meth:`Controller.review` 返回，交给图去调度。

    ``_rewind`` 刻意**不在这里调用**：撤回是图流程控制的一部分，留在 ``graph.py``
    才有"撤回与调度是同一个判断"这条不变式（见 ``graph.next_node`` 的 docstring）。
    控制器只负责"想"和"调工具"，"把谁排回去"由图决定。
    """

    kind: str                        # "call_tool" | "finish"
    tool: str = ""                   # kind == call_tool 时有效
    why: str = ""                    # 模型给的理由（落 Trace）
    text: str = ""                   # 给律师看的**一句话**关键决策（SSE 的 decision 帧）
    added: int = 0                   # 本次检索**实际新增**的证据条数


class Controller:
    """轮后裁决器。每个任务一个实例（挂在 Harness 上，``reset_runtime`` 时新建）。"""

    def __init__(self) -> None:
        self.state = ControllerState()

    def reset(self) -> None:
        self.state = ControllerState()

    # ---------------------------------------------------------------- 主流程
    async def review(self, state: LegalState, harness: Any) -> Decision | None:
        """一轮跑完后的裁决。返回 ``None`` 表示**完全不介入**（行为与改造前逐字一致）。

        "返回 None" 是绝大多数情况：LLM 不可用、预算用尽、证据已齐、
        意图是直答 —— 每一条都在真正发起模型调用**之前**判掉。
        """
        from backend.app.config import settings

        if not bool(getattr(settings, "CONTROLLER_ENABLED", False)):
            return None

        st = self.state
        cfg = _budget(settings)

        # ---- 闸一：轮数与调用次数（廉价判断，先做，避免白探模型）
        if st.rounds >= cfg["max_rounds"]:
            return self._stop(harness, "rounds", f"已达自主思考轮数上限（{cfg['max_rounds']} 轮）")
        if st.llm_calls >= cfg["max_llm_calls"]:
            return self._stop(harness, "llm_calls",
                              f"已达自主裁决的模型调用上限（{cfg['max_llm_calls']} 次）")
        if st.tool_calls >= cfg["max_tool_calls"]:
            return self._stop(harness, "tool_calls",
                              f"已达直调工具条数上限（{cfg['max_tool_calls']} 次）")

        # ---- 闸二：候选集。**没有可做的事就根本不问模型** ——
        # 这是省一次 chat_json（本地模型 2~8s）最有效的一刀，而且比"问了再判 finish"更确定：
        # 模型没得选的时候也可能硬选一个。
        allow = self._candidates(state, harness, cfg)
        if not allow:
            return None

        # ---- 闸三：直答意图不补证据。off_topic 的路线只有 ReportNode、
        # 不检索不终审（见 INTENT_ROUTES），给它补检索属于无中生有。
        if _is_direct(state):
            return None

        # ---- 闸四：步数预算。补完还要重跑终审段，装不下就别启动
        if harness.exhausted():
            return self._stop(harness, "steps", "步数预算已耗尽，不再自主补证据")

        # ---- 闸五：已知钉在规则引擎上时**一次调用都不发**。
        # 读的是 llm_client 的**当前**状态、不做模块级缓存判断：模型恢复
        # （``LLM_REPROBE_INTERVAL`` 到点后由节点侧的调用触发重探）之后，
        # 下一次裁决就能用上自主能力，不必重启进程。
        # 这一条同时兑现了"降级时行为与改造前一致" —— 降级路径上不新增任何模型流量，
        # 也就不会多出一次 provider 重探的 8 秒等待。
        if _provider_is_rule():
            return None

        return await self._think(state, harness, allow, cfg)

    # ---------------------------------------------------------------- 想
    async def _think(self, state: LegalState, harness: Any,
                     allow: list[str], cfg: dict[str, Any]) -> Decision | None:
        """把观察值与候选集交给模型，解析出一个动作。

        防御纪律逐条照搬 ``planning.refine_with_llm``（那边是同一个问题的成熟解）：
        候选集**事前**裁剪（不是事后靠 ``can_call`` 拦 —— 那样模型会反复试越权工具）
        → ``provider == "rule"`` 不算数 → 任何异常静默回退 → 未知动作由白名单兜住。
        """
        from backend.llm.client import llm_client

        # 产品级提示词：同一套候选动作，但"该补什么证据、为什么补"的口径
        # 由产品自己的裁决器定义（``backend.agents.products.runtime.controller_system``）。
        # 没绑定产品时用通用提示词（行为与改造前一致）。
        system = _LLM_SYSTEM
        product = getattr(harness, "product", None)
        if product is not None:
            from backend.agents.products.runtime import controller_system

            system = controller_system(product)

        st = self.state
        st.rounds += 1
        st.llm_calls += 1

        user = self._observe(state, harness, allow)
        try:
            # ``chat_json`` 没有 per-call timeout（provider 的超时是构造时钉死的），
            # 所以这里自己包一层：控制器是**可选增强**，它卡住绝不能拖住已经跑完的交付物。
            data, result = await asyncio.wait_for(
                llm_client.chat_json(
                    [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
                    task="controller_review", default={},
                ),
                timeout=float(cfg["llm_timeout"]),
            )
        except Exception as exc:  # noqa: BLE001 - 含 asyncio.TimeoutError
            # 超时/异常都按"想不出来"处理，不记录缺口 ——
            # 这不是证据缺失，是可选增强没生效，记成缺口会污染律师看的缺口清单。
            logger.info("自主裁决未走模型（按 finish 处理）：%s", type(exc).__name__)
            st.note("skip", f"裁决未生效（{type(exc).__name__}），按原路线交付")
            return None

        # 规则引擎兜底时 ``chat_json`` 也给得出结果，但那不是"agent 在想"，不能算数。
        # 这一条是"LLM 挂掉时行为与改造前逐字一致"的落点：CI 无模型 → 恒为 rule。
        if getattr(result, "provider", "") == "rule":
            logger.info("自主裁决：模型不可用（rule），不介入")
            st.note("skip", "模型不可用，本次不做自主补证据")
            return None

        raw = str(data.get("action") or "").strip()
        if raw not in allow:
            logger.info("自主裁决给出候选集外的动作 %r，已忽略", raw[:60])
            st.note("skip", "裁决给出候选集外的动作，已忽略")
            return None

        why = str(data.get("why") or "").strip()[:160]
        if raw == "finish":
            st.note("finish", f"证据已够用：{why}" if why else "证据已够用")
            logger.info("自主裁决：finish（%s）", why)
            return Decision(kind="finish", why=why)

        tool = raw.split(":", 1)[1]
        return await self._act(state, harness, tool, why)

    # ---------------------------------------------------------------- 做
    async def _act(self, state: LegalState, harness: Any, tool: str,
                   why: str) -> Decision | None:
        """执行 ``call_tool``：走 ``harness.tool``（唯一安全通道），结果**必须并进证据池**。

        ⚠ **``harness.tool`` 是唯一入口，不得旁路。** 越权检查、配额、熔断、幂等缓存、
        审计留痕、``tool_calls`` 记账六件事全在它里面；绕过去（直接
        ``invoker.call`` / 直接调工具函数）会让这次检索在审计与配额上"不存在" ——
        而它拿到的法条照样会进交付物。这正是用户四条约束里的第一条。
        """
        spec = _TOOLS[tool]
        st = self.state
        args = self._args(state, harness, tool)

        result = await harness.tool(tool, "Controller", **args)
        st.tool_calls += 1

        if not getattr(result, "ok", False):
            note = getattr(result, "error", "") or "工具调用未成功"
            st.note("skip", f"补检{spec['label']}未成功：{note}")
            logger.info("自主补检 %s 未成功：%s", tool, note)
            return None

        items = (getattr(result, "data", None) or {}).get(spec["pool"]) or []
        # **必须经 harness.merge 进池**：ValidatorNode 的终审读的是
        # ``state[pool]``（见 validator._evidence_pool），只把结果放进"给模型看的
        # 观察文本"里就等于绕过了终审 —— 模型一旦在交付物里写它，第一轮
        # ``_collect_mentions`` 就会以"引用不在证据池内"拦下，白跑一轮。
        added = harness.merge(state, spec["pool"], list(items)) if items else 0

        label = spec["label"]
        if added:
            text = f"补检{label}：新增 {added} 条（{why}）" if why else f"补检{label}：新增 {added} 条"
            st.acted.append(tool)
            st.note("call_tool", text)
            # ⚠ 这一条与 ``visible_decisions()`` 是**同一个集合**：
            # 它是 SSE 的 ``decision`` 帧（chat.frame_of 按 node/event 分流），
            # 那个是 ``/doc/compose`` 随 body 回的一份。改这里就要改那里。
            harness.tracer.info("Controller", "decision", text)
            logger.info("自主补检 %s：新增 %d 条（%s）", tool, added, why)
            return Decision(kind="call_tool", tool=tool, why=why, text=text, added=added)

        # 没新增：**不要重跑终审段**。白跑两三个节点、耗掉步数，结论一个字不会变 ——
        # 这与 ``_should_loop`` 的第三层（"没有新证据就不要白转一圈"）是同一条纪律。
        text = f"补检{label}无新增，不再重跑终审"
        st.note("skip", text)
        harness.tracer.info("Controller", "no_gain", text)
        return Decision(kind="call_tool", tool=tool, why=why, text=text, added=0)

    # ---------------------------------------------------------------- 观察
    def _candidates(self, state: LegalState, harness: Any,
                    cfg: dict[str, Any]) -> list[str]:
        """候选动作集。**事前**裁剪，模型只能在这个集合里选。

        只把"池子为空、且配额还够"的工具放进来：
        - 池子非空 ⇒ 节点已经检过了，再检一次大概率是同一批结果；
        - 配额用尽 ⇒ 放进去也是白试，模型还会反复挑它（事后拦截的典型坏处）。

        产品工具池裁剪（2026-10-07）：绑定产品 Agent 后，**只放该产品自己的
        工具集里的候选** —— 类案检索 Agent 的裁决器不会去补"本所办案指引"，
        文书套用 Agent 的裁决器也不会去补"法条/类案"（它的工具池只有
        ``knowledge_search``）。没绑定产品时放全部候选（行为与改造前一致）。
        """
        allow: list[str] = []
        product = getattr(harness, "product", None)
        tool_pool: tuple[str, ...] = ()
        if product is not None:
            tool_pool = tuple(getattr(product, "tool_pool", ()) or ())
        for tool, spec in _TOOLS.items():
            if tool_pool and tool not in tool_pool:
                continue
            if state.get(spec["pool"]):
                continue
            if tool in self.state.acted:
                continue          # 已经补过、没补到东西，不再重复
            quota = spec["quota"]
            if quota:
                cap = int(getattr(_settings(), quota, 0) or 0)
                if cap and harness.quota_used(tool) >= cap:
                    continue
            allow.append(f"call_tool:{tool}")
        if allow:
            allow.append("finish")
        return allow

    def _observe(self, state: LegalState, harness: Any, allow: list[str]) -> str:
        """把"现在的证据状态 + 候选集"渲染成模型可读的一段话。

        刻意只给**计数与状态**，不给证据正文：控制器要判断的是"缺不缺"，
        不是"内容是什么"；喂正文既撑大 prompt（每次多几千 token），
        又会让它忍不住去分析案情（那是 AnalysisNode 的活）。
        """
        pools = "、".join(
            f"{s['label']} {len(state.get(s['pool']) or [])} 条" for s in _TOOLS.values())
        gaps = state.get("evidence_gap") or []
        lines = [
            f"律师提问：{str(state.get('query') or '')[:400]}",
            f"本次意图：{state.get('intent') or '未知'}",
            f"已执行节点：{'、'.join(_executed(state, harness)) or '无'}",
            f"现有证据：{pools}",
            f"引用终审：{state.get('citation_check') or 'pending'}",
            f"已有证据缺口：{len(gaps)} 条",
            "",
            "候选动作（只能选一个）：",
        ]
        for item in allow:
            if item == "finish":
                lines.append("- finish —— 证据够用，直接交付")
                continue
            tool = item.split(":", 1)[1]
            spec = _TOOLS[tool]
            quota = spec["quota"]
            if quota:
                cap = int(getattr(_settings(), quota, 0) or 0)
                left = max(0, cap - harness.quota_used(tool))
                left_note = f"配额还剩 {left} 次"
            else:
                left_note = "无配额限制"
            lines.append(f"- {item} —— {spec['label']}池为空（{left_note}）")
        return "\n".join(lines)

    def _args(self, state: LegalState, harness: Any, tool: str) -> dict[str, Any]:
        """工具参数。检索式复用 Planner 为本节点生成的那一组。

        ``case_search`` 的 ``top_k`` **强制取 ``state["top_k"]``**：
        类案检索页的「返回条数」是律师选的，控制器直调时若用工具默认值 5、
        而律师选的是 3，返回就超出他要求的上限 —— 接口层的
        ``len(done["cases"]) <= top_k`` 断言会直接挂。
        """
        spec = _TOOLS[tool]
        queries = []
        try:
            queries = list(harness.queries_for(spec["node"]) or [])
        except Exception:  # noqa: BLE001 - 取不到检索式就退回原始提问
            queries = []
        query = (queries[0] if queries else "") or str(state.get("query") or "")
        args: dict[str, Any] = {"query": query.strip()}

        from backend.agents.nodes.case_node import DEFAULT_TOP_K, MAX_TOP_K

        if tool == "case_search":
            top_k = int(state.get("top_k") or 0) or DEFAULT_TOP_K
            args["top_k"] = max(1, min(top_k, MAX_TOP_K))
        return args

    # ---------------------------------------------------------------- 对外
    def visible_decisions(self) -> list[dict[str, Any]]:
        """**给律师看的**关键决策（约束 4 的"过程中"层）。

        与 ``state.decisions`` 的区别是刻意的，判据只有一条：
        **这一步不做，交付物会不一样吗**。``call_tool`` 且真的补到了东西 ——
        是的，它会多出证据、还要重跑终审；其余（模型不可用、裁决超时、
        补检无新增、证据已够用）都是"想过但没改变任何东西"，它们留在
        ``state.decisions`` 与 Trace 里给排障用，不摆到律师面前。

        **必须与 Trace 里的 ``decision`` 事件是同一个集合** —— 那两个是同一句
        "关键决策"的两条投递路径：SSE 走 Trace 推帧，``/doc/compose`` 不是 SSE，
        只能随最终 body 一起回（见 ``orchestrator.RunOutcome.decisions``）。
        改了一处不改另一处，表现是**某一个入口比别的入口多显示一句/少显示一句**，
        两边都不报错。``tests/test_controller.py`` 里有一条用例把这个等号钉死。
        """
        return [dict(d) for d in self.state.decisions if d.get("kind") == "call_tool"]

    # ---------------------------------------------------------------- 诊断
    def _stop(self, harness: Any, layer: str, reason: str) -> None:
        """记下"是哪一层闸止住的"，然后返回 None（不介入）。

        必须留痕：否则"这次为什么没自主补证据"在 Trace 里完全无解 ——
        而它和"补了但没补到"是两种完全不同的处置方向。

        ⚠ 这一条走 **Trace**（``Controller/stop``）而**不**进
        ``state.decisions``：两者的读者不同 —— decisions 记的是"模型说了什么"
        （裁决过程），这一条是"预算闸在哪儿合的"（调度事实）。而且闸一比闸二先判，
        每次都发生在**调用模型之前**：混进 decisions 会把"闸住的次数"
        记成"想过的次数"。事件名刻意**不叫** ``decision``，``frame_of``
        就只会把它当 trace 推给已订阅的人，不会在律师界面上多出一行。
        """
        self.state.stopped_by = layer
        logger.info("自主裁决停止：%s", reason)
        try:
            harness.tracer.info("Controller", "stop", reason)
        except Exception as exc:  # noqa: BLE001 - 留痕失败不该改变"不介入"这个结果
            logger.debug("停止留痕失败（不影响流程）：%s", exc)
        return None

    def status(self) -> dict[str, Any]:
        st = self.state
        return {
            "rounds": st.rounds,
            "llm_calls": st.llm_calls,
            "tool_calls": st.tool_calls,
            "acted": list(st.acted),
            "stopped_by": st.stopped_by,
            "decisions": list(st.decisions),
        }


# --------------------------------------------------------------------------- 小工具
def _settings() -> Any:
    from backend.app.config import settings

    return settings


def _budget(settings: Any) -> dict[str, Any]:
    return {
        "max_rounds": int(getattr(settings, "CONTROLLER_MAX_ROUNDS", 2) or 0),
        "max_llm_calls": int(getattr(settings, "CONTROLLER_LLM_MAX_CALLS", 4) or 0),
        "max_tool_calls": int(getattr(settings, "CONTROLLER_MAX_TOOL_CALLS", 3) or 0),
        "llm_timeout": float(getattr(settings, "CONTROLLER_LLM_TIMEOUT", 12.0) or 12.0),
    }


def _provider_is_rule() -> bool:
    """当前是否**已确定**钉在规则引擎兜底上。

    只认"已选定且就是 rule"：``status()`` 在还没探测过时给的是"未探测"，
    那种情况要放行 —— 否则"模型本来可用但还没人探测过"会把自主能力永久挡在门外。
    """
    try:
        from backend.llm.client import llm_client

        return llm_client.status().get("provider") == "rule"
    except Exception:  # noqa: BLE001 - 判断不了就放行，交给下游的 provider 检查兜底
        return False


def _is_direct(state: LegalState) -> bool:
    """是不是"连检索都不做"的直答意图（与法律无关）。"""
    from backend.agents.harness.router import is_direct

    return is_direct(str(state.get("intent") or ""))


def _executed(state: LegalState, harness: Any) -> list[str]:
    """已执行节点（观察用）。与 ``graph._executed`` 同源，优先取 Harness 上的游标。"""
    if harness is not None and getattr(harness, "executed", None):
        return list(dict.fromkeys(harness.executed))
    return []


__all__ = ["Controller", "ControllerState", "Decision"]
