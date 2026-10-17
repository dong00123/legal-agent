"""知识库、法规库、更新包与系统配置服务（方案第十二章管理后台）。

这四张表此前都只有建表语句、没有写入路径：``la_knowledge_doc`` 只在入库脚本里
被 ``_upsert_*`` 写过，``la_update_pack`` / ``la_system_config`` 全项目零写入。
后果是管理后台的「知识库管理」「法规库版本发布与回滚」「系统配置」三页
没有任何数据来源 —— 不是页面没写，是背后没有业务层。

几个刻意的设计：

1. **版本是知识库的核心字段，不是备注。**
   方案 4.x 要求"知识库引用必须标注指引版本"。同一份《合同审查清单》
   改版后旧版仍在被引用，律师依据的可能是已经作废的审查口径，
   而输出里只写"依据《合同审查清单》"——他无从分辨。
   所以 ``publish`` 不覆盖旧版，而是**新建版本 + 把旧版置为 archived**，
   保证历史上任何一次引用都能回指到当时生效的那一版。

2. **配置读取带类型转换，且永远给出"当前生效值"。**
   ``la_system_config`` 只是**覆盖层**：没被管理员改过的键，生效值是
   ``Settings`` 里的值。因此 ``effective()`` 必须同时返回两样东西 ——
   覆盖值和生效值。只返回覆盖值的话，前端看到的是空表，
   而系统其实跑在一堆默认值上，看起来像"配置没生效"。

3. **法规库状态只允许四个取值。** 方案要求标注"现行有效 / 已修订 / 已废止"，
   法条效力写错比找不到法条更危险，所以这里不接受自由文本。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from backend.app.config import settings
from backend.app.db.models import KnowledgeDoc, StatuteDoc, SystemConfig, UpdatePack
from backend.app.db.session import session_scope

logger = logging.getLogger("legal_agent.services.knowledge")

DOC_TYPES = ("guideline", "checklist", "template", "other")
DOC_STATUS = ("active", "draft", "archived")
# 法条效力状态。与 ``agents.state.StatuteStatus`` 保持一致 —— 两处不一致会
# 导致入库标「已废止」而检索显示「未知」。
STATUTE_STATUS = ("现行有效", "已修订", "已废止", "尚未生效", "未知")
VALUE_TYPES = ("str", "int", "float", "bool", "json")
CONFIG_GROUPS = ("model", "agent", "degrade", "rag", "system")


class KnowledgeError(ValueError):
    """业务校验失败（接口层转 400）。"""


# --------------------------------------------------------------------------- 知识库
def doc_to_dict(d: KnowledgeDoc, *, with_content: bool = False,
                with_path: bool = False) -> dict[str, Any]:
    """知识库文档的对外表示。

    ``stored_path`` 默认**不返回**，与 ``case_service.material_to_dict`` 同一条理由：
    它是服务端文件系统的绝对路径，内网路径会泄露目录结构。管理后台列表里
    显示"XX.pdf"就够了，不需要知道它落在哪个盘。服务端自己要用时显式开 ``with_path``
    （重建索引、重新解析都要按路径去读文件）。
    """
    out: dict[str, Any] = {
        "id": d.id,
        "title": d.title or "",
        "doc_type": d.doc_type or "",
        "category": d.category or "",
        "version": d.version or "",
        "effective_date": d.effective_date or "",
        "status": d.status or "",
        "indexed": bool(d.indexed),
        "content_chars": len(d.content or ""),
        "uploaded_by": d.uploaded_by,
        "updated_at": d.updated_at.isoformat(timespec="seconds") if d.updated_at else "",
    }
    if with_content:
        out["content"] = d.content or ""
    if with_path:
        out["stored_path"] = d.stored_path or ""
    return out


def list_docs(*, keyword: str = "", doc_type: str = "", category: str = "",
              status: str = "", limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 50), 200))
    stmt = select(KnowledgeDoc)
    if keyword:
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(or_(KnowledgeDoc.title.like(like), KnowledgeDoc.category.like(like)))
    if doc_type:
        stmt = stmt.where(KnowledgeDoc.doc_type == doc_type)
    if category:
        stmt = stmt.where(KnowledgeDoc.category == category)
    if status:
        stmt = stmt.where(KnowledgeDoc.status == status)
    stmt = stmt.order_by(KnowledgeDoc.updated_at.desc()).limit(limit).offset(max(0, int(offset)))
    with session_scope() as db:
        return [doc_to_dict(d) for d in db.execute(stmt).scalars().all()]


def get_doc(doc_id: int, *, with_content: bool = True) -> dict[str, Any] | None:
    with session_scope() as db:
        d = db.get(KnowledgeDoc, doc_id)
        return doc_to_dict(d, with_content=with_content) if d else None


def upsert_doc(
    *,
    title: str,
    doc_id: int | None = None,
    doc_type: str = "guideline",
    category: str = "",
    version: str = "v1.0",
    effective_date: str = "",
    status: str = "draft",
    content: str = "",
    stored_path: str = "",
    uploaded_by: int | None = None,
) -> dict[str, Any]:
    """新建或更新知识库文档。

    身份判定分两步，**先按 id、没有再按（标题 + 版本）**：

    - 传了 ``doc_id`` → 直接更新那一行；
    - 没传 ``doc_id`` → 先查有没有同「标题 + 版本」的行，有则更新、无则新建。

    第二步不是可有可无的。名字里的 "upsert" 就该是 upsert：不查重的话，
    同一份《合同审查清单》v1.0 被导入两次就变成两行，界面上看不出哪一行是新的，
    而引用回指时随机命中其中一行 —— 版本控制反而失效。

    ``status="active"`` 时**把同类同名的其它版本置为 archived**（见模块 docstring 第 1 条）。
    """
    title = (title or "").strip()
    if not title:
        raise KnowledgeError("标题不能为空")
    if doc_type not in DOC_TYPES:
        raise KnowledgeError(f"文档类型只能是 {' / '.join(DOC_TYPES)}")
    if status not in DOC_STATUS:
        raise KnowledgeError(f"状态只能是 {' / '.join(DOC_STATUS)}")
    version = (version or "v1.0").strip()

    with session_scope() as db:
        d = db.get(KnowledgeDoc, doc_id) if doc_id else None
        if d is None and doc_id is None:
            d = db.execute(
                select(KnowledgeDoc).where(KnowledgeDoc.title == title,
                                           KnowledgeDoc.version == version)
            ).scalar_one_or_none()

        superseded: list[str] = []
        if d is None:
            d = KnowledgeDoc(title=title)
            db.add(d)
        else:
            # 版本升为 active 时，登记被替代的其它生效版本
            if status == "active" and d.status != "active":
                old = db.execute(
                    select(KnowledgeDoc).where(
                        KnowledgeDoc.title == title,
                        KnowledgeDoc.id != d.id,
                        KnowledgeDoc.status == "active",
                    )
                ).scalars().all()
                for o in old:
                    o.status = "archived"
                    superseded.append(o.version or "")
                if superseded:
                    logger.info("知识库《%s》新版本 %s 生效，旧版本 %s 已归档",
                                title, version, "、".join(superseded))
            else:
                # 同一版本被重复导入：登记出来，便于发现"导入脚本跑重了"
                old_same = db.execute(
                    select(KnowledgeDoc).where(
                        KnowledgeDoc.title == title,
                        KnowledgeDoc.version == version,
                        KnowledgeDoc.id != d.id,
                    )
                ).scalars().all()
                for o in old_same:
                    logger.warning("知识库《%s》%s 存在重复行（id=%s），已归档，"
                                   "请检查导入脚本是否重复执行", title, version, o.id)
                    o.status = "archived"
                    superseded.append(f"{o.version or ''}(重复行 id={o.id})")

        d.title = title
        d.doc_type = doc_type
        d.category = category or ""
        d.version = version
        d.effective_date = effective_date or ""
        d.status = status
        if content:
            d.content = content
        if stored_path:
            d.stored_path = stored_path
        if uploaded_by is not None:
            d.uploaded_by = uploaded_by
        # 正文变了就要重新入库；只改元数据则保留原有 indexed 状态
        if content:
            d.indexed = False
        db.flush()
        out = doc_to_dict(d)
        out["superseded_versions"] = superseded
        return out


def index_doc(doc_id: int, *, indexed: bool = True) -> dict[str, Any]:
    """标记知识库文档是否已写入检索索引（入库管线跑完后回写）。"""
    with session_scope() as db:
        d = db.get(KnowledgeDoc, doc_id)
        if d is None:
            return {"ok": False, "reason": "文档不存在"}
        d.indexed = bool(indexed)
        db.flush()
        return {"ok": True, **doc_to_dict(d)}


def delete_doc(doc_id: int) -> dict[str, Any]:
    with session_scope() as db:
        d = db.get(KnowledgeDoc, doc_id)
        if d is None:
            return {"ok": False, "reason": "文档不存在"}
        info = doc_to_dict(d)
        db.delete(d)
    return {"ok": True, **info}


def categories() -> list[str]:
    with session_scope() as db:
        rows = db.execute(
            select(KnowledgeDoc.category).distinct().order_by(KnowledgeDoc.category)
        ).scalars().all()
        return [str(c) for c in rows if c]


# --------------------------------------------------------------------------- 法规库
def statute_to_dict(s: StatuteDoc) -> dict[str, Any]:
    return {
        "id": s.id,
        "doc_name": s.doc_name or "",
        "doc_no": s.doc_no or "",
        "category": s.category or "",
        "level": s.level or "",
        "publisher": s.publisher or "",
        "publish_date": s.publish_date or "",
        "effective_date": s.effective_date or "",
        "repealed_date": s.repealed_date or "",
        "status": s.status or "",
        "version": s.version or "",
        "article_count": int(s.article_count or 0),
        "indexed": bool(s.indexed),
        "update_pack": s.update_pack or "",
        "updated_at": s.updated_at.isoformat(timespec="seconds") if s.updated_at else "",
    }


def list_statutes(*, keyword: str = "", status: str = "", category: str = "",
                  level: str = "", limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 50), 200))
    stmt = select(StatuteDoc)
    if keyword:
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(or_(StatuteDoc.doc_name.like(like), StatuteDoc.doc_no.like(like)))
    if status:
        stmt = stmt.where(StatuteDoc.status == status)
    if category:
        stmt = stmt.where(StatuteDoc.category == category)
    if level:
        stmt = stmt.where(StatuteDoc.level == level)
    stmt = stmt.order_by(StatuteDoc.doc_name).limit(limit).offset(max(0, int(offset)))
    with session_scope() as db:
        return [statute_to_dict(s) for s in db.execute(stmt).scalars().all()]


def get_statute(doc_name: str) -> dict[str, Any] | None:
    with session_scope() as db:
        s = db.execute(
            select(StatuteDoc).where(StatuteDoc.doc_name == doc_name)
        ).scalar_one_or_none()
        return statute_to_dict(s) if s else None


def set_statute_status(doc_name: str, status: str, *,
                       repealed_date: str = "", version: str = "") -> dict[str, Any]:
    """改法条效力状态（法规库版本发布/回滚的核心动作）。"""
    if status not in STATUTE_STATUS:
        raise KnowledgeError(f"效力状态只能是 {' / '.join(STATUTE_STATUS)}")
    with session_scope() as db:
        s = db.execute(
            select(StatuteDoc).where(StatuteDoc.doc_name == doc_name)
        ).scalar_one_or_none()
        if s is None:
            return {"ok": False, "reason": f"法规库中无此法规：{doc_name}"}
        s.status = status
        if repealed_date:
            s.repealed_date = repealed_date
        if version:
            s.version = version
        db.flush()
        logger.info("法规效力状态变更：%s -> %s", doc_name, status)
        return {"ok": True, **statute_to_dict(s)}


def statute_stats() -> dict[str, Any]:
    with session_scope() as db:
        total = int(db.execute(select(func.count()).select_from(StatuteDoc)).scalar_one() or 0)
        by_status = {str(s or ""): int(c or 0)
                     for s, c in db.execute(select(StatuteDoc.status, func.count())
                                            .group_by(StatuteDoc.status)).all()}
        articles = int(db.execute(select(func.sum(StatuteDoc.article_count))).scalar_one() or 0)
        indexed = int(db.execute(
            select(func.count()).select_from(StatuteDoc).where(StatuteDoc.indexed.is_(True))
        ).scalar_one() or 0)
        # 条文数为 0 的法规必须单独列出来。
        #
        # 它们登记为「现行有效」、在法规库列表里正常显示、可以点开，
        # 但**检索永远查不到它们** —— 因为索引里一条条文都没有。
        # 律师遇到这种情况分不清两件事："这部法规不适用" 与 "它压根没入库"，
        # 而后者会让人得出"没有相关规定"的结论，这比报错危险得多。
        # 本机实测就躺着 5 部（北京/天津/汕头/江苏 的知识产权条例等），
        # 而当时没有任何界面能看出它们与正常法规有何不同。
        empty = db.execute(
            select(StatuteDoc.doc_name, StatuteDoc.status, StatuteDoc.indexed)
            .where(StatuteDoc.article_count <= 0)
            .order_by(StatuteDoc.doc_name.asc())
            .limit(100)
        ).all()
    empty_docs = [
        {"doc_name": str(n or ""), "status": str(s or ""), "indexed": bool(i)}
        for n, s, i in empty
    ]
    return {
        "statutes": total,
        "articles": articles,
        "indexed": indexed,
        "by_status": by_status,
        "empty_docs": empty_docs,
        "empty_docs_note": ("这些法规已登记但没有条文，检索不会返回它们；"
                            "需重新入库（后台「法规库 → 重新入库」）或确认语料文件是否存在"),
    }


# --------------------------------------------------------------------------- 更新包
def pack_to_dict(p: UpdatePack, *, with_path: bool = False) -> dict[str, Any]:
    """更新包的对外表示。``stored_path`` 同样默认不外泄（同 ``doc_to_dict``）。

    ``rollback`` 需要按路径回滚某个版本的法规数据，但那条路径在服务端内部
    从数据库直接取（不经这个函数），所以关掉它不会影响回滚能力。
    """
    out: dict[str, Any] = {
        "id": p.id,
        "pack_id": p.pack_id or "",
        "filename": p.filename or "",
        "doc_count": int(p.doc_count or 0),
        "status": p.status or "",
        "detail": p.detail or "",
        "uploaded_by": p.uploaded_by,
        "created_at": p.created_at.isoformat(timespec="seconds") if p.created_at else "",
    }
    if with_path:
        out["stored_path"] = p.stored_path or ""
    return out


def list_packs(*, status: str = "", limit: int = 50) -> list[dict[str, Any]]:
    stmt = select(UpdatePack)
    if status:
        stmt = stmt.where(UpdatePack.status == status)
    stmt = stmt.order_by(UpdatePack.id.desc()).limit(max(1, min(int(limit or 50), 200)))
    with session_scope() as db:
        return [pack_to_dict(p) for p in db.execute(stmt).scalars().all()]


def record_pack(
    pack_id: str,
    *,
    filename: str = "",
    stored_path: str = "",
    doc_count: int = 0,
    status: str = "imported",
    detail: str = "",
    uploaded_by: int | None = None,
) -> dict[str, Any]:
    """登记一个法规更新包（导入完成后调用）。"""
    pack_id = (pack_id or "").strip()
    if not pack_id:
        raise KnowledgeError("pack_id 不能为空")
    try:
        with session_scope() as db:
            p = db.execute(
                select(UpdatePack).where(UpdatePack.pack_id == pack_id)
            ).scalar_one_or_none()
            if p is None:
                p = UpdatePack(pack_id=pack_id)
                db.add(p)
            p.filename = filename or p.filename or ""
            p.stored_path = stored_path or p.stored_path or ""
            p.doc_count = int(doc_count or 0)
            p.status = status or "imported"
            p.detail = (detail or "")[:4000]
            if uploaded_by is not None:
                p.uploaded_by = uploaded_by
            db.flush()
            return pack_to_dict(p)
    except IntegrityError as exc:
        raise KnowledgeError(f"更新包已登记：{pack_id}") from exc


def rollback_pack(pack_id: str) -> dict[str, Any]:
    """回滚一个更新包。

    **只改状态，不动法规数据。** 真正的回滚需要按包内清单反向恢复各法规的
    版本快照，而快照依赖导入时的原始文件（``stored_path``）。
    在文件不在或已被清理的情况下假装"回滚成功"是最坏的结果 ——
    所以这里的状态标为 ``rolled_back``，并在 detail 里写清边界。
    """
    with session_scope() as db:
        p = db.execute(select(UpdatePack).where(UpdatePack.pack_id == pack_id)).scalar_one_or_none()
        if p is None:
            return {"ok": False, "reason": f"更新包不存在：{pack_id}"}
        if p.status == "rolled_back":
            return {"ok": True, "pack_id": pack_id, "status": "rolled_back", "note": "已是回滚状态"}
        p.status = "rolled_back"
        p.detail = (p.detail + "\n[回滚] 已标记为回滚；法规数据未自动恢复，"
                    "需按包内清单人工核对或重新导入上一版更新包。").strip()
        db.flush()
        logger.warning("更新包 %s 已标记回滚（法规数据未自动恢复）", pack_id)
        return {"ok": True, **pack_to_dict(p)}


# --------------------------------------------------------------------------- 配置
def _coerce(value: Any, value_type: str) -> Any:
    """按声明类型转换。转换失败返回原字符串 —— **不要**静默变成默认值，
    否则管理员把 ``MAX_STEPS`` 填成 ``abc`` 会看到"已保存"而实际生效的
    还是 16，这是最难查的一类问题。"""
    text = "" if value is None else str(value)
    try:
        if value_type == "int":
            return int(float(text))
        if value_type == "float":
            return float(text)
        if value_type == "bool":
            return text.strip().lower() in ("1", "true", "yes", "on", "是")
        if value_type == "json":
            return json.loads(text)
    except (TypeError, ValueError):
        return text
    return text


def effective_config() -> list[dict[str, Any]]:
    """配置清单：**每个可调项都给出「当前生效值」与「代码默认值」**。

    只有被管理员改过的键才在 ``la_system_config`` 里有行；其余项的生效值来自
    ``Settings``。两个值都返回，前端才能区分"没配过"与"配成了默认值"。
    """
    from backend.app.services import config_service

    overrides = {c["key"]: c for c in list_configs()}
    out: list[dict[str, Any]] = []
    for key, meta in config_service.EDITABLE.items():
        default = getattr(settings, key, "")
        row = overrides.get(key)
        out.append({
            "key": key,
            "group": meta["group"],
            "description": meta["description"],
            "value_type": meta["type"],
            "default_value": default,
            "override_value": row["value"] if row else None,
            "effective_value": _coerce(row["value"], meta["type"]) if row else default,
            "overridden": row is not None,
            "updated_by": row.get("updated_by") if row else None,
            "updated_at": row.get("updated_at", "") if row else "",
        })
    return out


def config_to_dict(c: SystemConfig) -> dict[str, Any]:
    return {
        "id": c.id,
        "key": c.key,
        "value": c.value or "",
        "value_type": c.value_type or "str",
        "group": c.group or "",
        "description": c.description or "",
        "updated_by": c.updated_by,
        "updated_at": c.updated_at.isoformat(timespec="seconds") if c.updated_at else "",
    }


def list_configs(*, group: str = "") -> list[dict[str, Any]]:
    stmt = select(SystemConfig)
    if group:
        stmt = stmt.where(SystemConfig.group == group)
    stmt = stmt.order_by(SystemConfig.group, SystemConfig.key)
    with session_scope() as db:
        return [config_to_dict(c) for c in db.execute(stmt).scalars().all()]


def set_config(key: str, value: Any, *, value_type: str = "", group: str = "",
               description: str = "", updated_by: int | None = None) -> dict[str, Any]:
    """写一条配置覆盖值。"""
    from backend.app.services import config_service

    key = (key or "").strip().upper()
    if not key:
        raise KnowledgeError("配置键不能为空")
    if key not in config_service.EDITABLE:
        raise KnowledgeError(
            f"配置键不可运行时修改：{key}；可改项见管理后台「系统配置」（共 "
            f"{len(config_service.EDITABLE)} 项）"
        )
    meta = config_service.EDITABLE[key]
    vtype = value_type or meta["type"]
    if vtype not in VALUE_TYPES:
        raise KnowledgeError(f"值类型只能是 {' / '.join(VALUE_TYPES)}")
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)

    with session_scope() as db:
        c = db.execute(select(SystemConfig).where(SystemConfig.key == key)).scalar_one_or_none()
        if c is None:
            c = SystemConfig(key=key)
            db.add(c)
        c.value = "" if value is None else str(value)
        c.value_type = vtype
        c.group = group or meta["group"]
        c.description = description or meta["description"]
        if updated_by is not None:
            c.updated_by = updated_by
        db.flush()
        logger.info("系统配置已更新：%s = %s（%s）", key, c.value, vtype)
        return config_to_dict(c)


def reset_config(key: str) -> dict[str, Any]:
    """删除覆盖值，回到代码默认值。"""
    key = (key or "").strip().upper()
    with session_scope() as db:
        c = db.execute(select(SystemConfig).where(SystemConfig.key == key)).scalar_one_or_none()
        if c is None:
            return {"ok": True, "key": key, "note": "本就没有覆盖值"}
        db.delete(c)
    return {"ok": True, "key": key, "note": "已恢复为代码默认值"}


def stats() -> dict[str, Any]:
    with session_scope() as db:
        docs = int(db.execute(select(func.count()).select_from(KnowledgeDoc)).scalar_one() or 0)
        active_docs = int(db.execute(
            select(func.count()).select_from(KnowledgeDoc).where(KnowledgeDoc.status == "active")
        ).scalar_one() or 0)
        packs = int(db.execute(select(func.count()).select_from(UpdatePack)).scalar_one() or 0)
        overrides = int(db.execute(select(func.count()).select_from(SystemConfig)).scalar_one() or 0)
    return {"knowledge_docs": docs, "active_docs": active_docs,
            "update_packs": packs, "config_overrides": overrides}


__all__ = [
    "CONFIG_GROUPS",
    "DOC_STATUS",
    "DOC_TYPES",
    "KnowledgeError",
    "STATUTE_STATUS",
    "VALUE_TYPES",
    "categories",
    "config_to_dict",
    "delete_doc",
    "doc_to_dict",
    "effective_config",
    "get_doc",
    "get_statute",
    "index_doc",
    "list_configs",
    "list_docs",
    "list_packs",
    "list_statutes",
    "pack_to_dict",
    "record_pack",
    "reset_config",
    "rollback_pack",
    "set_config",
    "set_statute_status",
    "statute_stats",
    "statute_to_dict",
    "stats",
    "upsert_doc",
]
