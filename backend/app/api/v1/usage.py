"""对话 / 技能使用记录 API（监控中心数据源）。

读取 ``la_usage_log``（由 ``chat.py`` 的 ``_log_usage`` 在每次专家对话 / AI 助手 /
产品入口问答后逐条写入），供监控中心「对话使用记录」区块展示用户历史提问与使用留痕。

与三张表刻意区分：
- ``/evolution/*`` 读的是 Skill 观测 / 健康诊断（la_trace 等），描述"技能跑得怎么样"；
- ``/tasks/stats`` 读的是 ``la_task``，描述"任务跑没跑完"；
- 本接口读 ``la_usage_log``，描述"用户问过什么 / 命中了哪个技能" ——
  监控中心缺的正是这一块：之前问过的内容都写进了库，却没有接口把它读出来展示。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from backend.app.api.deps import RequireUser
from backend.app.db.models import UsageLog
from backend.app.db.session import session_scope

router = APIRouter(prefix="/usage", tags=["使用记录"])


@router.get("/logs", summary="对话使用记录（la_usage_log 明细）")
def usage_logs(
    user: RequireUser,
    limit: int = Query(default=50, ge=1, le=200),
    skill_id: str = Query(default="", description="按技能 id 过滤；留空不限制"),
    expert_id: str = Query(default="", description="按专家 id 过滤；留空不限制"),
) -> dict[str, Any]:
    """最近使用留痕。管理员看全所，非管理员只看本人（与 /tasks/stats 同口径）。

    字段与 ``la_usage_log`` 对齐：id / user_id / expert_id / skill_id / intent /
    confidence / elapsed_ms / created_at。前端「对话使用记录」表格直接按此渲染。
    """
    conds = [] if user.is_admin else [UsageLog.user_id == user.id]
    if skill_id:
        conds.append(UsageLog.skill_id == skill_id)
    if expert_id:
        conds.append(UsageLog.expert_id == expert_id)

    with session_scope() as db:
        total = int(db.execute(
            select(func.count()).select_from(UsageLog).where(*conds)
        ).scalar_one_or_none() or 0)
        skill_hit = int(db.execute(
            select(func.count()).select_from(UsageLog)
            .where(UsageLog.skill_id != "", *conds)
        ).scalar_one_or_none() or 0)
        rows = db.execute(
            select(UsageLog).where(*conds)
            .order_by(UsageLog.id.desc())
            .limit(limit)
        ).scalars().all()

    items = [
        {
            "id": r.id,
            "user_id": r.user_id,
            "expert_id": r.expert_id or "",
            "skill_id": r.skill_id or "",
            "intent": r.intent or "",
            "confidence": r.confidence,
            "elapsed_ms": r.elapsed_ms,
            "created_at": (
                r.created_at.isoformat(sep=" ", timespec="seconds")
                if r.created_at is not None else None
            ),
        }
        for r in rows
    ]
    return {
        "items": items,
        "count": len(items),
        "total": total,
        "skill_hit": skill_hit,
        "scope": "本人" if not user.is_admin else "全所",
    }
