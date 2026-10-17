"""引用记录服务：把 Citation Validator 的终审结果落进 ``la_citation``。

这张表此前零写入 —— Validator 把逐条核对结果写在 State 里、报告里也体现了，
但**没有落表**。于是管理后台的「引用校验拦截率」算不出来，
而执业责任追溯要求的"某次输出引用了哪条法条、当时核对结论是什么"，
只能靠任务快照里的 JSON 翻找 —— 而快照是会被裁剪的（见 ``task_service`` 的
``shrink_state``：列表超 400 条即截断）。

两件事因此必须落表而不是留在 JSON 里：

1. **被拦截的引用。** ``citation_check="failed"`` 时被剔除的那几条是合规证据，
   必须独立可查。它们恰恰是最容易被"保留前 400 条"裁掉的部分。
2. **按法条聚合的查询。** "这条法条被引用过几次、有没有被拦过" ——
   这是评估法规库质量与模型引用行为的基础数据，JSON 里算不出来。

写入纪律：同一 ``task_id`` 重复落库时**先清后写**（幂等），
否则任务重跑会在表里留下两份互相矛盾的核对结论。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, func, select

from backend.app.db.models import Citation
from backend.app.db.session import session_scope

logger = logging.getLogger("legal_agent.services.citation")

CITATION_TYPES = ("statute", "case", "knowledge")


def citation_to_dict(c: Citation) -> dict[str, Any]:
    return {
        "id": c.id,
        "task_id": c.task_id or "",
        "case_id": c.case_id or "",
        "type": c.ctype or "statute",
        "name": c.name or "",
        "article": c.article or "",
        "case_no": c.case_no or "",
        "court": c.court or "",
        "status": c.status or "",
        "version": c.version or "",
        # **不返回 ``source_path``**：它是语料文件在服务器上的绝对路径，
        # 对任何调用方都没有用途（前端明确不渲染它），却会泄露服务端目录结构。
        # 需要追溯来源时查库，而不是把它放进每个 API 响应里。
        "raw": c.raw or "",
        "verified": bool(c.verified),
        "reason": c.reason or "",
        "confidence": float(c.confidence or 0.0),
        "created_at": c.created_at.isoformat(timespec="seconds") if c.created_at else "",
    }


def save_from_state(task_id: str, state: dict[str, Any]) -> dict[str, Any]:
    """把一次任务的引用清单落表（幂等：先按 task_id 清空再写）。

    返回 ``{"saved": n, "verified": n, "rejected": n}``。
    """
    task_id = str(task_id or state.get("task_id") or "")
    if not task_id:
        return {"saved": 0, "verified": 0, "rejected": 0, "reason": "缺少 task_id"}

    items = list(state.get("citations") or [])
    if not items:
        # 没有引用也要清一遍 —— 任务重跑后引用可能全部被拦，
        # 此时"旧记录还在"比"没有记录"更危险。
        with session_scope() as db:
            db.execute(delete(Citation).where(Citation.task_id == task_id))
        return {"saved": 0, "verified": 0, "rejected": 0,
                "reason": "本次输出无引用（可能全部被引用校验拦截）"}

    case_id = str(state.get("case_id") or "")
    rows: list[Citation] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        rows.append(Citation(
            task_id=task_id,
            case_id=case_id,
            ctype=str(it.get("type") or "statute")[:16],
            name=str(it.get("name") or "")[:256],
            article=str(it.get("article") or "")[:64],
            case_no=str(it.get("case_no") or "")[:96],
            court=str(it.get("court") or "")[:128],
            status=str(it.get("status") or "")[:16],
            version=str(it.get("version") or "")[:64],
            source_path=str(it.get("source_path") or "")[:512],
            raw=str(it.get("raw") or "")[:2000],
            verified=bool(it.get("verified")),
            reason=str(it.get("reason") or "")[:512],
            confidence=float(it.get("confidence") or (1.0 if it.get("verified") else 0.0)),
        ))

    with session_scope() as db:
        db.execute(delete(Citation).where(Citation.task_id == task_id))
        for r in rows:
            db.add(r)

    verified = sum(1 for r in rows if r.verified)
    logger.info("引用记录已落表：task=%s 共 %d 条（通过 %d，拦截 %d）",
                task_id, len(rows), verified, len(rows) - verified)
    return {"saved": len(rows), "verified": verified, "rejected": len(rows) - verified}


def list_citations(
    *,
    task_id: str = "",
    case_id: str = "",
    ctype: str = "",
    verified: bool | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 100), 500))
    stmt = select(Citation)
    if task_id:
        stmt = stmt.where(Citation.task_id == task_id)
    if case_id:
        stmt = stmt.where(Citation.case_id == case_id)
    if ctype:
        stmt = stmt.where(Citation.ctype == ctype)
    if verified is not None:
        stmt = stmt.where(Citation.verified.is_(bool(verified)))
    stmt = stmt.order_by(Citation.id.desc()).limit(limit).offset(max(0, int(offset)))
    with session_scope() as db:
        return [citation_to_dict(c) for c in db.execute(stmt).scalars().all()]


def stats(*, case_id: str = "") -> dict[str, Any]:
    """引用统计。``intercept_rate`` 是管理后台「引用校验拦截率」的来源。"""
    with session_scope() as db:
        base = select(func.count()).select_from(Citation)
        if case_id:
            base = base.where(Citation.case_id == case_id)
        total = int(db.execute(base).scalar_one() or 0)
        verified = int(db.execute(
            base.where(Citation.verified.is_(True))
        ).scalar_one() or 0)
        by_type = {}
        for t, c in db.execute(
            select(Citation.ctype, func.count()).group_by(Citation.ctype)
        ).all():
            by_type[str(t or "")] = int(c or 0)
        tasks = int(db.execute(
            select(func.count(func.distinct(Citation.task_id)))
        ).scalar_one() or 0)
    rejected = max(0, total - verified)
    return {
        "total": total,
        "verified": verified,
        "rejected": rejected,
        "intercept_rate": round(rejected / total, 3) if total else 0.0,
        "by_type": by_type,
        "tasks_with_citations": tasks,
    }


def top_rejected(limit: int = 20) -> list[dict[str, Any]]:
    """被拦截最多的引用对象 —— 用来发现"模型总在瞎引哪一条"。"""
    with session_scope() as db:
        rows = db.execute(
            select(Citation.name, Citation.article, func.count().label("n"))
            .where(Citation.verified.is_(False))
            .group_by(Citation.name, Citation.article)
            .order_by(func.count().desc())
            .limit(max(1, min(int(limit or 20), 100)))
        ).all()
    return [{"name": str(n or ""), "article": str(a or ""), "count": int(c or 0)}
            for n, a, c in rows]


__all__ = [
    "CITATION_TYPES",
    "citation_to_dict",
    "list_citations",
    "save_from_state",
    "stats",
    "top_rejected",
]
