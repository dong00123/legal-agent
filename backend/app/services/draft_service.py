"""文书草稿服务：支撑方案 12.3 的「草稿同步（Web ↔ EXE 同一账号无缝续编）」。

``la_draft`` 此前零写入：DraftNode 生成的文书只躺在 State 与任务快照里，
而任务快照是**会被裁剪的**（``shrink_state`` 对超长文本截断、对长列表限量）。
结果是"草稿同步"这件事在界面上无从实现 —— 律师在 Web 端改了一半的起诉状，
关掉浏览器就找不回来。

设计要点：

1. **版本号自增，且旧版本不删。** 律师改文书是"改错了要退回去"的高频场景。
   直接覆盖 content 等于把退路砍掉。``save`` 每次追加一版，
   ``list`` 默认只给最新版，``history`` 给全部。

2. **草稿与任务解耦。** 草稿可以来自任务（``task_id`` 非空），
   也可以是律师从空白起稿（``task_id`` 为空）。用 task_id 做主键会导致
   第二种情况根本存不下来。

3. **导出路径单独一列，且只在本服务内部流动。** 导出的 docx 是
   "这一版的快照产物"，与 content 不是同一件事：律师可能导出后又改了正文，
   此时导出物指向的仍是上一次导出的内容，界面上要能区分，所以不合并进 content。

   但**磁盘路径不出 ``draft_to_dict``** —— 只出 ``exported`` (bool) 与
   ``exported_name``（文件名）。理由与 ``case_service.material_path`` 同一条：
   服务端绝对路径一旦进了响应体，就会连部署用户名与目录结构一起泄露出去，
   而界面根本不需要它（下载走 ``GET /drafts/{id}/download``）。
   需要路径的只有 ``export_path()`` 一个出口。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, or_, select

from backend.app.db.models import DraftRecord, DraftVersion
from backend.app.db.session import session_scope

logger = logging.getLogger("legal_agent.services.draft")

# 与 ``agents/nodes/draft_node.DRAFT_LABELS``（DraftNode 实际会产出的类型）对齐。
# ⚠ 曾经这里只有 6 个值，少了 ``arbitration_application`` / ``appeal`` / ``legal_opinion``，
# 后果是「劳动仲裁申请书」这类文书**保存直接 400**（错误信息里还列着另一套类型名，
# 前端完全看不出是白名单不一致）。加类型时两边一起加。
DOC_TYPES = ("complaint", "defense", "arbitration_application", "appeal",
             "legal_opinion", "opinion", "revision", "contract", "other")


class DraftError(ValueError):
    """业务校验失败（接口层转 400）。"""


def draft_to_dict(d: DraftRecord, *, with_content: bool = False) -> dict[str, Any]:
    """草稿的可外发视图。

    **不含磁盘路径。** ``exported_path`` 只转成 ``exported``（是否导出过）与
    ``exported_name``（文件名）—— 界面要判断的是"上次导出的是哪一版、叫什么名字"，
    服务端绝对路径对它没有用，却会把部署目录结构带出去。
    要读文件的一律走 :func:`export_path`。

    **这是个白名单**：新加的列不写进来就不会出现在任何响应里（好处是新列
    不会意外泄露）。所以加列时**必须**同步改这里，否则表现是"字段存进去了、
    接口读不到"，在前端看就是"改了没反应"。

    大字段（``fills`` / ``content_html`` / ``chat_log``）只在 ``with_content=True``
    时给：列表接口用默认值调用，把每份草稿的富文本正文和整段对话都捎上会让
    草稿列表变得很重，而列表页一个都用不到。``template_id`` 反过来总是给 ——
    它只是个短 id，列表上要显示"这份是套模板生成的"。
    """
    raw_path = d.exported_path or ""
    out: dict[str, Any] = {
        "draft_id": d.draft_id,
        "task_id": d.task_id or "",
        "case_id": d.case_id or "",
        "user_id": d.user_id,
        "doc_type": d.doc_type or "",
        "title": d.title or "",
        "version": int(d.version or 1),
        "citations": d.citations or [],
        "exported": bool(raw_path),
        "exported_name": Path(raw_path).name if raw_path else "",
        "content_chars": len(d.content or ""),
        # 后加的列在旧行上是 NULL（reconcile_schema 加列不填默认值），
        # 所以这里一律 `or ""` / `or {}` / `or []`。
        "template_id": d.template_id or "",
        "updated_at": d.updated_at.isoformat(timespec="seconds") if d.updated_at else "",
        "created_at": d.created_at.isoformat(timespec="seconds") if d.created_at else "",
    }
    if with_content:
        out["content"] = d.content or ""
        out["fills"] = d.fills or {}
        out["content_html"] = d.content_html or ""
        out["chat_log"] = d.chat_log or []
    return out


def export_path(draft_id: str) -> str:
    """取草稿"最近一次导出"的磁盘路径。**这是唯一出口。**

    **为什么不给 ``draft_to_dict`` 加个 ``with_path=True``。** 它的返回值会直接
    进 HTTP 响应体（列表、详情、导出回执都在用），只要它"可能带路径"，
    就迟早有调用方忘了剥离 —— ``stored_path`` 已经这样漏过一次了。

    返回 ``""`` 表示该草稿没导出过（调用方按业务决定报 404 还是别的），
    草稿本身不存在也返回 ``""``：调用方在此之前都已经查过一次存在性。
    """
    with session_scope() as db:
        d = db.execute(
            select(DraftRecord).where(DraftRecord.draft_id == draft_id)
        ).scalar_one_or_none()
        return (d.exported_path or "") if d else ""


def create_draft(
    *,
    content: str,
    doc_type: str = "complaint",
    title: str = "",
    case_id: str = "",
    task_id: str = "",
    user_id: int | None = None,
    citations: list[dict[str, Any]] | None = None,
    draft_id: str = "",
    template_id: str = "",
    fills: dict[str, str] | None = None,
    content_html: str = "",
    chat_log: list[dict[str, Any]] | None = None,
    snapshot: bool = False,
) -> dict[str, Any]:
    if doc_type not in DOC_TYPES:
        raise DraftError(f"文书类型只能是 {' / '.join(DOC_TYPES)}")
    if not (content or "").strip():
        raise DraftError("草稿正文不能为空")

    draft_id = draft_id or f"draft_{uuid.uuid4().hex[:12]}"
    with session_scope() as db:
        d = DraftRecord(
            draft_id=draft_id, task_id=task_id or "", case_id=case_id or "",
            user_id=user_id, doc_type=doc_type, title=title or _auto_title(doc_type, content),
            content=content, version=1, citations=list(citations or []),
            template_id=template_id or "", fills=dict(fills or {}),
            content_html=content_html or "", chat_log=list(chat_log or []),
        )
        db.add(d)
        db.flush()
        if snapshot:
            _snapshot(db, d, source="compose", note="首次生成")
        logger.info("草稿已创建：%s（%s，%d 字）", draft_id, doc_type, len(content))
        return draft_to_dict(d, with_content=True)


def save_draft(
    draft_id: str,
    *,
    content: str,
    title: str = "",
    user_id: int | None = None,
    citations: list[dict[str, Any]] | None = None,
    template_id: str | None = None,
    fills: dict[str, str] | None = None,
    content_html: str | None = None,
    chat_log: list[dict[str, Any]] | None = None,
    snapshot: bool = False,
    snapshot_source: str = "manual",
    snapshot_note: str = "",
) -> dict[str, Any]:
    """保存新版本（版本号自增，见模块 docstring 第 1 条）。

    ``snapshot`` **默认 False，且只有 docs / revise 路由会传 True。**
    这里刻意不"默认拍快照"：``save_from_state`` 在每个 agent 任务结束时都会跑
    （``orchestrator``），而它有三种噪声源 —— 同一 task 重跑、缓存命中落新草稿、
    以及只改了个标点的保存。默认拍的话历史会被刷爆，"看看上一版"反而找不到东西。
    """
    if not (content or "").strip():
        raise DraftError("草稿正文不能为空")
    with session_scope() as db:
        d = db.execute(
            select(DraftRecord).where(DraftRecord.draft_id == draft_id)
        ).scalar_one_or_none()
        if d is None:
            return {"ok": False, "reason": f"草稿不存在：{draft_id}"}
        prev = d.content or ""
        # 内容真没变就不涨版本、也不拍快照 —— 否则"保存"按钮连点两下就是两条历史
        unchanged = (prev == content
                     and (content_html is None or (d.content_html or "") == content_html)
                     and (fills is None or (d.fills or {}) == fills))
        d.content = content
        if template_id is not None:
            d.template_id = template_id
        if fills is not None:
            d.fills = dict(fills)
        if content_html is not None:
            d.content_html = content_html
        if chat_log is not None:
            d.chat_log = list(chat_log)
        if unchanged:
            # 版本号不动，但上面的字段赋值照旧落库（可能只改了对话记录）
            out = draft_to_dict(d, with_content=True)
            out["ok"] = True
            out["unchanged"] = True
            return out
        d.version = int(d.version or 1) + 1
        if title:
            d.title = title
        if citations is not None:
            d.citations = list(citations)
        if snapshot:
            _snapshot(db, d, source=snapshot_source, note=snapshot_note)
        out = draft_to_dict(d, with_content=True)
        out["ok"] = True
        if prev and len(prev) != len(content):
            out["size_delta"] = len(content) - len(prev)
        return out


def get_draft(draft_id: str, *, with_content: bool = True) -> dict[str, Any] | None:
    with session_scope() as db:
        d = db.execute(
            select(DraftRecord).where(DraftRecord.draft_id == draft_id)
        ).scalar_one_or_none()
        return draft_to_dict(d, with_content=with_content) if d else None


def _draft_stmt(*, user_id: int | None = None, case_id: str = "", task_id: str = "",
                doc_type: str = "", keyword: str = ""):
    stmt = select(DraftRecord)
    if user_id is not None:
        stmt = stmt.where(DraftRecord.user_id == user_id)
    if case_id:
        stmt = stmt.where(DraftRecord.case_id == case_id)
    if task_id:
        stmt = stmt.where(DraftRecord.task_id == task_id)
    if doc_type:
        stmt = stmt.where(DraftRecord.doc_type == doc_type)
    if keyword:
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(or_(DraftRecord.title.like(like), DraftRecord.content.like(like)))
    return stmt


def count_drafts(*, user_id: int | None = None, case_id: str = "", task_id: str = "",
                 doc_type: str = "", keyword: str = "") -> int:
    """与 list_drafts 同过滤条件的总数（分页接口的 ``total`` 用）。

    为什么要单独数：``page_of`` 在 total 缺省时会返回 ``null``，
    前端草稿卡直接把 ``null`` 渲染出来 —— 2026-09-21 修。
    """
    with session_scope() as db:
        stmt = _draft_stmt(user_id=user_id, case_id=case_id, task_id=task_id,
                           doc_type=doc_type, keyword=keyword)
        return int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one() or 0)


def list_drafts(
    *,
    user_id: int | None = None,
    case_id: str = "",
    task_id: str = "",
    doc_type: str = "",
    keyword: str = "",
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 50), 200))
    stmt = _draft_stmt(user_id=user_id, case_id=case_id, task_id=task_id,
                       doc_type=doc_type, keyword=keyword)
    stmt = stmt.order_by(DraftRecord.updated_at.desc()).limit(limit).offset(max(0, int(offset)))
    with session_scope() as db:
        return [draft_to_dict(d) for d in db.execute(stmt).scalars().all()]


def delete_draft(draft_id: str) -> dict[str, Any]:
    with session_scope() as db:
        d = db.execute(
            select(DraftRecord).where(DraftRecord.draft_id == draft_id)
        ).scalar_one_or_none()
        if d is None:
            return {"ok": False, "reason": "草稿不存在"}
        info = draft_to_dict(d)
        # 版本快照跟着草稿一起删。留着的话，草稿列表的 `total` 会越来越多，
        # 而历史版本永远挂在一条不存在的草稿上、谁也看不到。
        db.execute(delete(DraftVersion).where(DraftVersion.draft_id == draft_id))
        db.delete(d)
    return {"ok": True, **info}


def mark_exported(draft_id: str, export_path: str) -> dict[str, Any]:
    with session_scope() as db:
        d = db.execute(
            select(DraftRecord).where(DraftRecord.draft_id == draft_id)
        ).scalar_one_or_none()
        if d is None:
            return {"ok": False, "reason": "草稿不存在"}
        d.exported_path = export_path
        db.flush()
        return {"ok": True, **draft_to_dict(d)}


def _snapshot(db: Any, d: DraftRecord, *, source: str, note: str = "") -> None:
    """给草稿的**当前**状态存一份快照。调用方负责已在事务里、且 ``d`` 已 flush。

    按 ``(draft_id, version)`` **幂等**：同一版重复调用只留一条。这样调用方
    不必自己判断"这版是不是已经存过"，也顺手挡住了 save_from_state 那条
    会重复触发路径带来的重复快照。
    """
    version = int(d.version or 1)
    exists = db.execute(
        select(DraftVersion.id).where(
            DraftVersion.draft_id == d.draft_id, DraftVersion.version == version)
    ).first()
    if exists:
        return
    db.add(DraftVersion(
        draft_id=d.draft_id, version=version, source=source, note=(note or "")[:256],
        content=d.content or "", content_html=d.content_html or "",
        fills=dict(d.fills or {}), template_id=d.template_id or "",
    ))


def _version_to_dict(v: DraftVersion, *, with_content: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": v.id,
        "draft_id": v.draft_id,
        "version": int(v.version or 1),
        "source": v.source or "manual",
        "note": v.note or "",
        "template_id": v.template_id or "",
        "content_chars": len(v.content or ""),
        "created_at": v.created_at.isoformat(timespec="seconds") if v.created_at else "",
    }
    if with_content:
        out["content"] = v.content or ""
        out["content_html"] = v.content_html or ""
        out["fills"] = v.fills or {}
    return out


def list_versions(draft_id: str) -> list[dict[str, Any]]:
    """草稿的历史版本，新的在前。"""
    with session_scope() as db:
        rows = db.execute(
            select(DraftVersion).where(DraftVersion.draft_id == draft_id)
            .order_by(DraftVersion.version.desc(), DraftVersion.id.desc())
        ).scalars().all()
        return [_version_to_dict(v) for v in rows]


def restore_version(draft_id: str, version_id: int) -> dict[str, Any]:
    """把草稿恢复到某个历史版本。**恢复本身也是一个新版本，不擦掉中间的历史。**

    做法：读出旧版内容写回草稿、版本号继续自增、再给"恢复后"的状态拍一张
    快照。这样"恢复到 v1 又后悔了"还能再回到 v3 —— 如果用回滚（把 version
    减回去、删掉中间版本）就再也回不来了，而"恢复错了想撤回"是常规操作。
    """
    with session_scope() as db:
        d = db.execute(
            select(DraftRecord).where(DraftRecord.draft_id == draft_id)
        ).scalar_one_or_none()
        if d is None:
            return {"ok": False, "reason": f"草稿不存在：{draft_id}"}
        v = db.execute(
            select(DraftVersion).where(
                DraftVersion.id == version_id, DraftVersion.draft_id == draft_id)
        ).scalar_one_or_none()
        if v is None:
            return {"ok": False, "reason": f"版本不存在：{version_id}"}

        restored_from = int(v.version or 1)
        d.content = v.content or ""
        d.content_html = v.content_html or ""
        d.fills = dict(v.fills or {})
        if v.template_id:
            d.template_id = v.template_id
        d.version = int(d.version or 1) + 1
        _snapshot(db, d, source="manual", note=f"恢复到 v{restored_from}")
        out = draft_to_dict(d, with_content=True)
        out["ok"] = True
        out["restored_from"] = restored_from
        return out


def save_from_state(task_id: str, state: dict[str, Any], *,
                    user_id: int | None = None) -> dict[str, Any]:
    """把一次任务产出的文书存成草稿。

    ``revision_draft``（合同修改对照稿）优先于 ``draft``：
    合同审查任务的交付物是对照稿，``draft`` 字段在合同审查路径下本来就是空的
    （该路径刻意不含 DraftNode，见 ``router.INTENT_ROUTES`` 的说明）。
    """
    body = (state.get("revision_draft") or "").strip() or (state.get("draft") or "").strip()
    if not body:
        return {"saved": False, "reason": "本次任务未产出文书正文"}

    doc_type = str(state.get("draft_type") or "").strip() or (
        "revision" if state.get("revision_draft") else "other"
    )
    if doc_type not in DOC_TYPES:
        doc_type = "other"

    task_id = str(task_id or state.get("task_id") or "")
    # 同一任务重复入库时更新而不是新建，避免重跑留下两份草稿
    existing = next(
        (d for d in list_drafts(task_id=task_id, limit=1) if d.get("task_id") == task_id),
        None,
    ) if task_id else None
    if existing:
        out = save_draft(existing["draft_id"], content=body, user_id=user_id,
                         citations=list(state.get("citations") or []))
        return {"saved": True, "draft_id": existing["draft_id"],
                "version": out.get("version"), "updated": True}

    out = create_draft(
        content=body, doc_type=doc_type,
        title=(state.get("query") or "")[:120],
        case_id=str(state.get("case_id") or ""), task_id=task_id,
        user_id=user_id, citations=list(state.get("citations") or []),
    )
    return {"saved": True, "draft_id": out["draft_id"], "version": 1, "updated": False}


def stats() -> dict[str, Any]:
    with session_scope() as db:
        total = int(db.execute(select(func.count()).select_from(DraftRecord)).scalar_one() or 0)
        exported = int(db.execute(
            select(func.count()).select_from(DraftRecord)
            .where(DraftRecord.exported_path != "")
        ).scalar_one() or 0)
        by_type = {str(t or ""): int(c or 0)
                   for t, c in db.execute(select(DraftRecord.doc_type, func.count())
                                          .group_by(DraftRecord.doc_type)).all()}
    return {"drafts": total, "exported": exported, "by_type": by_type,
            "export_rate": round(exported / total, 3) if total else 0.0}


def _auto_title(doc_type: str, content: str) -> str:
    """从正文首行取标题。

    **取首行而不是首句**：中文公文的标题就在第一行（"民事起诉状"），
    按句号切会把"原告：张三，男，1980 年出生。"当成标题。
    """
    for line in (content or "").splitlines():
        line = line.strip()
        if 2 <= len(line) <= 60:
            return line
    return {
        "complaint": "民事起诉状", "defense": "民事答辩状", "opinion": "代理意见书",
        "arbitration_application": "劳动仲裁申请书", "appeal": "民事上诉状",
        "legal_opinion": "法律意见书",
        "revision": "合同修改对照稿", "contract": "合同文本",
    }.get(doc_type, "未命名文书")


__all__ = [
    "DOC_TYPES",
    "DraftError",
    "create_draft",
    "delete_draft",
    "draft_to_dict",
    "export_path",
    "get_draft",
    "list_drafts",
    "mark_exported",
    "save_draft",
    "save_from_state",
    "stats",
]
