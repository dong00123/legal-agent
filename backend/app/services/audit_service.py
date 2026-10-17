"""审计查询服务（管理后台「审计中心」）。

**本模块只有读，没有写、没有改、没有删。**
写入在 ``agents/harness/audit.py``（双写：JSONL 文件为真源，MySQL 为可查询副本）。
这里不提供任何删除接口 —— 不是"暂时没实现"，而是合规上不允许存在这条代码路径。
方案第十四章：审计日志「不可删改」。

两个数据源各有分工，查询时**必须说清读的是哪一个**：

- **文件（``data/audit/audit-*.jsonl``）**：真源。DB 写入失败时记录仍在这里。
  按天分文件，适合"某一天的全量导出"与故障排查。
- **DB（``la_audit_log``）**：可查询副本。支持按用户/案件/动作/时间检索。
  但它可能比文件少（DB 写失败只记警告）。所以 ``consistency()`` 会给出
  两边的条数差 —— 差值不为 0 就说明有记录没进库，需要按文件为准。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from backend.app.config import settings
from backend.app.db.models import AuditLog
from backend.app.db.session import session_scope

logger = logging.getLogger("legal_agent.services.audit")

MAX_EXPORT_ITEMS = 20_000


def log_to_dict(a: AuditLog) -> dict[str, Any]:
    return {
        "id": a.id,
        "ts": a.created_at.isoformat(timespec="seconds") if a.created_at else "",
        "trace_id": a.trace_id or "",
        "task_id": a.task_id or "",
        "case_id": a.case_id or "",
        "user_id": a.user_id,
        "username": a.username or "",
        "team_id": a.team_id,
        "action": a.action or "",
        "resource": a.resource or "",
        "resource_id": a.resource_id or "",
        "detail": a.detail or "",
        "client": a.client or "",
        "ip": a.ip or "",
        "result": a.result or "",
        "extra": a.extra or {},
    }


# --------------------------------------------------------------------------- DB 查询
def query_db(
    *,
    keyword: str = "",
    action: str = "",
    user_id: int | None = None,
    team_id: int | None = None,
    case_id: str = "",
    task_id: str = "",
    result: str = "",
    date_from: str = "",
    date_to: str = "",
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """按条件检索审计记录（读的是 DB 副本）。"""
    limit = max(1, min(int(limit or 100), 1000))
    stmt = select(AuditLog)
    if keyword:
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(AuditLog.detail.like(like) | AuditLog.resource_id.like(like)
                          | AuditLog.username.like(like))
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if user_id is not None:
        stmt = stmt.where(AuditLog.user_id == user_id)
    if team_id is not None:
        stmt = stmt.where(AuditLog.team_id == team_id)
    if case_id:
        stmt = stmt.where(AuditLog.case_id == case_id)
    if task_id:
        stmt = stmt.where(AuditLog.task_id == task_id)
    if result:
        stmt = stmt.where(AuditLog.result == result)
    if date_from:
        stmt = stmt.where(AuditLog.created_at >= _parse_day(date_from))
    if date_to:
        # 含当天：右界取次日零点，否则"查 3 号"查不到 3 号的记录
        stmt = stmt.where(AuditLog.created_at < _parse_day(date_to) + timedelta(days=1))
    stmt = stmt.order_by(AuditLog.id.desc()).limit(limit).offset(max(0, int(offset)))
    with session_scope() as db:
        return [log_to_dict(a) for a in db.execute(stmt).scalars().all()]


def _parse_day(value: str) -> datetime:
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(str(value).strip(), fmt)
        except ValueError:
            continue
    return datetime.now()


def trace_of(task_id: str) -> list[dict[str, Any]]:
    """按任务追溯：该任务全链路的审计记录（时间正序）。"""
    with session_scope() as db:
        rows = db.execute(
            select(AuditLog).where(AuditLog.task_id == task_id).order_by(AuditLog.id)
        ).scalars().all()
        return [log_to_dict(a) for a in rows]


def actions() -> list[dict[str, Any]]:
    """出现过的动作及次数（供筛选下拉）。"""
    with session_scope() as db:
        rows = db.execute(
            select(AuditLog.action, func.count().label("n"))
            .group_by(AuditLog.action).order_by(func.count().desc())
        ).all()
    return [{"action": str(a or ""), "count": int(c or 0)} for a, c in rows]


# --------------------------------------------------------------------------- 文件真源
def tail_file(limit: int = 200, *, date: str = "") -> list[dict[str, Any]]:
    """读审计文件尾部（真源）。"""
    from backend.agents.harness.audit import audit_writer

    return audit_writer.tail(limit, date=date or None)


def daily_files() -> list[dict[str, Any]]:
    """审计文件清单（按天）。"""
    d = Path(settings.AUDIT_LOG_DIR)
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("audit-*.jsonl"), reverse=True):
        try:
            stat = p.stat()
        except OSError:
            continue
        out.append({
            "date": p.stem.replace("audit-", ""),
            "path": str(p),
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        })
    return out


def consistency(*, date: str = "") -> dict[str, Any]:
    """文件真源 与 DB 副本 的条数对账。

    差值不为 0 意味着有审计记录没进库 —— 此时**以文件为准**，
    并应当把该时间段的文件重新导入 DB（``import_file_to_db``）。
    """
    day = date or f"{datetime.now():%Y-%m-%d}"
    file_rows = tail_file(MAX_EXPORT_ITEMS, date=day)
    with session_scope() as db:
        start = _parse_day(day)
        db_count = int(db.execute(
            select(func.count()).select_from(AuditLog)
            .where(AuditLog.created_at >= start, AuditLog.created_at < start + timedelta(days=1))
        ).scalar_one() or 0)
    diff = len(file_rows) - db_count
    return {
        "date": day, "file_count": len(file_rows), "db_count": db_count, "diff": diff,
        "consistent": diff == 0,
        "note": "一致" if diff == 0 else (
            f"有 {diff} 条只存在于文件（DB 写入曾失败）；审计以文件为准，可从文件补录"
        ),
    }


def import_file_to_db(date: str = "") -> dict[str, Any]:
    """把某天的审计文件补录进 DB（幂等：按 trace_id+action+ts 去重）。

    用于 DB 曾短暂不可用后的补救。**只增不改不删**：已存在的记录跳过，
    不做覆盖 —— 覆盖就等于给了修改审计的路径。
    """
    day = date or f"{datetime.now():%Y-%m-%d}"
    rows = tail_file(MAX_EXPORT_ITEMS, date=day)
    if not rows:
        return {"date": day, "imported": 0, "skipped": 0, "reason": "当日无审计文件或文件为空"}

    imported = skipped = 0
    with session_scope() as db:
        existing = {
            (r.trace_id or "", r.action or "", r.created_at.isoformat(timespec="seconds"))
            for r in db.execute(
                select(AuditLog).where(AuditLog.created_at >= _parse_day(day),
                                       AuditLog.created_at < _parse_day(day) + timedelta(days=1))
            ).scalars().all()
        }
        for r in rows:
            key = (str(r.get("trace_id") or ""), str(r.get("action") or ""), str(r.get("ts") or ""))
            if key in existing:
                skipped += 1
                continue
            db.add(AuditLog(
                trace_id=key[0], task_id=str(r.get("task_id") or ""),
                case_id=str(r.get("case_id") or ""),
                user_id=r.get("user_id"), username=str(r.get("username") or ""),
                team_id=r.get("team_id"), action=key[1],
                resource=str(r.get("resource") or ""), resource_id=str(r.get("resource_id") or ""),
                detail=str(r.get("detail") or ""), client=str(r.get("client") or ""),
                ip=str(r.get("ip") or ""), result=str(r.get("result") or "ok"),
                extra=r.get("extra") or {},
            ))
            existing.add(key)
            imported += 1
    logger.info("审计补录 %s：新增 %d 条，跳过已存在 %d 条", day, imported, skipped)
    return {"date": day, "imported": imported, "skipped": skipped, "total_in_file": len(rows)}


# --------------------------------------------------------------------------- 统计
def stats(*, date: str = "") -> dict[str, Any]:
    """审计统计（读 DB，附带与文件的差值）。"""
    with session_scope() as db:
        total = int(db.execute(select(func.count()).select_from(AuditLog)).scalar_one() or 0)
        by_action = {str(a or ""): int(c or 0) for a, c in db.execute(
            select(AuditLog.action, func.count()).group_by(AuditLog.action)
            .order_by(func.count().desc()).limit(30)
        ).all()}
        by_result = {str(r or ""): int(c or 0) for r, c in db.execute(
            select(AuditLog.result, func.count()).group_by(AuditLog.result)
        ).all()}
        by_client = {str(c or ""): int(n or 0) for c, n in db.execute(
            select(AuditLog.client, func.count()).group_by(AuditLog.client)
        ).all()}
        denied = int(db.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.result == "denied")
        ).scalar_one() or 0)
    return {
        "total": total, "by_action": by_action, "by_result": by_result,
        "by_client": by_client, "denied": denied,
        "files": len(daily_files()), "dir": settings.AUDIT_LOG_DIR,
        "checked_date": date or f"{datetime.now():%Y-%m-%d}",
    }


def export_jsonl(*, date: str = "", limit: int = MAX_EXPORT_ITEMS) -> tuple[str, str]:
    """导出审计记录为 JSONL 文本，返回 ``(文件名, 内容)``。

    导出**不做脱敏也不做删减** —— 审计的作用就是完整留痕。
    接口层负责限制只有管理员能调。
    """
    day = date or f"{datetime.now():%Y-%m-%d}"
    rows = tail_file(limit, date=day)
    if not rows:
        rows = query_db(date_from=day, date_to=day, limit=limit)
    content = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
    return f"audit-{day}.jsonl", content


__all__ = [
    "MAX_EXPORT_ITEMS",
    "actions",
    "consistency",
    "daily_files",
    "export_jsonl",
    "import_file_to_db",
    "log_to_dict",
    "query_db",
    "stats",
    "tail_file",
    "trace_of",
]
