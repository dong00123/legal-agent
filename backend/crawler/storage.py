"""存储层：MySQL 双去重写入 + 单日配额 + 断点游标。

**双去重**（``doc_id`` + 正文 ``content_hash``）解决的是两类不同的重复，
只做其中一种都会漏：

- 只按 ``doc_id``：同一篇正文转载到另一入口时 doc_id 不同，会各存一份；
- 只按正文哈希：同一篇被修订过（全角/半角、页眉水印混进正文）时哈希不同，
  会被当成新文档，同一次内容变更就多出一份"孪生"记录。

两重都做，才是"该存的存、该跳的跳"。

**写入顺序也是硬约束**：先按 ``doc_id`` 查、再按哈希查、最后才插入。
反过来（先插再查哈希）在唯一键冲突时会抛 IntegrityError，把一次正常的
"重复抓取"报成一次失败。
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import date
from typing import Any

from sqlalchemy import func, select, update

from backend.app.config import settings
from backend.app.db.models.crawl import CrawlCursor, CrawlDoc, CrawlQuota
from backend.app.db.session import session_scope
from backend.crawler.models import (
    SAVE_CREATED,
    SAVE_DUPLICATE,
    SAVE_HASH_DUPLICATE,
    SAVE_UPDATED,
    ParsedDoc,
    SaveOutcome,
)

logger = logging.getLogger("legal_agent.crawler.storage")

# 正文归一化：空白折叠。折叠是去重的前提 —— 同一篇正文两次抓取，
# 页面上多一个换行/多几个空格就会让哈希不同，去重直接失效。
_WS_RE = re.compile(r"[ \t\u3000]+")
_BLANK_LINES_RE = re.compile(r"\n{2,}")


def normalize_body(text: str) -> str:
    """正文归一化：统一换行 + 折叠行内空白 + 压缩空行 + 去首尾空白。"""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    t = _WS_RE.sub(" ", t)
    t = _BLANK_LINES_RE.sub("\n", t)
    return t.strip()


def content_hash(text: str) -> str:
    """正文 sha256。基于**归一化后**的正文，否则去重形同虚设。"""
    return hashlib.sha256(normalize_body(text).encode("utf-8")).hexdigest()


def url_digest(url: str) -> str:
    """URL 摘要，用作没有案号时的 doc_id 主体。"""
    return hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:16]


def build_doc_id(source: str, *, case_no: str = "", url: str = "", fallback: str = "") -> str:
    """构造来源级唯一键 ``{source}:{主体}``。

    主体优先级：案号 → URL 摘要 → 调用方给的 fallback。
    给了案号却用 URL 做键，会把"同一篇的镜像页"存成两条 —— 这正是双去重里
    哈希那一重本该兜住、却因为 doc_id 已经不同而**先被第一重放过**的情形。
    """
    key = (case_no or "").strip() or url_digest(url) or (fallback or "").strip()
    if not key:
        key = "unknown"
    return f"{(source or 'unknown').strip()}:{key}"


def _today() -> str:
    return date.today().isoformat()


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- 写入
def save_doc(doc: ParsedDoc) -> SaveOutcome:
    """双去重写入。返回本次的处置结果（created/updated/duplicate/hash_duplicate）。"""
    body = normalize_body(doc.body)
    h = doc.content_hash or (content_hash(body) if body else "")
    now = _now_iso()

    with session_scope() as db:
        row = db.execute(
            select(CrawlDoc).where(CrawlDoc.doc_id == doc.doc_id)
        ).scalar_one_or_none()

        if row is not None:
            row.seen_count = int(row.seen_count or 0) + 1
            row.fetched_at = now
            if row.content_hash and h and row.content_hash == h:
                # 完全一致：只更新"最近见到的时间"，正文与字段都不动。
                return SaveOutcome(SAVE_DUPLICATE, doc.doc_id, h, "doc_id 命中且正文未变")
            row.content_hash = h
            row.body = body
            row.body_len = len(body)
            _apply_fields(row, doc)
            return SaveOutcome(SAVE_UPDATED, doc.doc_id, h, "doc_id 命中，正文有修订")

        # 第二重：正文哈希。空正文不参与 —— 否则所有解析失败的页面会互相"去重"，
        # 只剩一条，把"到底抓到了几页空页"这个信号抹掉。
        if h:
            same = db.execute(
                select(CrawlDoc).where(CrawlDoc.content_hash == h).limit(1)
            ).scalar_one_or_none()
            if same is not None:
                logger.info("正文哈希命中已存在文档 %s，跳过 %s", same.doc_id, doc.doc_id)
                return SaveOutcome(
                    SAVE_HASH_DUPLICATE, doc.doc_id, h, f"正文与 {same.doc_id} 相同"
                )

        new = CrawlDoc(
            doc_id=doc.doc_id,
            content_hash=h,
            body_len=len(body),
            source=doc.source,
            url=doc.url,
            title=doc.title,
            doc_type=doc.doc_type,
            case_no=doc.case_no,
            court=doc.court,
            judge_date=doc.judge_date,
            body=body,
            extra=dict(doc.extra or {}),
            fetched_at=now,
            seen_count=1,
        )
        _apply_fields(new, doc, overwrite_extra=True)
        db.add(new)
        return SaveOutcome(SAVE_CREATED, doc.doc_id, h, "新入库")


def _apply_fields(row: CrawlDoc, doc: ParsedDoc, *, overwrite_extra: bool = False) -> None:
    """把解析结果写进 ORM 行。

    只在新值非空时覆盖：解析层取不到某个字段时返回空串，若直接覆盖，
    会把"上一次抓到、这一次没抓到"的好数据抹掉 —— 这正是"数据越采越少"的成因。
    """
    for attr, value in (
        ("url", doc.url),
        ("title", doc.title),
        ("doc_type", doc.doc_type),
        ("case_no", doc.case_no),
        ("court", doc.court),
        ("judge_date", doc.judge_date),
    ):
        if value:
            setattr(row, attr, value)

    extra = dict(doc.extra or {})
    extra["field_sources"] = dict(doc.fields or {})
    if overwrite_extra:
        row.extra = extra
    else:
        merged = dict(row.extra or {})
        merged.update(extra)
        row.extra = merged


# --------------------------------------------------------------------------- 查询
def get_doc(doc_id: str) -> dict[str, Any] | None:
    with session_scope() as db:
        row = db.execute(select(CrawlDoc).where(CrawlDoc.doc_id == doc_id)).scalar_one_or_none()
        return _doc_to_dict(row) if row is not None else None


def count_docs(source: str = "") -> int:
    with session_scope() as db:
        stmt = select(func.count(CrawlDoc.id))
        if source:
            stmt = stmt.where(CrawlDoc.source == source)
        return int(db.execute(stmt).scalar() or 0)


def _doc_to_dict(row: CrawlDoc) -> dict[str, Any]:
    return {
        "doc_id": row.doc_id,
        "source": row.source,
        "url": row.url,
        "title": row.title,
        "doc_type": row.doc_type,
        "case_no": row.case_no,
        "court": row.court,
        "judge_date": row.judge_date,
        "content_hash": row.content_hash,
        "body_len": row.body_len,
        "seen_count": row.seen_count,
        "fetched_at": row.fetched_at,
        "extra": dict(row.extra or {}),
    }


# --------------------------------------------------------------------------- 单日配额
def _ensure_quota_row(source: str, day: str, limit: int) -> None:
    """确保配额行存在（并发下允许失败，交由随后的条件 UPDATE 兜底）。

    ``limit`` 每次都用**当前配置值**刷新：改了配额却要等次日才生效，
    会让人以为"配置没起作用"，而排查方向会落到配额逻辑本身上。
    """
    try:
        with session_scope() as db:
            row = db.execute(
                select(CrawlQuota).where(
                    CrawlQuota.source == source, CrawlQuota.quota_date == day
                )
            ).scalar_one_or_none()
            if row is None:
                db.add(CrawlQuota(source=source, quota_date=day, used=0, limit=limit))
            elif int(row.limit or 0) != int(limit or 0):
                row.limit = int(limit or 0)
    except Exception as exc:  # noqa: BLE001 - 并发插入撞唯一键属正常
        logger.debug("配额行创建竞争（忽略）：%s vs %s：%s", source, day, exc)


def try_consume_quota(
    source: str, *, limit: int | None = None, day: str | None = None, n: int = 1
) -> bool:
    """尝试消耗 ``n`` 个配额。返回是否成功。

    **靠一条带条件的 UPDATE 裁决**（``used + n <= limit``，rowcount 为准），
    而不是"先查后改"：多 worker 并发时后者会让两边都认为自己还有额度，
    实际抓取量突破上限 —— 这类超发只能从对方站点的日志里发现。
    ``limit <= 0`` 表示不限，此时无条件累加。
    """
    day = day or _today()
    lim = int(settings.CRAWL_DAILY_QUOTA if limit is None else limit)
    _ensure_quota_row(source, day, lim)

    with session_scope() as db:
        stmt = update(CrawlQuota).where(
            CrawlQuota.source == source, CrawlQuota.quota_date == day
        )
        if lim > 0:
            stmt = stmt.where(CrawlQuota.used + n <= lim)
        stmt = stmt.values(used=CrawlQuota.used + n)
        result = db.execute(stmt)
        return int(result.rowcount or 0) == 1


def quota_status(source: str, *, limit: int | None = None, day: str | None = None) -> dict[str, Any]:
    day = day or _today()
    lim = int(settings.CRAWL_DAILY_QUOTA if limit is None else limit)
    with session_scope() as db:
        row = db.execute(
            select(CrawlQuota).where(
                CrawlQuota.source == source, CrawlQuota.quota_date == day
            )
        ).scalar_one_or_none()
        used = int(row.used or 0) if row is not None else 0
        stored_limit = int(row.limit or 0) if row is not None else lim
    effective = lim or stored_limit
    return {
        "source": source,
        "date": day,
        "used": used,
        "limit": effective,
        "remaining": (None if effective <= 0 else max(effective - used, 0)),
        "unlimited": effective <= 0,
    }


def release_quota(source: str, *, day: str | None = None, n: int = 1) -> None:
    """归还配额（抓取最终失败时调用）。

    不归还的后果：一次网络抖动就把额度永久吃掉一部分，长跑下来
    "明明没抓到几条，配额却满了" —— 这种账对不上会让人不再信任配额。
    """
    day = day or _today()
    with session_scope() as db:
        stmt = (
            update(CrawlQuota)
            .where(CrawlQuota.source == source, CrawlQuota.quota_date == day)
            .values(used=CrawlQuota.used - n)
        )
        db.execute(stmt.execution_options(synchronize_session=False))
        row = db.execute(
            select(CrawlQuota).where(
                CrawlQuota.source == source, CrawlQuota.quota_date == day
            )
        ).scalar_one_or_none()
        if row is not None and int(row.used or 0) < 0:
            row.used = 0


# --------------------------------------------------------------------------- 断点游标
def save_cursor(
    source: str,
    cursor_key: str,
    *,
    status: str | None = None,
    payload: dict[str, Any] | None = None,
    note: str | None = None,
    finished: bool = False,
) -> dict[str, Any]:
    """落断点（upsert）。``payload`` 整体替换而非合并 —— 合并会让上一轮的键残留，
    续跑时读到过期的页码。"""
    now = _now_iso()
    with session_scope() as db:
        row = db.execute(
            select(CrawlCursor).where(
                CrawlCursor.source == source, CrawlCursor.cursor_key == cursor_key
            )
        ).scalar_one_or_none()
        if row is None:
            row = CrawlCursor(source=source, cursor_key=cursor_key)
            db.add(row)
        if status is not None:
            row.status = status
        if payload is not None:
            row.payload = dict(payload)
        if note is not None:
            row.note = note[:512]
        if finished:
            row.finished_at = now
        db.flush()
        return {
            "source": row.source,
            "cursor_key": row.cursor_key,
            "status": row.status,
            "payload": dict(row.payload or {}),
            "note": row.note,
        }


def load_cursor(source: str, cursor_key: str) -> dict[str, Any]:
    """读断点。**不存在时返回空结构而不是 None** —— 调用方据此当成"从头开始"，
    不需要为"首次运行"写一条分支（分支越少越不容易漏）。"""
    with session_scope() as db:
        row = db.execute(
            select(CrawlCursor).where(
                CrawlCursor.source == source, CrawlCursor.cursor_key == cursor_key
            )
        ).scalar_one_or_none()
        if row is None:
            return {"source": source, "cursor_key": cursor_key, "status": "", "payload": {}, "note": ""}
        return {
            "source": row.source,
            "cursor_key": row.cursor_key,
            "status": row.status,
            "payload": dict(row.payload or {}),
            "note": row.note,
            "finished_at": row.finished_at,
        }


def list_cursors(source: str = "") -> list[dict[str, Any]]:
    with session_scope() as db:
        stmt = select(CrawlCursor)
        if source:
            stmt = stmt.where(CrawlCursor.source == source)
        rows = db.execute(stmt).scalars().all()
        return [
            {
                "source": r.source,
                "cursor_key": r.cursor_key,
                "status": r.status,
                "payload": dict(r.payload or {}),
                "note": r.note,
            }
            for r in rows
        ]
