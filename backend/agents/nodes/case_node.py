"""CaseNode（类案检索）。

方案 4.1 关键约束：**检索 ≤2 次**；案号真实性校验。

类案检索是"锦上添花"的证据通道 —— 方案第十三章的示例明确：
**类案库检索超时，要明确告知律师"本次类案检索不可用，分析意见仅基于法规检索结果"，
并在输出中标注证据缺口**。所以本节点失败时只降级、不中断。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from backend.agents.harness import Harness
from backend.agents.retrieval_policy import effective_domain, enrich_query
from backend.agents.state import GAP_NO_CASES, LegalState, focus_of

logger = logging.getLogger("legal_agent.nodes.case")

AGENT = "case"

# 没指定条数时的默认召回数。与改动前写死的那个 5 一致。
DEFAULT_TOP_K = 5

# 条数上限。与 ``SearchRequest.top_k``（1~50）对齐 —— 两处不一致时，
# 界面能把 50 发进来、节点却按另一个数截断，而两边都不报错。
MAX_TOP_K = 50


async def case_node(state: LegalState, harness: Harness) -> LegalState:
    """类案检索 + 裁判要旨提炼。"""
    harness.step("CaseNode")

    # A2A 委派方（如 AnalysisNode 发现类案池为空）可指定**专项检索式**。
    # 就地读、不弹出 —— 弹出会让第二次委派拿到空检索式并退回 Planner 的宽检索式，
    # 详见 ``agents.state.focus_of``。
    focus = focus_of(state)
    queries = [focus] if focus else harness.queries_for("CaseNode")
    queries = [enrich_query(state, q) for q in queries]
    if not queries:
        harness.gaps("case", "无可用检索式，未执行类案检索",
                     "分析意见仅基于法规检索结果")
        return state

    # 检索条数：由**调用方**指定（类案检索页的「返回条数」面板），留空用默认值。
    # 原先这里在调用点上写死 ``top_k=5``，页面的选择没有任何通路进来 ——
    # 面板里选 3 条还是 20 条，命中数一模一样，且没有任何一处报错。
    top_k = int(state.get("top_k") or 0)
    if top_k <= 0:
        top_k = DEFAULT_TOP_K
    else:
        top_k = min(top_k, MAX_TOP_K)

    hits: list[dict[str, Any]] = []
    # 节点内多条检索式彼此独立（都只是往证据池追加命中），并行执行：
    # 串行实测 case_search 两条 10.7+9.0≈20s，并行后整体耗时收敛到最慢一条；
    # 检索式数量 ≤ 配额（CASE_SEARCH_MAX_CALLS=2），guard_quota 仍按任务计数
    # 兜底拦截超发，结果合并顺序无依赖。
    if harness.invoker.handler.should_skip("case_search"):
        harness.tracer.warn("CaseNode", "skip", "类案检索工具已熔断，停止后续检索")
    else:
        results = await asyncio.gather(
            *(
                harness.tool("case_search", AGENT, query=q, top_k=top_k,
                             cause_of_action=_cause_of_action(state),
                             domain=effective_domain(state))
                for q in queries
            ),
            return_exceptions=True,
        )
        for q, res in zip(queries, results):
            if isinstance(res, Exception):
                harness.gaps("case", f"类案检索工具调用失败（{res}）",
                             "该检索式未完成，类案召回不完整")
                harness.tracer.warn("CaseNode", "failed", f"类案检索失败：{res}")
                continue
            if not res.ok:
                # 工具调用失败与"检索无命中"是**两件不同的事**：前者说明这条检索式根本没跑成，
                # 后者说明跑了但库里没有。措辞必须区分，否则一条真实的工具故障会被
                # "本次没找到类案"这句话盖过去，运维侧永远发现不了。
                harness.gaps("case", f"类案检索工具调用失败（{res.error}）",
                             "该检索式未完成，类案召回不完整")
                harness.tracer.warn("CaseNode", "failed", f"类案检索失败：{res.error}")
                continue
            got = (res.data or {}).get("cases") or []
            if res.degraded and res.fallback_note:
                harness.gaps("case", res.fallback_note, "类案召回完整度下降")
            if not got:
                harness.tracer.info("CaseNode", "no_hit", f"检索式无命中：{q[:60]}")
            hits.extend(got)

    added = harness.merge(state, "cases", [h for h in hits if h.get("case_no") or h.get("case_name")])
    harness.tracer.info("CaseNode", "retrieved", f"新增类案 {added} 个")

    if not state.get("cases"):
        harness.gaps("case", GAP_NO_CASES,
                     "分析意见仅基于法规检索结果，不含裁判倾向参考")
        return state

    await _verify_case_nos(state, harness)
    harness.snapshot()
    return state


async def _verify_case_nos(state: LegalState, harness: Harness) -> None:
    """案号/案例名存在性预校验。

    为什么在这里先做一次（Validator 还会做）：类案库里有相当比例的判例
    **只有案例名、没有案号**（本项目 93 个案例全部无案号）。在下游生成文本之前
    就把"能引用/不能引用"标清楚，可以避免文书里写出一堆无法核验的案号。
    """
    cases = list(state.get("cases") or [])
    unverifiable = 0
    # top-10 存在性核验彼此独立，并行执行：串行 9 次 verify 实测约 2.3s，
    # 并行后整体收敛到最慢一次；核验结果按 case 一一回填，顺序无依赖。
    verify_results = await asyncio.gather(
        *(
            harness.tool("case_no_verify", AGENT,
                         case_no=c.get("case_no") or "",
                         case_name=c.get("case_name") or "")
            for c in cases[:10]
        ),
        return_exceptions=True,
    )
    for c, res in zip(cases[:10], verify_results):
        if isinstance(res, Exception) or not res.ok:
            c["verified"] = False
            unverifiable += 1
            continue
        data = res.data if isinstance(res.data, dict) else {}
        c["verified"] = bool(data.get("exists"))
        if not c["verified"]:
            unverifiable += 1

    if unverifiable:
        harness.gaps(
            "case",
            f"{unverifiable} 个类案未能通过存在性核验（多为无案号的典型案例）",
            "这些判例只可用于理解裁判思路，不得在文书中作为正式引用",
        )
    harness.tracer.info("CaseNode", "verify",
                        f"核验 {min(len(cases), 10)} 个，未通过 {unverifiable} 个")


def _cause_of_action(state: LegalState) -> str:
    """案由：优先用材料解析出的案由，其次从争点里猜。"""
    for p in state.get("parties") or []:
        cause = p.get("cause_of_action")
        if cause:
            return str(cause)
    text = f"{state.get('query') or ''} {'。'.join(str(i.get('title') or '') for i in (state.get('issues') or []))}"
    for kw, cause in (("违法解除", "劳动合同纠纷"), ("劳动关系", "确认劳动关系纠纷"),
                      ("竞业限制", "竞业限制纠纷"), ("工伤", "工伤保险待遇纠纷"),
                      ("加班", "劳动报酬纠纷"), ("租赁", "房屋租赁合同纠纷"),
                      ("买卖", "买卖合同纠纷"), ("著作权", "著作权纠纷"),
                      ("商标", "商标权纠纷"), ("专利", "专利权纠纷")):
        if kw in text:
            return cause
    return ""
