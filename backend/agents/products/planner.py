"""产品级自主规划：让产品 Agent 自己决定本次要跑哪些节点。

## 它解决什么

改造前，钉了意图的产品入口（类案检索 / 合同审查 / 文书中心）直接走
``INTENT_ROUTES`` 的**固定静态路由** —— 同一种产品永远跑同一条节点序列，
产品入口本身没有"想"的能力。

改造后，每次任务启动时，该产品的 Agent 在自己的节点池里**自主规划**：
用 LLM 按本次提问与材料决定跑哪些节点（规则先给基线，模型在池内复核裁剪，
必需节点不可删，模型不可用/失败一律回退规则基线 —— 与
``planning.refine_with_llm`` 同一套防御纪律）。

## 与动态规划的关系

``planning.py`` 的动态规划服务的是"自然语言提问 → 意图分类后按问题裁剪"，
**只对未钉意图的入口生效**（AI 助手）。钉了意图的产品入口原来**刻意跳过**
动态规划（意图是页面点名的，不能按问题裁掉核心节点，见 intent_node 的注释）。

产品级规划是**钉意图产品的自主能力**：它同样按问题裁剪/增补，但边界是
该产品的节点池、必需节点是该产品的必需节点 —— 类案检索页永远有 CaseNode，
合同审查页永远有 Intake + Review，文书中心永远不需要 ReportNode。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from backend.agents.products.runtime import NODE_ORDER, ProductRuntime, planner_system
from backend.app.config import settings

logger = logging.getLogger("legal_agent.products.planner")

_LLM_USER = (
    "律师提问：{query}\n"
    "是否提供材料：{has_material}\n"
    "文档类型：{doc_type}\n"
    "规则先给出的基线方案：{base}\n"
    "（基线是规则兜底，你可以按本次问题在节点池内增删，但不能删必需节点）"
)


async def product_plan(runtime: ProductRuntime, *, query: str,
                       has_material: bool = False, doc_type: str = "",
                       material_text: str = "") -> list[str] | None:
    """产品 Agent 自主规划本次节点序列。

    返回：
    - ``list[str]`` —— 规划出的节点序列（已 sanitize，可直接作为 route override）；
    - ``None`` —— 未启用 / 模型不可用 / 结果非法（调用方用规则基线路由，
      行为与改造前一致）。

    **任何失败都回退规则基线**：产品级规划是增强，它不该让整条链路跑不起来，
    也不该让律师等一个规划调用超时（与 ``planning.refine_with_llm`` 同一纪律）。
    """
    if not bool(getattr(settings, "PRODUCT_PLAN_ENABLED", False)):
        return None
    if not runtime.intent:
        return None  # AI 助手不钉意图，不触发产品级规划（走原有动态规划）

    base = list(runtime.base_route)
    allow = sorted(set(runtime.node_pool), key=_rank)
    system = planner_system(runtime)
    user = _LLM_USER.format(
        query=(query or "")[:600],
        has_material="是" if has_material else "否",
        doc_type=doc_type or "未知",
        base=" → ".join(base) if base else "（空）",
    )

    try:
        from backend.llm.client import llm_client

        data, result = await asyncio.wait_for(
            llm_client.chat_json(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                task="product_plan", default={},
            ),
            timeout=float(getattr(settings, "PRODUCT_PLAN_LLM_TIMEOUT", 12.0) or 12.0),
        )
    except Exception as exc:  # noqa: BLE001 - 含超时
        logger.info("产品规划未走模型（按规则基线）：%s", type(exc).__name__)
        return None

    # 规则引擎兜底时不算是"agent 在想"（与 planning.refine_with_llm 同一判据）
    if getattr(result, "provider", "") == "rule":
        logger.info("产品规划：模型不可用，沿用规则基线")
        return None

    picked = data.get("nodes")
    if not isinstance(picked, list) or not picked:
        return None

    cleaned = _sanitize([str(n) for n in picked], runtime, base)
    if not cleaned or cleaned == base:
        # 裁空 / 与规则基线一致：不产生新路线，调用方走原静态路由（行为与改造前一致）
        return None
    logger.info("产品规划（%s）：%s → %s（%s）",
                runtime.name, " → ".join(base) or "（空）", " → ".join(cleaned),
                str(data.get("why") or "")[:80])
    return cleaned


def _sanitize(picked: list[str], runtime: ProductRuntime, base: list[str]) -> list[str]:
    """收口：池内过滤 + 必需节点补齐 + 收尾形状保持 + 依赖顺序。

    与 ``planning.sanitize`` 同一职责，但边界是**产品**的节点池与必需节点。
    """
    allow = set(runtime.node_pool)
    kept = [n for n in picked if n in allow]
    kept = list(dict.fromkeys(kept))

    # 收尾形状：文书中心刻意不产出报告（交付物由模板渲染），不允许加 ReportNode；
    # 其余产品必须带 ReportNode 收尾。
    if runtime.force_report:
        if "ReportNode" not in kept:
            kept.append("ReportNode")
    else:
        kept = [n for n in kept if n != "ReportNode"]

    # 必需节点补齐（产品 Agent 不能砍掉自己赖以成立的节点）
    for n in runtime.essential:
        if n in allow and n not in kept:
            kept.append(n)

    # 依赖顺序（不能打乱 Intake → ... → Report 的先后关系）
    kept = sorted(kept, key=_rank)

    # 全被裁空或裁成空壳 → 退回基线
    if not kept:
        return list(base)
    return kept


def _rank(node: str) -> int:
    return NODE_ORDER.index(node) if node in NODE_ORDER else len(NODE_ORDER)


__all__ = ["product_plan"]
