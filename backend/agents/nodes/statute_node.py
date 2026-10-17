"""StatuteNode（法规检索 + 时效校验）。

方案 4.1 关键约束：**检索 ≤3 次；强制带效力状态**。

两条约束都在这里落地：
- 次数由 ``ToolExecutor.guard_quota`` 强制（每次任务 ``statute_search`` 最多 3 次），
  所以本节点按 Plan 给的检索式顺序调用，用光配额就停 —— 不做"再试一次"的模糊逻辑；
- 效力状态：检索结果缺 ``status`` 时，**必须**补一次
  ``statute_validity_check``。缺状态的法条比没有法条更危险 —— 律师会以为它有效。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from backend.agents.harness import Harness
from backend.agents.retrieval_policy import effective_domain, enrich_query
from backend.agents.state import GAP_NO_STATUTES, LegalState, focus_of
from backend.app.config import settings

logger = logging.getLogger("legal_agent.nodes.statute")

AGENT = "statute"


async def statute_node(state: LegalState, harness: Harness) -> LegalState:
    """法规检索 + 时效性校验。"""
    harness.step("StatuteNode")

    # A2A 委派方（如 AnalysisNode 的时效兜底）可通过 payload 指定**专项检索式**。
    # **就地读、不弹出** —— 弹出式读取会让同一条法条的第二次委派拿到空检索式，
    # 于是它退回 Planner 的宽检索式重跑一遍（贵且与委派意图无关）。
    # 清理由委派层负责，它记了委派前的原值，能原样还回去。
    focus = focus_of(state)
    queries = [focus] if focus else harness.queries_for("StatuteNode")
    queries = [enrich_query(state, q) for q in queries]
    if not queries:
        harness.gaps("statute", "无可用检索式，未执行法规检索",
                     "本次结论缺少法条依据，需律师自行核查")
        return state

    before = {"statutes": list(state.get("statutes") or [])}
    hits: list[dict[str, Any]] = []
    domain = effective_domain(state)

    # 节点内多条检索式彼此独立（都只是往证据池追加命中），并行执行：
    # 串行实测 statute_search 三条 22.4+10.0+7.1≈40s，并行后整体耗时收敛到
    # 最慢一条（约 22s）；检索式数量 ≤ 配额（STATUTE_SEARCH_MAX_CALLS=3），
    # guard_quota 仍按任务计数兜底拦截超发，结果合并顺序无依赖。
    if harness.invoker.handler.should_skip("statute_search"):
        harness.tracer.warn("StatuteNode", "skip", "法规检索工具已熔断，停止后续检索")
    else:
        results = await asyncio.gather(
            *(harness.tool("statute_search", AGENT, query=q, top_k=6,
                           domain=domain) for q in queries),
            return_exceptions=True,
        )
        for q, res in zip(queries, results):
            if isinstance(res, Exception):
                harness.gaps("statute", f"法规检索失败：{res}",
                             "法条依据可能不完整，结论的法律依据需律师自行核查")
                continue
            if not res.ok:
                harness.gaps("statute", f"法规检索失败：{res.error}",
                             "法条依据可能不完整，结论的法律依据需律师自行核查")
                continue
            got = (res.data or {}).get("statutes") or []
            if res.degraded and res.fallback_note:
                harness.tracer.warn("StatuteNode", "degraded", res.fallback_note)
            if not got:
                harness.tracer.info("StatuteNode", "no_hit", f"检索式无命中：{q[:60]}")
            hits.extend(got)

    added = harness.merge(state, "statutes", [h for h in hits if h.get("doc_name")])
    harness.tracer.info("StatuteNode", "retrieved", f"新增法条 {added} 条")

    if not state.get("statutes"):
        harness.gaps("statute", GAP_NO_STATUTES,
                     "本次结论无法律依据支撑，不得作为正式意见使用")
        return state

    await _ensure_status(state, harness)

    if not harness.new_info(before, ("statutes",)) and added == 0:
        harness.tracer.warn("StatuteNode", "no_new_info",
                            "本轮检索未带来新增法条，后续回环将不再重复检索")

    harness.snapshot()
    return state


# --------------------------------------------------------------------------- 工具
async def _ensure_status(state: LegalState, harness: Harness) -> None:
    """确保每条法条都带效力状态。缺状态的批量校验一次。"""
    items = list(state.get("statutes") or [])
    missing = [s for s in items if (s.get("status") or "") in ("", "未知")]
    if not missing:
        verified = sum(1 for s in items if s.get("status") == "现行有效")
        harness.tracer.info("StatuteNode", "status_ok",
                            f"{len(items)} 条法条均已标注效力状态（现行有效 {verified} 条）")
        return

    res = await harness.tool(
        "statute_validity_check", AGENT,
        items=[{"doc_name": s.get("doc_name") or "", "article_no": s.get("article_no") or ""}
               for s in missing],
    )
    if not res.ok:
        harness.gaps("statute", f"效力状态校验失败：{res.error}",
                     "引用前须人工核实法条是否已废止")
        for s in items:
            if not s.get("status"):
                s["status"] = "未知"
        return

    # ToolResult.data 在这里是 dict（statute_validity_check 的返回体），统一成 dict 处理
    data = res.data if isinstance(res.data, dict) else {}
    by_key = {
        f"{(r.get('doc_name') or '').strip('《》')}|{r.get('article_no') or ''}": r.get("status") or "未知"
        for r in (data.get("results") or [])
    }
    filled = 0
    for s in state.get("statutes") or []:
        if (s.get("status") or "") in ("", "未知"):
            key = f"{(s.get('doc_name') or '').strip('《》')}|{s.get('article_no') or ''}"
            s["status"] = by_key.get(key, "未知")
            filled += 1

    unknown = sum(1 for s in state["statutes"] if s.get("status") == "未知")
    harness.tracer.info("StatuteNode", "status_filled",
                        f"补齐 {filled} 条效力状态；仍未确认 {unknown} 条")
    if unknown:
        harness.gaps("statute", f"{unknown} 条法条效力状态未确认（法规库未收录元数据）",
                     "引用这些法条前须人工核实是否现行有效")

    # 已废止的法条不立即删除（Validator 需要看到它才能拦截），但在这里先告警
    repealed = [s for s in state["statutes"] if s.get("status") == "已废止"]
    if repealed:
        harness.gaps("statute",
                     f"检索到 {len(repealed)} 条已废止条文，将在引用终审被拦截",
                     "不得作为裁判依据引用")


def statute_status() -> dict[str, Any]:
    return {"quota": settings.STATUTE_SEARCH_MAX_CALLS}
