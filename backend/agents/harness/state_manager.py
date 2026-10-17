"""Harness State Manager：任务状态管理。

职责：
1. **累加而非覆盖**：检索证据、证据缺口、Trace 按语义去重后追加，
   避免 Validator 回环补充检索时把已有证据冲掉；
2. **步数控制**：实现方案 4.3 防死循环第一层（``max_steps``）与第三层
   （新 Observation 无新增有效信息即停）；
3. **持久化**：把 State 快照写入 ``la_task``，支撑「双端任务互通」与任务回放。

关于「无新增有效信息」的判定：不能只比列表长度，因为回环检索很容易
拿回**同一批法条**（只是排序变了）。所以比对的是**去重后的语义键集合**，
键的构造见 :data:`DEDUP_KEYS`。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Callable

from backend.agents.harness.context import RunContext
from backend.agents.state import LegalState
from backend.app.config import settings

logger = logging.getLogger("legal_agent.harness.state")

# 去重语义键：同一个法条/判例/知识片段只保留一份
DEDUP_KEYS: dict[str, Callable[[dict[str, Any]], str]] = {
    "statutes": lambda d: f"{d.get('doc_name', '')}|{d.get('article_no', '')}",
    "cases": lambda d: (d.get("case_no") or "") or (d.get("case_name") or "") or (d.get("chunk_id") or ""),
    "knowledges": lambda d: f"{d.get('title', '')}|{d.get('chunk_id', '')}",
    "facts": lambda d: (d.get("text") or "")[:120],
    "issues": lambda d: (d.get("title") or "")[:120],
    "risk_points": lambda d: f"{d.get('clause_no', '')}|{(d.get('risk') or '')[:60]}",
    "missing_clauses": lambda d: d.get("name", ""),
    "citations": lambda d: f"{d.get('type', '')}|{d.get('name', '')}|{d.get('article', '')}"
                            f"|{d.get('case_no', '')}",
}


# --------------------------------------------------------------------------- 累加
def merge_list(state: LegalState, field: str, items: list[dict[str, Any]]) -> int:
    """去重追加，返回**实际新增**条数。"""
    if not items:
        return 0
    key_fn = DEDUP_KEYS.get(field)
    existing = list(state.get(field) or [])
    seen: set[str] = set()
    if key_fn:
        seen = {key_fn(d) for d in existing if isinstance(d, dict)}

    added = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        if key_fn:
            k = key_fn(it)
            if k in seen:
                continue
            seen.add(k)
        existing.append(it)
        added += 1

    state[field] = existing  # type: ignore[literal-required]
    return added


def _gap_key(reason: str) -> str:
    """缺口的去重键：只看**理由**，不看 kind。

    理由才是"事实陈述"，kind 只说明"这条是哪个 Agent 先发现的"。原先按
    ``kind|reason`` 去重，于是同一条事实（如"类案不可用、分析仅基于法规结果"）
    由 CaseNode（kind=case）和 AnalysisNode（kind=analysis）各登记一次时，
    两条谁也去不掉谁，在报告里并排重复出现。

    去重后保留**首次登记**的 kind —— 那是"谁先发现的"，比后到的更有信息量。
    """
    text = (reason or "").strip()
    return re.sub(r"[\s　。．.；;，,、]", "", text)


def add_evidence_gap(state: LegalState, kind: str, reason: str, impact: str = "") -> None:
    """登记证据缺口（按理由去重，见 :func:`_gap_key`）。"""
    key = _gap_key(reason)
    if not key:
        return
    gaps = list(state.get("evidence_gap") or [])
    if any(_gap_key(g.get("reason") or "") == key for g in gaps):
        return
    gaps.append({"kind": kind, "reason": reason, "impact": impact, "degraded": True})
    state["evidence_gap"] = gaps


def has_new_info(before: LegalState, after: LegalState,
                 fields: tuple[str, ...] = ("statutes", "cases", "knowledges")) -> bool:
    """防死循环第三层：新 Observation 是否带来新增有效信息。

    只比"长度"是不够的 —— Validator 回环时很容易拿回同一批法条（顺序不同），
    那样长度没变但也算"无新增"。所以比对去重后的语义键集合。
    """
    for f in fields:
        key_fn = DEDUP_KEYS.get(f)
        if key_fn is None:
            if len(after.get(f) or []) > len(before.get(f) or []):
                return True
            continue
        kb = {key_fn(d) for d in (before.get(f) or []) if isinstance(d, dict)}
        ka = {key_fn(d) for d in (after.get(f) or []) if isinstance(d, dict)}
        if ka - kb:
            return True
    return False


# --------------------------------------------------------------------------- 步数
def planned_steps(state: LegalState) -> int:
    """这次流程**计划**走几步：计划路线 + IntentNode。

    两个坑（都是真机 SSE 抓出来的）：
    1. ``route`` 在 IntentNode 执行时只是入口占位 ``["IntentNode"]``，计划还没排出来 ——
       这时如果照算会得到「第 1/2 步」这种莫名其妙的分母，所以此时返回 0（不显示分母）。
    2. 排完计划后 ``route`` 是**不含 IntentNode** 的执行节点列表，所以分母要 +1；
       但如果哪天 route 里真的带了 IntentNode，就不能再加了（否则多算一步）。
    """
    route = [n for n in (state.get("route") or []) if isinstance(n, str)]
    others = [n for n in route if n != "IntentNode"]
    if not others:
        return 0
    return len(others) + 1


def bump_step(state: LegalState, ctx: RunContext, node: str) -> int:
    """步数 +1 并记一笔 Trace。

    **分母用计划步数，不用 MAX_STEPS**（2026-09-21 改）。
    原先写的是 ``第 {steps}/{MAX_STEPS} 步``，于是界面上显示「ReviewNode · 第 6/16 步」——
    律师看到会以为后面还有 10 步没跑、进度条是不是永远走不完。
    实际情况是：合同审查的计划路径是 **10 步**（IntentNode + route 里 9 个节点），
    16 是**失控防护的预算上限**（最长路径 + 一次完整回环 + 余量），两者根本不是一回事。

    **这里刻意不登记"已达最大步数"的证据缺口**。原先是在这里登记的，后果是：
    只要步数碰到上限就写一条缺口，哪怕当时流程正好跑完、什么都没被截断。
    表现就是每份报告的证据缺口清单里都挂着一条"已达最大步数限制（8 步），
    流程提前收敛"，而实际上完全没有收敛 —— 律师看两次就会整份清单都不信了。
    真正的截断点只有一个：``graph.next_node`` 发现还有待执行节点但预算已耗尽。
    缺口必须登记在那里，因为它才是"真的少跑了东西"的地方。
    """
    steps = int(state.get("steps") or 0) + 1
    state["steps"] = steps
    total = planned_steps(state)
    if not total:
        detail = f"第 {steps} 步"
    elif steps <= total:
        detail = f"第 {steps}/{total} 步"
    else:
        # 回环重跑会让实际步数超过计划数。别写成「第 11/10 步」也别把分母改成 11
        # （那会看着像"每步都是最后一步"），直接把话说明白。
        detail = f"第 {steps} 步（计划 {total} 步，本次含回环重跑）"
    ctx.tracer.info(node, "step", detail)
    return steps


def steps_exhausted(state: LegalState) -> bool:
    return int(state.get("steps") or 0) >= settings.MAX_STEPS


def mark_route(state: LegalState, node: str) -> None:
    """把节点登记进 path（若不在计划里才补登）。

    ``route`` 是**计划**（IntentNode 依 Planner 写入），不是执行记录。
    原先这里是"执行过的节点一律追加"，于是回环一轮之后 route 里会出现
    ``[IntentNode, IntakeNode, ..., IntentNode, IntakeNode, ...]`` 这样的重复串：
    前端展示的"本次打算做什么"与实际路径对不上，律师看到的就是一团乱账。
    这里只在节点不在计划里时补登，覆盖"测试里直接调单个节点"的场景；
    真实的执行轨迹由 Trace 的 step 事件与 ``Harness.executed`` 承载
    （见 ``agents/graph.py`` 的 ``_executed``）。
    """
    route = list(state.get("route") or [])
    if node in route:
        return
    route.append(node)
    state["route"] = route


# --------------------------------------------------------------------------- 工具记录
def record_tool_call(ctx: RunContext, state: LegalState, *, tool: str, agent: str,
                     args: dict[str, Any], ok: bool, elapsed_ms: int = 0,
                     retries: int = 0, error: str = "", degraded: bool = False,
                     note: str = "") -> None:
    """把一次工具调用写进 State.tool_calls 与 Trace。"""
    summary = _summarize_args(args)
    calls = list(state.get("tool_calls") or [])
    calls.append({
        "step": int(state.get("steps") or 0),
        "tool": tool,
        "agent": agent,
        "args_summary": summary,
        "ok": ok,
        "elapsed_ms": elapsed_ms,
        "retries": retries,
        "error": error,
        "cached": False,
        "degraded": degraded,
        "started_at": datetime.now().isoformat(timespec="seconds"),
    })
    state["tool_calls"] = calls

    level = "info" if ok and not degraded else ("warning" if degraded else "error")
    ctx.tracer.add(agent, f"tool:{tool}",
                   f"{summary} → ok={ok} degraded={degraded} {note}".strip(),
                   level, elapsed_ms)


def _summarize_args(args: dict[str, Any]) -> str:
    """参数摘要（只记关键信息，避免把整篇合同塞进 Trace）。"""
    parts: list[str] = []
    for k, v in (args or {}).items():
        if isinstance(v, str):
            parts.append(f"{k}={v[:80]}")
        elif isinstance(v, list):
            parts.append(f"{k}[{len(v)}]")
        else:
            parts.append(f"{k}={v}")
    return " ".join(parts)[:300]


# --------------------------------------------------------------------------- 置信度
def compute_confidence(state: LegalState) -> float:
    """置信度打分。

    完全确定性（同样的 State 一定得到同样的分数），便于回归测试与阈值调参。
    权重设计意图：
    - 有法条依据是硬基础（0.35）；
    - 类案支撑是加分项（0.20），缺失只降不判死；
    - 引用全部校验通过给满分权重（0.25），有拦截直接砍半；
    - 证据缺口与技术降级各扣分；
    - 法律咨询/法规检索类任务没有类案是正常的，因此类案项按意图调整分母。
    """
    score = 0.0
    has_statute = bool(state.get("statutes"))
    has_case = bool(state.get("cases"))
    intent = state.get("intent") or ""

    score += 0.35 if has_statute else 0.0

    # 类案：只有"类案检索 / 案件分析"这类任务才把类案当必需项
    case_required = intent in ("case_search", "case_analysis")
    if has_case:
        score += 0.20
    elif not case_required:
        score += 0.20  # 非必需场景不惩罚

    check = state.get("citation_check") or "pending"
    citations = state.get("citations") or []
    if citations:
        verified = sum(1 for c in citations if c.get("verified"))
        ratio = verified / len(citations)
        score += 0.25 * (ratio if check != "failed" else ratio * 0.5)
    elif check == "passed":
        score += 0.25
    else:
        # 无引用且未校验：不白送这 0.25
        score += 0.0

    # 内容完整性。**report 必须算在内**：咨询与快速问答的交付物就是报告
    # （`DraftNode` 只在要文书时才跑），漏掉它等于说"这一类任务永远没有产出"。
    # 实测后果不是分数偏低一点，而是**天花板压在门槛之下**：
    # 无法条 +0.35、类案 +0.20 就是全部可得，扣掉证据缺口后 0.50，
    # 而缓存准入是 0.60 —— 于是一个检索到 14 条法条、9 篇类案、
    # 终审也通过了的结论，仍然一辈子进不了缓存。
    if (state.get("draft") or state.get("report")
            or state.get("risk_points") or state.get("strategy")):
        score += 0.10
    if state.get("facts"):
        score += 0.05
    if state.get("issues"):
        score += 0.05

    # 扣分项。**排除"置信度低于阈值"这条缺口本身**：它由 finalize 在算出低
    # 置信度后登记，若参与扣分就形成正反馈——缺口→置信度更低→再登记一条
    # 数值不同的缺口（真机上滚出 0.15/0.10/0.05 三条递减缺口，去重按理由
    # 生效不了，因为每条数值都不同）。
    gaps = [g for g in (state.get("evidence_gap") or [])
            if not str(g.get("reason") or "").startswith("置信度")]
    score -= min(0.20, 0.05 * len(gaps))
    if state.get("error"):
        score -= 0.10

    return round(max(0.0, min(1.0, score)), 2)


# --------------------------------------------------------------------------- 持久化
def snapshot(ctx: RunContext, state: LegalState, *, status: str = "running",
             task_service: Any = None) -> None:
    """把 State 快照写入任务记录（失败不影响主流程）。

    ``trace`` 取 ``state["trace"]``（已合并的全量），**不是** ``ctx.tracer.to_list()``。
    这是本项目踩过的第三次"同一行写法的坑"：收尾归档用新 Harness 跑，
    它的 Tracer 里只有自己那 4 条事件，落库后整条任务的 Trace 就从 63 条变成 4 条。
    改用 State 里那份合并结果后，无论谁来落快照，写进去的都是全量且单调不减的。
    """
    try:
        if task_service is None:
            from backend.app.services.task_service import task_service as _ts
            task_service = _ts
        task_service.save_snapshot(
            task_id=state.get("task_id") or ctx.task_id,
            status=status,
            state=dict(state),
            trace=list(state.get("trace") or []),
            confidence=float(state.get("confidence") or 0.0),
            citation_check=state.get("citation_check") or "pending",
            error=state.get("error") or "",
        )
    except Exception as exc:  # noqa: BLE001 - 快照失败不能中断办案流程
        logger.warning("任务快照写入失败（不影响主流程）：%s", exc)


def _trace_key(ev: dict[str, Any]) -> tuple[str, str, str, str]:
    """Trace 事件的去重键（节点 + 事件 + 详情 + 时间戳）。

    不用对象身份也不自增序号：合并发生在两个不同的 Tracer 之间，
    它们各自从 0 开始编号，靠序号对齐必然错位。
    """
    return (
        str(ev.get("node") or ""),
        str(ev.get("event") or ""),
        str(ev.get("detail") or ""),
        str(ev.get("ts") or ""),
    )


def merge_trace(state: LegalState, events: list[dict[str, Any]]) -> int:
    """把 Trace 事件**合并**进 ``state["trace"]``（只增不减），返回新增条数。

    **必须是合并，不能是覆盖** —— 这是本项目踩过两次的坑，两处写法不同、
    症状却一样（任务状态成功、Trace 却莫名变短）：

    1. ``Harness.flush_trace`` 早期写成 ``state["trace"] = tracer.to_list()``：
       收尾归档用新 Harness（新 Tracer 只有自己那几条）跑一次，
       整条任务的 63 条 Trace 被冲成 4 条；
    2. ``finalize`` 里同样一行赋值，在 ``finish()`` 时又冲一次。

    后果比运行中报错难查得多：**落库的 trace、审计追溯、管理后台的
    各 Agent 耗时分布全都读它**，而任务本身状态正常、报告也完整。
    统一走本函数之后，这类覆盖不会再出现。
    """
    merged = list(state.get("trace") or [])
    seen = {_trace_key(e) for e in merged}
    added = 0
    for ev in events or []:
        key = _trace_key(ev)
        if key in seen:
            continue
        seen.add(key)
        merged.append(ev)
        added += 1
    if added:
        state["trace"] = merged
    return added


def finalize(state: LegalState, ctx: RunContext) -> LegalState:
    """收尾：算置信度、补免责声明、合并 notes。

    **命中缓存时不重算置信度**：缓存路径下本次没有检索、没有证据进 State，
    而 compute_confidence 的三块权重（法条/类案/引用）是按本次 State 算的 ——
    照算会把原任务 0.86 的结论硬生生压成 0.15，还会顺带登记一条
    "置信度低于阈值，需律师复核"的假缺口。结论是照搬来的，置信度就该照搬。
    """
    if state.get("cache_hit"):
        merge_trace(state, ctx.tracer.to_list())
        state["disclaimer"] = settings.DISCLAIMER
        state["notes"] = list(dict.fromkeys([*(state.get("notes") or []), *ctx.notes]))
        return state

    state["confidence"] = compute_confidence(state)
    state["disclaimer"] = settings.DISCLAIMER
    # 合并而不是赋值：见 merge_trace 的说明
    merge_trace(state, ctx.tracer.to_list())
    state["notes"] = list(dict.fromkeys([*(state.get("notes") or []), *ctx.notes]))

    # 直答（与法律无关的寒暄等）没有证据链可评：置信度模型的三块权重
    # （法条/类案/引用）全为空，分数天然低。给这种任务登记"需律师复核
    # 关键判断"纯属噪音——答案文本本身已经如实说明了情况。
    if (state["confidence"] < settings.CITATION_MIN_CONFIDENCE
            and (state.get("intent") or "") != "off_topic"):
        ctx.note(
            f"本次结论置信度 {state['confidence']:.2f} 低于阈值 "
            f"{settings.CITATION_MIN_CONFIDENCE}，建议补充材料后重新分析"
        )
        add_evidence_gap(
            state, "tool",
            f"置信度 {state['confidence']:.2f} 低于阈值 {settings.CITATION_MIN_CONFIDENCE}",
            "结论可靠性下降，需律师复核关键判断",
        )
    return state


def state_digest(state: LegalState) -> dict[str, Any]:
    """State 摘要（写日志与接口返回用，避免返回全文）。"""
    from backend.agents.state import state_summary

    d = state_summary(state)
    d["evidence_gaps"] = [g.get("reason", "") for g in (state.get("evidence_gap") or [])][:10]
    d["tool_calls"] = len(state.get("tool_calls") or [])
    d["elapsed_nodes"] = [e.get("node") for e in (state.get("trace") or []) if e.get("elapsed_ms")]
    return d
