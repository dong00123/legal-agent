"""监控中心统计服务：专家使用与技能命中聚合。

数据源 ``la_usage_log``（轻量使用记录，见 :mod:`backend.app.db.models.usage_log`）。

口径：
- ``days`` 是自然天窗口（``created_at >= now - days``），最小 1 天；
- 按天趋势以**本地日期** ``YYYY-MM-DD`` 分组（DB 层 ``CAST(created_at AS DATE)``），
  日期随数字一起返回，避免"只看总数、不知道窗口"的误读；
- 专家名 / 技能名统一在**服务层**反查注册表（``expert_registry`` / ``skill_registry`` /
  ``backend.skills``），查不到时回退为原始 id —— 前端不用再拉两份注册表。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Date, cast, func, select

from backend.app.db.models import UsageLog
from backend.app.db.session import session_scope

logger = logging.getLogger("legal_agent.services.monitor")


def _since(days: int) -> datetime:
    return datetime.now() - timedelta(days=max(1, int(days or 7)))


def _expert_name(expert_id: str) -> str:
    from backend.app.services.expert_registry import expert_registry

    item = expert_registry.get(expert_id)
    if item and (item.get("name") or ""):
        return item["name"]
    return expert_id or "(未指定)"


def _skill_name(skill_id: str) -> str:
    from backend.app.services.skill_registry import skill_registry
    from backend.skills import get_skill as flow_skill

    # 技能市场注册表（sk-*）
    item = skill_registry.get(skill_id)
    if item and (item.get("name") or ""):
        return item["name"]
    # 流程内置技能注册表（case_research / contract_review / doc_draft）
    try:
        spec = flow_skill(skill_id)
        if spec and spec.name:
            return spec.name
    except Exception:  # noqa: BLE001 - 查不到就回退原始 id
        pass
    return skill_id or "(未指定)"


def expert_usage(*, days: int = 7) -> dict[str, Any]:
    """按专家聚合：使用次数 / 平均置信度 / 平均耗时 / 最近使用 / 按天趋势。"""
    start = _since(days)
    with session_scope() as db:
        total = int(db.execute(
            select(func.count()).select_from(UsageLog)
            .where(UsageLog.created_at >= start, UsageLog.expert_id != "")
        ).scalar_one() or 0)
        rows = db.execute(
            select(
                UsageLog.expert_id,
                func.count(),
                func.avg(UsageLog.confidence),
                func.avg(UsageLog.elapsed_ms),
                func.max(UsageLog.created_at),
            )
            .where(UsageLog.created_at >= start, UsageLog.expert_id != "")
            .group_by(UsageLog.expert_id)
            .order_by(func.count().desc())
        ).all()
        trend = [
            {"date": str(d or ""), "count": int(c or 0)}
            for d, c in db.execute(
                select(cast(UsageLog.created_at, Date), func.count())
                .where(UsageLog.created_at >= start, UsageLog.expert_id != "")
                .group_by(cast(UsageLog.created_at, Date))
                .order_by(cast(UsageLog.created_at, Date))
            ).all()
        ]
    return {
        "days": days,
        "total": total,
        "items": [
            {
                "expert_id": r.expert_id,
                "name": _expert_name(r.expert_id),
                "count": int(r[1] or 0),
                "avg_confidence": round(float(r[2]), 3) if r[2] is not None else None,
                "avg_elapsed_ms": int(r[3] or 0) if r[3] is not None else 0,
                "last_used_at": r[4].isoformat(timespec="seconds") if r[4] else "",
            }
            for r in rows
        ],
        "trend": trend,
    }


def skill_usage(*, days: int = 7) -> dict[str, Any]:
    """按技能聚合：命中次数 / 命中意图分布 / 按天趋势（仅统计命中过的技能）。"""
    start = _since(days)
    with session_scope() as db:
        total = int(db.execute(
            select(func.count()).select_from(UsageLog)
            .where(UsageLog.created_at >= start, UsageLog.skill_id != "")
        ).scalar_one() or 0)
        rows = db.execute(
            select(UsageLog.skill_id, UsageLog.intent, func.count())
            .where(UsageLog.created_at >= start, UsageLog.skill_id != "")
            .group_by(UsageLog.skill_id, UsageLog.intent)
            .order_by(UsageLog.skill_id)
        ).all()
        items: dict[str, dict[str, Any]] = {}
        for skill_id, intent, cnt in rows:
            it = items.setdefault(
                skill_id,
                {"skill_id": skill_id, "name": "", "count": 0, "intents": {}},
            )
            it["count"] += int(cnt or 0)
            it["intents"][intent or "(未命中意图)"] = int(cnt or 0)
        trend = [
            {"date": str(d or ""), "count": int(c or 0)}
            for d, c in db.execute(
                select(cast(UsageLog.created_at, Date), func.count())
                .where(UsageLog.created_at >= start, UsageLog.skill_id != "")
                .group_by(cast(UsageLog.created_at, Date))
                .order_by(cast(UsageLog.created_at, Date))
            ).all()
        ]
    ordered = sorted(items.values(), key=lambda x: x["count"], reverse=True)
    for it in ordered:
        it["name"] = _skill_name(it["skill_id"])
    return {"days": days, "total": total, "items": ordered, "trend": trend}


def usage_logs(*, days: int = 7, limit: int = 100) -> dict[str, Any]:
    """返回近 ``days`` 天的调用明细（按时间倒序，最多 ``limit`` 条）。

    供监控中心健康诊断使用：前端基于明细过滤低置信度（confidence < 0.6）
    与慢调用（elapsed_ms > 3000），并聚合出总调用 / 今日调用等概览指标。
    """
    start = _since(days)
    with session_scope() as db:
        rows = db.execute(
            select(
                UsageLog.id,
                UsageLog.created_at,
                UsageLog.expert_id,
                UsageLog.skill_id,
                UsageLog.intent,
                UsageLog.confidence,
                UsageLog.elapsed_ms,
            )
            .where(UsageLog.created_at >= start)
            .order_by(UsageLog.created_at.desc())
            .limit(limit)
        ).all()
    return {
        "days": days,
        "total": len(rows),
        "items": [
            {
                "id": r.id,
                "created_at": r.created_at.isoformat(timespec="seconds") if r.created_at else "",
                "expert_id": r.expert_id,
                "expert_name": _expert_name(r.expert_id),
                "skill_id": r.skill_id,
                "skill_name": _skill_name(r.skill_id),
                "intent": r.intent or "",
                "confidence": round(float(r.confidence), 3) if r.confidence is not None else None,
                "elapsed_ms": int(r.elapsed_ms or 0) if r.elapsed_ms is not None else 0,
            }
            for r in rows
        ],
    }
