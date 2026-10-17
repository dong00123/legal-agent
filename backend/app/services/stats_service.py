"""运营统计服务（管理后台「运营统计」页）。

方案 12.3 要求四类指标：**任务量、文书采纳率、引用校验拦截率、各 Agent 耗时分布**。
前三个分别来自 ``la_task`` / ``la_draft`` / ``la_citation``；
第四个只能从 ``la_task.trace`` 里算 —— 它是唯一记录了每个节点 ``elapsed_ms`` 的地方。

一个必须说清的口径问题：**「耗时分布」统计的是「节点自身的耗时」还是「含子调用的总耗时」？**
Trace 事件里带 ``elapsed_ms`` 的主要是 ``step`` 事件，它衡量的是节点执行时间，
里面包含了该节点发起的 LLM 调用与工具调用。所以这里的口径是
**节点墙钟耗时（含其内部调用）**，并把工具调用单独拉出来看。
两个数字相加没有意义，界面上必须分开标注 ——
否则会出现"各节点加起来超过任务总时长"这种看起来很荒谬的报表。

另外**采纳率不是"生成即采纳"**：``la_draft`` 里 ``exported_path`` 非空才算导出过，
而"导出"是律师把它变成正式文书前的最后一步。这是目前唯一可得的近似口径，
在返回值里如实标注为 ``proxy``，不要包装成精确指标。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from backend.app.db.models import TaskRecord
from backend.app.db.session import session_scope

logger = logging.getLogger("legal_agent.services.stats")


def _since(days: int) -> datetime:
    return datetime.now() - timedelta(days=max(1, int(days or 7)))


# --------------------------------------------------------------------------- 任务量
def task_volume(*, days: int = 7) -> dict[str, Any]:
    """任务量与趋势（按天）。"""
    start = _since(days)
    with session_scope() as db:
        total = int(db.execute(select(func.count()).select_from(TaskRecord)).scalar_one() or 0)
        recent = int(db.execute(
            select(func.count()).select_from(TaskRecord).where(TaskRecord.created_at >= start)
        ).scalar_one() or 0)
        by_status = {str(s or ""): int(c or 0) for s, c in db.execute(
            select(TaskRecord.status, func.count()).group_by(TaskRecord.status)
        ).all()}
        by_intent = {str(i or ""): int(c or 0) for i, c in db.execute(
            select(TaskRecord.intent, func.count()).group_by(TaskRecord.intent)
        ).all()}
        by_source = {str(s or ""): int(c or 0) for s, c in db.execute(
            select(TaskRecord.source_client, func.count()).group_by(TaskRecord.source_client)
        ).all()}
        by_kind = {str(k or ""): int(c or 0) for k, c in db.execute(
            select(TaskRecord.kind, func.count()).group_by(TaskRecord.kind)
        ).all()}
        avg_conf = db.execute(
            select(func.avg(TaskRecord.confidence)).where(TaskRecord.created_at >= start)
        ).scalar_one()
        avg_elapsed = db.execute(
            select(func.avg(TaskRecord.elapsed_ms)).where(
                TaskRecord.elapsed_ms > 0, TaskRecord.created_at >= start)
        ).scalar_one()
        by_day = {
            str(d): int(c or 0)
            for d, c in db.execute(
                select(func.date(TaskRecord.created_at), func.count())
                .where(TaskRecord.created_at >= start)
                .group_by(func.date(TaskRecord.created_at))
                .order_by(func.date(TaskRecord.created_at))
            ).all()
        }
    return {
        "total": total, "recent": recent, "days": int(days),
        "by_status": by_status, "by_intent": by_intent,
        "by_source_client": by_source, "by_kind": by_kind,
        "by_day": by_day,
        "avg_confidence": round(float(avg_conf or 0.0), 3),
        "avg_elapsed_ms": int(avg_elapsed or 0),
    }


# --------------------------------------------------------------------------- 耗时分布
def agent_elapsed(*, days: int = 7, top: int = 30) -> dict[str, Any]:
    """各 Agent（节点）耗时分布，数据源是 ``la_task.trace``。

    见模块 docstring 关于口径的说明：这里的 ``total_ms`` 是
    **节点墙钟耗时（含其内部 LLM / 工具调用）**，不是"独占耗时"。
    """
    start = _since(days)
    with session_scope() as db:
        rows = db.execute(
            select(TaskRecord.trace, TaskRecord.elapsed_ms)
            .where(TaskRecord.created_at >= start)
            .order_by(TaskRecord.id.desc()).limit(500)
        ).all()

    node_ms: dict[str, int] = defaultdict(int)
    node_calls: dict[str, int] = defaultdict(int)
    node_errors: dict[str, int] = defaultdict(int)
    task_total = 0
    tasks = 0

    for trace, elapsed in rows:
        if not isinstance(trace, list):
            continue
        tasks += 1
        task_total += int(elapsed or 0)
        for ev in trace:
            if not isinstance(ev, dict):
                continue
            node = str(ev.get("node") or "")
            if not node:
                continue
            ms = int(ev.get("elapsed_ms") or 0)
            if ms > 0:
                node_ms[node] += ms
                node_calls[node] += 1
            if str(ev.get("level") or "") in ("error", "warning"):
                node_errors[node] += 1

    items = [
        {
            "node": node,
            "total_ms": total,
            "calls": node_calls.get(node, 0),
            "avg_ms": int(total / node_calls[node]) if node_calls.get(node) else 0,
            "warnings": node_errors.get(node, 0),
            "share": round(total / task_total, 3) if task_total else 0.0,
        }
        for node, total in node_ms.items()
    ]
    items.sort(key=lambda x: -x["total_ms"])
    return {
        "tasks_sampled": tasks,
        "task_total_ms": task_total,
        "nodes": items[: max(1, int(top or 30))],
        "caliber": "节点墙钟耗时（含其内部 LLM / 工具调用），各项之和可超过任务总时长",
    }


# --------------------------------------------------------------------------- 工具
def tool_usage(*, days: int = 7, top: int = 30) -> dict[str, Any]:
    """工具调用分布与失败率（数据源 ``la_task.tool_calls``）。

    **字段严格对应 ``ToolCallRecord``**（step / tool / agent / args_summary / ok /
    elapsed_ms / retries / error / cached）。早先这里统计过一个 ``degraded`` 字段，
    但 ``ToolCallRecord`` 里根本没有它 —— 于是报表上会恒定显示一列 0，
    看起来像"降级从未发生"，而实际降级信息在 ``ToolResult`` 里、不在 State 里。
    这类"永远是 0 的漂亮指标"比缺一列危险得多，所以直接去掉，
    换成确实存在的 ``cached``。
    """
    start = _since(days)
    with session_scope() as db:
        rows = db.execute(
            select(TaskRecord.tool_calls).where(TaskRecord.created_at >= start)
            .order_by(TaskRecord.id.desc()).limit(500)
        ).scalars().all()

    calls: dict[str, int] = defaultdict(int)
    fails: dict[str, int] = defaultdict(int)
    cached: dict[str, int] = defaultdict(int)
    retries: dict[str, int] = defaultdict(int)
    ms_sum: dict[str, int] = defaultdict(int)
    agents: dict[str, set[str]] = defaultdict(set)

    for items in rows:
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            tool = str(it.get("tool") or "")
            if not tool:
                continue
            calls[tool] += 1
            ms_sum[tool] += int(it.get("elapsed_ms") or 0)
            retries[tool] += int(it.get("retries") or 0)
            if not it.get("ok", True):
                fails[tool] += 1
            if it.get("cached"):
                cached[tool] += 1
            if it.get("agent"):
                agents[tool].add(str(it["agent"]))

    out = [
        {
            "tool": t,
            "calls": n,
            "fails": fails.get(t, 0),
            "cached": cached.get(t, 0),
            "retries": retries.get(t, 0),
            "fail_rate": round(fails.get(t, 0) / n, 3) if n else 0.0,
            "avg_ms": int(ms_sum[t] / n) if n else 0,
            "agents": sorted(agents.get(t, ())),
        }
        for t, n in calls.items()
    ]
    out.sort(key=lambda x: -x["calls"])
    return {"tools": out[: max(1, int(top or 30))], "total_calls": sum(calls.values())}


# --------------------------------------------------------------------------- 汇总
def dashboard(*, days: int = 7) -> dict[str, Any]:
    """管理后台首页：一次拿齐所有指标。"""
    from backend.app.services import case_service, citation_service, draft_service
    from backend.app.services import knowledge_service, user_service

    cites = citation_service.stats()
    drafts = draft_service.stats()
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "days": int(days),
        "tasks": task_volume(days=days),
        "cases": case_service.stats(),
        "users": user_service.stats(),
        "knowledge": knowledge_service.stats(),
        "statutes": knowledge_service.statute_stats(),
        "citations": cites,
        "drafts": drafts,
        # 采纳率口径说明（见模块 docstring）
        "adoption": {
            "rate": drafts["export_rate"],
            "proxy": True,
            "caliber": "已导出草稿数 / 全部草稿数；导出是律师把草稿转为正式文书前的最后一步，"
                       "作为采纳率的近似口径",
        },
        "top_rejected_citations": citation_service.top_rejected(limit=10),
    }


def full_report(*, days: int = 7) -> dict[str, Any]:
    """完整报表（运营统计页）。"""
    return {
        **dashboard(days=days),
        "agent_elapsed": agent_elapsed(days=days),
        "tool_usage": tool_usage(days=days),
    }


__all__ = [
    "agent_elapsed",
    "dashboard",
    "full_report",
    "task_volume",
    "tool_usage",
]
