"""案件档案与材料服务（方案第九章长期结构化记忆 + 第十四章数据隔离）。

本模块补上了一个此前**完全没有写入路径**的表：``la_case_material``。
它建了表、定义了字段（``stored_path`` / ``sha256`` / ``parsed`` / ``parsed_path``），
但全项目没有任何代码往里写过一行。后果是材料管理页永远空着 ——
而"材料"恰恰是律所办案的主线数据。

三条纪律：

1. **权限判定不在这里重复实现，也不设"空放行"的默认。**
   规则统一调 ``security.can_access_case``（写两份必然分叉，而分叉的那一份
   会变成越权入口）；主体缺省时**拒绝**而不是放行 —— 接口层漏传一次登录用户
   就是一次静默的全量越权，而且不会有任何报错。服务端内部调用显式传
   :data:`INTERNAL` 哨兵。
2. **材料落盘要留痕。** 每次读取（含 Agent 读取）都写审计 ——
   方案第十四章要求"材料访问全链路审计"。审计走 ``audit_writer``，
   它的落盘是真源，DB 只是可查询副本。
3. **文件名不直接当存储名。** 上传文件名可能带路径穿越（``../../x``）、
   重名、超长、非法字符。存储名用 ``{case_id}/{sha256前16}_{安全文件名}``，
   原文件名只在库里留作展示 —— 这样"同名不同内容"不会互相覆盖，
   而"同内容不同名"也不会重复占磁盘之外产生歧义。
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import func, or_, select

from backend.app.config import settings
from backend.app.db.models import Case, CaseMaterial
from backend.app.db.session import session_scope
from backend.app.security import can_access_case

logger = logging.getLogger("legal_agent.services.case")

CASE_TYPES = ("litigation", "contract", "consult")
CASE_TYPE_LABEL = {"litigation": "诉讼", "contract": "非诉/合同", "consult": "咨询"}
STAGES = ("intake", "evidence", "hearing", "judgment", "closed", "in_progress")
# Windows 与 POSIX 都不允许的字符，外加控制字符
_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
MAX_FILENAME_CHARS = 120


class CaseError(ValueError):
    """业务校验失败（接口层转 400）。"""


def assert_evidence_chain_allowed(case_type: str | None) -> None:
    """证据链只对**诉讼**案件开放（2026-09-29 用户点名：非诉/合同 与 咨询 不能传）。

    证据材料上传与证据链生成共用这一道判断。界面上那三块（证据材料 / 证据链 /
    充分度评估）也是一起隐藏的（见 ``web/app.js`` 的 ``evidenceBlocks``）——
    藏按钮是"别把做不到的事摆出来"，**这道判断才是规则本身**：
    直接打 API、或者开着一个改版前就加载好的旧页面，都从这里挡住。

    只读端点（GET 证据链 / GET 充分度评估）不拦：它们不产生新数据，
    拦了反而给将来别的调用方埋雷。
    """
    if (case_type or "") != "litigation":
        raise CaseError(
            "证据链只对诉讼案件开放，本案类型是「"
            + CASE_TYPE_LABEL.get(case_type or "", case_type or "未填")
            + "」"
        )


class CaseNotFound(LookupError):
    """案件不存在（接口层转 404）。"""


class CaseForbidden(PermissionError):
    """无权访问（接口层转 403）。"""


# --------------------------------------------------------------------------- 文件
def safe_filename(name: str) -> str:
    """把上传文件名压成安全形式。返回空串表示无法安全处理。"""
    base = Path(str(name or "")).name          # 先剥掉任何目录成分
    base = _UNSAFE_CHARS.sub("_", base).strip(" .")
    if not base:
        return ""
    if len(base) > MAX_FILENAME_CHARS:
        stem, dot, suffix = base.rpartition(".")
        keep = MAX_FILENAME_CHARS - len(suffix) - 1 if dot else MAX_FILENAME_CHARS
        base = f"{(stem or base)[:max(1, keep)]}{dot}{suffix}" if dot else base[:MAX_FILENAME_CHARS]
    return base


def sha256_of(path: str | Path, *, chunk: int = 1 << 20) -> str:
    """流式算哈希。整文件读进内存会在几百 MB 的卷宗上把内存打满。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def material_dir(case_id: str) -> Path:
    d = Path(settings.UPLOAD_DIR) / (case_id or "unfiled")
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------- 转换
def case_to_dict(case: Case, *, with_materials: bool = False) -> dict[str, Any]:
    d: dict[str, Any] = {
        "case_id": case.case_id,
        "title": case.title or "",
        "case_type": case.case_type or "",
        "cause_of_action": case.cause_of_action or "",
        "client_name": case.client_name or "",
        "opponent_name": case.opponent_name or "",
        "stage": case.stage or "",
        "status": case.status or "",
        "summary": case.summary or "",
        "dispute_issues": case.dispute_issues or [],
        "outcome": case.outcome or "",
        "tags": case.tags or [],
        "progress": case.progress or [],
        "owner_id": case.owner_id,
        "team_id": case.team_id,
        "created_at": case.created_at.isoformat(timespec="seconds") if case.created_at else "",
        "updated_at": case.updated_at.isoformat(timespec="seconds") if case.updated_at else "",
        # 证据链状态（2026-10-12）。加这两项是为了让**选案件的地方**能一眼看出
        # "这一件已经有链了" —— 否则律师要点进每一件才知道，几十件案件时就是几十次点击。
        #
        # ⚠️ 两项都取自**已有列/JSON**，不额外查库：
        #    ``evidence_chain`` 是 JSON 列（老行可能是 NULL，所以 ``or []`` 兜底；
        #    同 ``win_assessment`` 那条注释的道理），``evidence_chain_at`` 是普通列。
        #    **材料数不在这里给** —— 那要读 ``case.materials`` 关系，在列表里就是 N+1 次查询。
        "evidence_chain_at": case.evidence_chain_at or "",
        "chain_claims": len(case.evidence_chain or []),
    }
    if with_materials:
        d["materials"] = [material_to_dict(m) for m in (case.materials or [])]
    return d


def material_to_dict(m: CaseMaterial, *, with_path: bool = False) -> dict[str, Any]:
    """对外表示。

    ``stored_path`` 默认**不返回**：它是服务端文件系统路径，接口层没必要暴露
    （内网绝对路径会泄露目录结构）。但服务端自己需要它 —— 解析管线要知道去哪读、
    材料登记要判断"这份内容是否已存在"，所以留一个显式开关，
    而不是把路径默认带进每一个响应体。
    """
    out: dict[str, Any] = {
        "id": m.id,
        "case_id": m.case_id,
        "filename": m.filename or "",
        "doc_type": m.doc_type or "unknown",
        "size_bytes": int(m.size_bytes or 0),
        "sha256": m.sha256 or "",
        "parsed": bool(m.parsed),
        # ⚠️ **``parsed_path`` 不在这里返回**（2026-10-12 移除）。
        # 它是服务端的绝对路径，而本函数的返回值会**直接进 HTTP 响应体**
        # （``GET /cases/{id}/materials``、``GET /materials/{id}``、``/content``）。
        # 与 ``stored_path`` 是同一个坑、同一个理由 —— 当初只把 ``stored_path``
        # 藏起来是因为只想到了它，而 ``parsed_path`` 一直在往外漏。
        # 服务端自己要读它（``_material_text`` / 解析回写）走 ORM 属性，不经这里；
        # 前端只用到 ``parsed`` 这个布尔量。``with_path=True`` 才给 ``stored_path``。
        "uploaded_by": m.uploaded_by,
        "uploaded_at": m.created_at.isoformat(timespec="seconds") if m.created_at else "",
    }
    if with_path:
        out["stored_path"] = m.stored_path or ""
    return out


# --------------------------------------------------------------------------- 权限
# 内部调用令牌。服务端自己发起的读写（归档、材料登记回写）没有登录态。
# 用哨兵对象而不是 ``None``：``None`` 表示"接口层漏传了用户"，
# 那种情况必须**拒绝**而不是放行 —— 见 ``assert_access``。
INTERNAL = object()


def _load(db: Any, case_id: str) -> Case:
    case = db.execute(select(Case).where(Case.case_id == case_id)).scalar_one_or_none()
    if case is None:
        raise CaseNotFound(f"案件不存在：{case_id}")
    return case


def assert_access(viewer: Any, case: Case) -> None:
    """访问判定。**失败即拒绝**。

    ``viewer`` 三态：
    - :data:`INTERNAL` —— 服务端内部调用，放行；
    - ``None`` —— 接口层漏传登录用户，**抛错**。这里刻意不设"空就放行"的默认，
      因为那样任何一个接口忘了传 viewer 都会变成全量越权，
      而且不会有任何报错提示 —— 静默的越权比报错的拒绝危险得多；
    - ``CurrentUser`` —— 走 ``can_access_case``（管理员全通 / 本人 / 本团队）。
    """
    if viewer is INTERNAL:
        return
    if viewer is None:
        raise CaseForbidden("缺少访问主体：对外接口必须传入登录用户")
    if not can_access_case(viewer, case.owner_id, case.team_id):
        raise CaseForbidden(
            f"无权访问案件 {case.case_id}：仅限承办律师本人或其所属团队"
        )


# --------------------------------------------------------------------------- 案件
def list_cases(
    viewer: Any = None,
    *,
    keyword: str = "",
    case_type: str = "",
    status: str = "",
    stage: str = "",
    limit: int = 10,
    offset: int = 0,
    count_total: bool = False,
) -> list[dict[str, Any]] | dict[str, Any]:
    """案件列表。**非管理员一律在 SQL 上加隔离条件**，不是取回来再过滤。

    ``count_total=True`` 时返回 ``{"items": [...], "total": N}``（供页码计算）；
    默认返回纯 ``list``，旧调用方（统计、内部遍历）零改动。
    """
    limit = max(1, min(int(limit or 10), 200))
    if viewer is None:
        raise CaseForbidden("缺少访问主体：对外接口必须传入登录用户")

    stmt = select(Case)
    if viewer is not INTERNAL and not viewer.is_admin:
        conds = [Case.owner_id == viewer.id]
        if viewer.team_id is not None:
            conds.append(Case.team_id == viewer.team_id)
        stmt = stmt.where(or_(*conds))
    if keyword:
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(or_(Case.title.like(like), Case.case_id.like(like),
                              Case.cause_of_action.like(like), Case.client_name.like(like)))
    if case_type:
        stmt = stmt.where(Case.case_type == case_type)
    if status:
        stmt = stmt.where(Case.status == status)
    if stage:
        stmt = stmt.where(Case.stage == stage)
    base_stmt = stmt
    stmt = stmt.order_by(Case.updated_at.desc()).limit(limit).offset(max(0, int(offset)))
    with session_scope() as db:
        items = [case_to_dict(c) for c in db.execute(stmt).scalars().all()]
    if count_total:
        with session_scope() as db:
            total = int(db.execute(
                select(func.count()).select_from(base_stmt.subquery())
            ).scalar_one() or 0)
        return {"items": items, "total": total}
    return items


def get_case(case_id: str, viewer: Any = None, *,
             with_materials: bool = True) -> dict[str, Any]:
    with session_scope() as db:
        case = _load(db, case_id)
        assert_access(viewer, case)
        return case_to_dict(case, with_materials=with_materials)


def create_case(
    case_id: str,
    *,
    title: str = "",
    case_type: str = "litigation",
    cause_of_action: str = "",
    client_name: str = "",
    opponent_name: str = "",
    stage: str = "intake",
    owner_id: int | None = None,
    team_id: int | None = None,
    summary: str = "",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    case_id = (case_id or "").strip()
    if not case_id:
        raise CaseError("case_id 不能为空")
    if case_type and case_type not in CASE_TYPES:
        raise CaseError(f"案件类型只能是 {' / '.join(CASE_TYPES)}")
    with session_scope() as db:
        if db.execute(select(Case).where(Case.case_id == case_id)).scalar_one_or_none():
            raise CaseError(f"案件编号已存在：{case_id}")
        case = Case(
            case_id=case_id,
            title=(title or case_id)[:256],
            case_type=case_type or "litigation",
            cause_of_action=cause_of_action or "",
            client_name=client_name or "",
            opponent_name=opponent_name or "",
            stage=stage or "intake",
            status="active",
            owner_id=owner_id,
            team_id=team_id,
            summary=summary or "",
            tags=list(tags or []),
        )
        db.add(case)
        db.flush()
        logger.info("案件已创建：%s（%s）", case_id, case.title)
        return case_to_dict(case)


def update_case(case_id: str, viewer: Any = None, **fields: Any) -> dict[str, Any]:
    with session_scope() as db:
        case = _load(db, case_id)
        assert_access(viewer, case)
        for name in ("title", "case_type", "cause_of_action", "client_name",
                     "opponent_name", "stage", "status", "summary", "outcome"):
            if fields.get(name) is not None:
                setattr(case, name, str(fields[name]))
        if fields.get("dispute_issues") is not None:
            case.dispute_issues = list(fields["dispute_issues"])
        if fields.get("tags") is not None:
            case.tags = list(fields["tags"])
        db.flush()
        return case_to_dict(case)


def delete_case(case_id: str, viewer: Any = None) -> dict[str, Any]:
    """删除案件档案与其材料记录。

    **只删档案与材料记录，不删磁盘文件** —— 卷宗原文误删无法恢复，
    而档案记录删错了可以重建。需要清理磁盘时由管理员单独操作。
    """
    with session_scope() as db:
        case = _load(db, case_id)
        assert_access(viewer, case)
        n = db.execute(
            select(func.count()).select_from(CaseMaterial).where(CaseMaterial.case_id == case_id)
        ).scalar_one()
        db.query(CaseMaterial).filter(CaseMaterial.case_id == case_id).delete()
        db.delete(case)
        return {"ok": True, "case_id": case_id, "materials_removed": int(n or 0),
                "note": "磁盘上的卷宗原文未被删除"}


def add_progress(case_id: str, *, stage: str, note: str = "", operator: str = "",
                 task_id: str = "", result: str = "") -> dict[str, Any]:
    """追加一条办理进度。仅追加，不覆盖历史。"""
    from datetime import datetime

    with session_scope() as db:
        case = _load(db, case_id)
        entry = {
            "stage": stage, "note": note, "operator": operator,
            "task_id": task_id, "result": result,
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
        }
        case.progress = list(case.progress or []) + [entry]
        if stage in STAGES:
            case.stage = stage
        db.flush()
        return {"ok": True, "case_id": case_id, "progress": case.progress}


def stats(case_ids: Sequence[str] | None = None) -> dict[str, Any]:
    """案件统计。``case_ids`` 给定时只统计这批案件（律师端工作台用）。

    ⚠ **字段集必须与全所版一致**：工作台三张卡读的是 cases / materials，
    早先律师分支只返回 cases/by_status/by_type，于是「卷宗材料」永远是「—」。
    结果字段是接口契约，不能按分支裁剪。
    """
    scope = [] if case_ids is None else [Case.case_id.in_(list(case_ids))]
    mscope = [] if case_ids is None else [CaseMaterial.case_id.in_(list(case_ids))]
    with session_scope() as db:
        total = int(db.execute(
            select(func.count()).select_from(Case).where(*scope)
        ).scalar_one() or 0)
        by_type = {str(t or ""): int(c or 0)
                   for t, c in db.execute(select(Case.case_type, func.count())
                                          .where(*scope)
                                          .group_by(Case.case_type)).all()}
        by_status = {str(s or ""): int(c or 0)
                     for s, c in db.execute(select(Case.status, func.count())
                                            .where(*scope)
                                            .group_by(Case.status)).all()}
        by_stage = {str(s or ""): int(c or 0)
                    for s, c in db.execute(select(Case.stage, func.count())
                                           .where(*scope)
                                           .group_by(Case.stage)).all()}
        materials = int(db.execute(
            select(func.count()).select_from(CaseMaterial).where(*mscope)
        ).scalar_one() or 0)
    return {"cases": total, "by_type": by_type, "by_status": by_status,
            "by_stage": by_stage, "materials": materials}


# --------------------------------------------------------------------------- 材料
def register_material(
    case_id: str,
    source_path: str | Path,
    *,
    filename: str = "",
    doc_type: str = "unknown",
    uploaded_by: int | None = None,
    move: bool = False,
) -> dict[str, Any]:
    """把一个已落盘的文件登记为案件材料（并把文件搬进案件目录）。

    ``move=False`` 时复制而不是移动 —— 源文件可能是调用方传进来的临时文件，
    也可能是律所共享盘上的原件（移动会破坏原件的可访问性）。
    """
    with session_scope() as db:
        case = _load(db, case_id)

    src = Path(source_path)
    if not src.is_file():
        raise CaseError(f"源文件不存在：{src}")

    digest = sha256_of(src)
    display_name = safe_filename(filename or src.name)
    if not display_name:
        raise CaseError(f"文件名无法安全处理：{filename or src.name}")

    dest_dir = material_dir(case_id)
    dest = dest_dir / f"{digest[:16]}_{display_name}"
    if dest.resolve() != src.resolve():
        if dest.exists():
            # 同内容已存在：不重复占磁盘，但仍登记一条记录（可能是不同卷宗的同一份复印件）
            logger.info("材料内容已存在，复用：%s", dest.name)
        else:
            (shutil.move if move else shutil.copy2)(str(src), str(dest))

    with session_scope() as db:
        existing = db.execute(
            select(CaseMaterial).where(CaseMaterial.case_id == case_id,
                                       CaseMaterial.sha256 == digest)
        ).scalar_one_or_none()
        if existing is not None:
            existing.filename = display_name          # 展示名以最后一次为准
            db.flush()
            # with_path=True：调用方（解析管线）要知道去哪儿读这份材料
            return material_to_dict(existing, with_path=True)

        row = CaseMaterial(
            case_id=case_id, filename=display_name,
            stored_path=str(dest), doc_type=doc_type or "unknown",
            size_bytes=dest.stat().st_size if dest.exists() else src.stat().st_size,
            sha256=digest, parsed=False, uploaded_by=uploaded_by,
        )
        db.add(row)
        db.flush()
        out = material_to_dict(row, with_path=True)
    logger.info("案件 %s 新增材料：%s（%d 字节）", case_id, display_name, out["size_bytes"])
    return out


def _viewer_meta(viewer: Any) -> dict[str, Any] | None:
    """把访问主体转成审计用的字段。

    **不能写 ``if viewer is not None``** —— :data:`INTERNAL` 哨兵不是 ``None``，
    于是内部调用会一路走到 ``viewer.id`` 上并抛 ``AttributeError``。
    （这正是最初 ``list_materials`` 冒烟时报的错：报错点在审计调用那一行，
    看起来像审计坏了，实际是主体判定写松了。）
    """
    if viewer is None or viewer is INTERNAL:
        return None
    return {
        "user_id": getattr(viewer, "id", None),
        "username": getattr(viewer, "username", ""),
        "team_id": getattr(viewer, "team_id", None),
    }


def list_materials(case_id: str, viewer: Any = None) -> list[dict[str, Any]]:
    """列出材料。**读取即留痕**（方案第十四章材料访问审计）。"""
    with session_scope() as db:
        case = _load(db, case_id)
        assert_access(viewer, case)
        rows = db.execute(
            select(CaseMaterial).where(CaseMaterial.case_id == case_id)
            .order_by(CaseMaterial.id)
        ).scalars().all()
        out = [material_to_dict(m) for m in rows]

    meta = _viewer_meta(viewer)
    if meta and out:
        from backend.agents.harness.audit import audit_writer

        audit_writer.material_access(
            case_id=case_id,
            material=",".join(m["filename"] for m in out[:10]),
            client="api", detail=f"列出 {len(out)} 份材料", **meta,
        )
    return out


def material_path(material_id: int, viewer: Any = None) -> str:
    """取材料的磁盘路径，**权限校验在这里**。

    **为什么不给 ``get_material`` 加一个开关。** 它的返回值会直接进 HTTP 响应体
    （上传接口就踩过这个坑：``register_material`` 返回的是内部视图，
    整包回传等于把服务端绝对路径送给了浏览器）。只要它"可能带 stored_path"，
    就迟早会有调用方忘了剥离。路径只从这一个出口出来，调用方全是服务端自己。
    """
    with session_scope() as db:
        m = db.get(CaseMaterial, material_id)
        if m is None:
            raise CaseNotFound(f"材料不存在：{material_id}")
        case = _load(db, m.case_id)
        assert_access(viewer, case)
        path = m.stored_path or ""

    if not path:
        raise CaseError(f"材料 {material_id} 没有磁盘路径记录，可能未正确落盘")
    return path


def get_material(material_id: int, viewer: Any = None) -> dict[str, Any]:
    with session_scope() as db:
        m = db.get(CaseMaterial, material_id)
        if m is None:
            raise CaseNotFound(f"材料不存在：{material_id}")
        case = _load(db, m.case_id)
        assert_access(viewer, case)
        out = material_to_dict(m)
    meta = _viewer_meta(viewer)
    if meta:
        from backend.agents.harness.audit import audit_writer

        audit_writer.material_access(case_id=out["case_id"], material=out["filename"],
                                     client="api", **meta)
    return out


def delete_material(material_id: int, viewer: Any = None) -> dict[str, Any]:
    """删除材料记录。**默认不动磁盘文件**，与 ``delete_case`` 同一条理由。"""
    with session_scope() as db:
        m = db.get(CaseMaterial, material_id)
        if m is None:
            return {"ok": False, "reason": "材料不存在"}
        case = _load(db, m.case_id)
        assert_access(viewer, case)
        info = material_to_dict(m)
        db.delete(m)
    return {"ok": True, **info, "note": "磁盘文件保留，未删除"}


def mark_parsed(material_id: int, *, parsed_path: str = "", doc_type: str = "") -> dict[str, Any]:
    """标记材料已解析（解析管线跑完后回写）。"""
    with session_scope() as db:
        m = db.get(CaseMaterial, material_id)
        if m is None:
            return {"ok": False, "reason": "材料不存在"}
        m.parsed = True
        if parsed_path:
            m.parsed_path = parsed_path
        if doc_type:
            m.doc_type = doc_type
        db.flush()
        return {"ok": True, **material_to_dict(m)}


def auto_archive_from_state(task_id: str, state: dict[str, Any], *,
                            user_id: int | None = None) -> dict[str, Any] | None:
    """分析任务完成后自动归档成案件（2026-09-21 补的写入路径）。

    **为什么要做**：工作台「在办案件 / 卷宗材料」两张卡统计的是 ``la_case``，
    但分析链路此前从不建案件 —— 律师跑了 28 次审查，卡片永远是 0，
    看起来像统计坏了，其实是**没有数据源**。

    归档规则：
    - 幂等键 = 归档标题（文件名优先，纯文本回退到 query 前 40 字）。
      同一份合同反复审查只对应一个案件 —— 跟真实办案一致：
      一个案件多轮审查，不该每次审查就长出一个新案件。
    - ``case_id`` 由标题哈希生成（``AUTO-<sha1[:12]>``），重跑/重放不会灌重复案件。
    - 有落盘文件（``material_paths``）才登记材料，纯文本分析不造材料。
    - 每次分析都追加一条办理进度，律师能在案件时间线里看到"第几轮 AI 审查"。
    """
    intent = str(state.get("intent") or "")
    case_type = {"contract_review": "contract", "case_analysis": "litigation"}.get(intent, "consult")

    query = str(state.get("query") or "").strip()
    paths = [p for p in (state.get("material_paths") or []) if p]
    title = ""
    if paths:
        try:
            title = safe_filename(Path(paths[0]).stem)
        except Exception:  # noqa: BLE001
            title = ""
    if not title:
        title = query[:40] + ("…" if len(query) > 40 else "")
    if not title:
        return None  # 既没文件名也没 query，没法起标题（正常到不了这里）

    case_id = "AUTO-" + hashlib.sha256(title.encode("utf-8")).hexdigest()[:12]
    exists = False
    try:
        create_case(case_id, title=title, case_type=case_type, stage="intake",
                    owner_id=user_id, summary=query[:200], tags=["AI自动归档", intent or "unknown"])
    except CaseError:
        exists = True  # 同一事项已归档过，只追加进度
    except Exception as exc:  # noqa: BLE001
        logger.warning("案件 %s 自动创建失败：%s", case_id, exc)
        return None

    for p in paths[:3]:
        try:
            register_material(case_id, p,
                              doc_type="contract" if case_type == "contract" else "unknown",
                              uploaded_by=user_id)
        except CaseError as exc:
            logger.warning("案件 %s 材料登记跳过：%s", case_id, exc)

    try:
        add_progress(case_id, stage="intake",
                     note=("AI 分析（复检一轮）" if exists else "AI 分析完成，自动归档"),
                     operator="system", task_id=task_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("案件 %s 进度记录失败：%s", case_id, exc)
    return {"case_id": case_id, "title": title, "created": not exists}


# --------------------------------------------------------------------------- 证据链
# 只由占位符组成的正文**等于没有正文**。``[IMAGE_001]`` / ``[TABLE_001]`` 是抽取层
# 留下的"这里原来有个图/表"的位置标记：视觉不可用时识别结果回插不进去，正文里就
# 只剩这些记号。把它当正文交给律师是"看着有内容、其实什么都没有"，而且会连带把
# ``has_text`` 判成真 —— 界面上那颗「解析」按钮会被藏起来，这份材料就再也没救了。
#
# ⚠️ **只认未回插的形态**。回插后的样子是 ``[IMAGE_001: 借条：今借到…]``
# （冒号后面就是识别出来的原文），那是真内容，绝不能一起丢掉。
_PLACEHOLDER_ONLY = re.compile(r"^(?:\s*\[(?:IMAGE|TABLE)_\d+\]\s*)+$")
# 另一种"看着有内容、其实没有"：整段正文**只由识别失败的标记**组成
# （``[IMAGE_001: 图像内容未识别（…）]``）。多模态不可用时就是这么个形状 ——
# 图片材料解析完的正文全长这样，律师看到的是一行系统标记，而不是材料。
# 混合正文（有真识别结果、也有一两张没识别出来）**必须保留**：那里面有内容。
_VISION_FAIL_ONLY = re.compile(r"^(?:\s*\[(?:IMAGE|TABLE)_\d+:\s*图像内容未识别[^\]]*\]\s*)+$")


def _usable_text(raw: str) -> str:
    """把原始文本规范成"可当正文用"的形态；没有实质内容时返回空串。

    "没有实质内容"只包含两种**确定**的形态：只有占位符、或只有识别失败标记。
    不做"启发式判空"（比如按长度或字符集过滤）—— 误判会把真材料藏起来，
    那比显示一行标记严重得多。
    """
    text = (raw or "").strip()
    if not text or _PLACEHOLDER_ONLY.match(text) or _VISION_FAIL_ONLY.match(text):
        return ""
    return text


def _material_text(m: CaseMaterial, *, max_chars: int) -> tuple[str, str]:
    """尽力从材料取正文，返回 ``(文本, 来源)``，来源是 ``"parsed"`` / ``"raw"`` / ``""``。

    优先读解析产物（``parsed_path``），其次读原文（**仅文本类**）：
    解析产物可能是 JSON（含 ``text`` 字段）或纯文本；``.docx`` / ``.pdf`` 的二进制
    原文读出来是乱码，所以只认文本后缀。取不到返回 ``("", "")`` ——
    调用方各自决定"读不到"该怎么交代（证据链那边是容错的，界面那边要说明原因）。
    """
    for p, src in ((m.parsed_path or "", "parsed"), (m.stored_path or "", "raw")):
        if not p or not Path(p).is_file():
            continue
        try:
            if p.lower().endswith(".json"):
                import json as _json

                blob = _json.loads(Path(p).read_text(encoding="utf-8", errors="ignore"))
                text = blob.get("text") if isinstance(blob, dict) else ""
                if isinstance(text, str):
                    usable = _usable_text(text)
                    if usable:
                        return usable[:max_chars], src
            elif p.lower().endswith((".txt", ".md", ".text")):
                usable = _usable_text(Path(p).read_text(encoding="utf-8", errors="ignore"))
                if usable:
                    return usable[:max_chars], src
        except Exception:  # noqa: BLE001
            continue
    return "", ""


def _read_material_text(m: CaseMaterial, *, max_chars: int = 6000) -> str:
    """给**模型**吃的那一份：只要文本，读不到就是空串（证据链据此标注缺口）。"""
    return _material_text(m, max_chars=max_chars)[0]


# 界面「查看材料」的单次文本上限。给得很大是因为它**不是给模型吃的**：
# 律师要看的是材料原文，截断得太早等于没看。真正防的是"一份几十万字的判决书
# 把浏览器和响应体一起顶穿"，所以给一个宽但有数的闸。
MATERIAL_VIEW_MAX_CHARS = 200_000


def _parsed_gap_reason(product_path: str) -> str:
    """从解析产物里取一条「为什么没抽出文字」的人话理由；取不到回空串。

    产物里的 ``evidence_gaps`` / ``warnings`` 就是管线对这类情况的交代
    （例如"多模态识别服务不可用"）。界面此前只能干说"没有正文"，于是律师
    唯一能做的就是反复点「解析」——**而原因不在材料上，在服务上**，
    点一百次也一样。把原因翻出来，人才知道该去起服务还是该换材料。
    """
    if not product_path or not Path(product_path).is_file():
        return ""
    try:
        import json

        blob = json.loads(Path(product_path).read_text(encoding="utf-8", errors="ignore"))
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(blob, dict):
        return ""
    for gap in blob.get("evidence_gaps") or []:
        if isinstance(gap, dict) and gap.get("reason"):
            return str(gap["reason"])
    warns = blob.get("warnings") or []
    return str(warns[0]) if warns else ""



def read_material_text(material_id: int, viewer: Any = None, *,
                       max_chars: int = MATERIAL_VIEW_MAX_CHARS) -> dict[str, Any]:
    """读一份材料的正文（界面「查看材料」用）。**读取即留痕。**

    为什么不复用 ``_read_material_text`` 一个函数两用：那个是**给模型**的
    （6000 字上限、读不到静默返回空串）。界面要的是"人能看到原样、且知道
    看的是解析产物还是原文、有没有被截断"。合成一个的后果是界面永远只看得到
    前 6000 字，而**用户不会知道后面还有** —— 那比不给看更容易出事。
    """
    cap = max(1000, int(max_chars))
    with session_scope() as db:
        m = db.get(CaseMaterial, material_id)
        if m is None:
            raise CaseNotFound(f"材料不存在：{material_id}")
        case = _load(db, m.case_id)
        assert_access(viewer, case)
        info = material_to_dict(m)
        text, source = _material_text(m, max_chars=cap)
        # ⚠️ 必须在 session 里取出这几个值：`session_scope` 退出时会 commit，
        # ORM 对象的属性随之过期，出了这个块再读 `m.parsed_path` 会炸在
        # DetachedInstanceError 上（这个文件里已经有过同类踩坑）。
        has_product = bool(m.parsed_path)
        product_path = m.parsed_path or ""
        parsed_once = bool(m.parsed)
        stored = m.stored_path or ""

    has_file = bool(stored) and Path(stored).is_file()

    notice = ""
    if not text:
        # 「解析过但没抽到文字」与「还没解析」要**分开说**（2026-10-12）。
        # 从前两种情况共用一句"请先点解析"，而前者的正解是去配多模态服务 ——
        # 界面上表现为"点解析永远没反应"，看起来像按钮坏了。
        # 能分开判是因为 ``mark_parsed`` 会把 ``parsed`` 置位，产物路径另存一列。
        if parsed_once:
            reason = _parsed_gap_reason(product_path)
            notice = ("这份材料解析过了，但没有抽出文字"
                      + (f"：{reason}" if reason else "（原件里可能确实没有可识别的文字）")
                      + "。可以再解析一次；若原件是扫描件或照片，"
                        "需要在后端配置多模态识别服务（VLM）才能读出内容。")
        else:
            notice = ("这份材料还没有解析。点「解析」抽取文字，完成后这里会显示正文 —— "
                      "PDF / Word / 图片都可以。")
    elif has_product and source == "raw":
        notice = "解析产物读取失败，以下显示的是**原文**。"

    meta = _viewer_meta(viewer)
    if meta:
        from backend.agents.harness.audit import audit_writer

        audit_writer.material_access(
            case_id=info["case_id"], material=info["filename"],
            action="material.read", resource_id=str(material_id),
            detail=f"查看材料正文 {len(text)} 字（来源 {source or '无'}）", **meta,
        )

    return {
        **info,
        "text": text,
        "chars": len(text),
        "source": source,
        # 截断要**明说**：律师拿这份文字去核对时，得知道后面还有没有。
        "truncated": len(text) >= cap,
        "notice": notice,
        # 给界面判断"该不该把「解析」按钮摆出来"用（2026-10-12）：
        # 原件还在才解析得动；已经解析过、又没有文字时，按钮该说"重新解析"，
        # 而提示要把原因讲清 —— 否则那颗按钮点下去永远没反应，像是坏的。
        "has_text": bool(text),
        "has_file": has_file,
    }


def get_evidence_chain(case_id: str, viewer: Any = None) -> dict[str, Any]:
    """读取已生成的证据链（没有就返回空）。"""
    with session_scope() as db:
        case = _load(db, case_id)
        assert_access(viewer, case)
        return {
            "case_id": case_id,
            "chain": case.evidence_chain or [],
            "generated_at": case.evidence_chain_at or "",
        }


def find_case_by_task(task_id: str, viewer: Any = None) -> dict[str, Any] | None:
    """按任务号反查它归属的案件。

    自动归档的案件在 ``progress`` 流水里记了 task_id（见 ``auto_archive_from_state``），
    所以这里遍历可见案件、匹配流水即可，**不需要额外映射表**。
    律师可见案件上限 200，线性扫描成本可忽略。
    """
    for c in list_cases(viewer):
        for p in (c.get("progress") or []):
            if p.get("task_id") == task_id:
                return c
    return None


async def generate_evidence_chain(case_id: str, viewer: Any = None) -> dict[str, Any]:
    """生成并落库证据链。

    聚合：案件摘要 + 各材料文件名/类型/正文（尽力），交给 LLM 产出
    ``[{claim, evidence:[{material, excerpt, support}], gap}]`` 结构。
    模型不可用（降级）时退回确定性骨架：仍以材料为节点、标注"待补充论证"。
    """
    import datetime as _dt
    import json as _json

    from backend.llm.client import llm_client

    with session_scope() as db:
        case = _load(db, case_id)
        assert_access(viewer, case)
        assert_evidence_chain_allowed(case.case_type)
        mats = db.execute(
            select(CaseMaterial).where(CaseMaterial.case_id == case_id).order_by(CaseMaterial.id)
        ).scalars().all()

    mat_blocks = []
    for m in mats:
        text = _read_material_text(m)
        mat_blocks.append({
            "filename": m.filename or "",
            "doc_type": m.doc_type or "unknown",
            "text": text,
        })

    prompt = (
        "你是资深诉讼律师的证据助理。请根据下面的案件信息与已有材料，构建「证据链」：\n"
        "把案件的核心事实主张（claim）逐条列出，每条主张对应支撑它的证据"
        "（material=材料名称，excerpt=材料中的关键片段或要点，support=直接/间接），\n"
        "并标明该主张的证据缺口（gap，例如缺少某类凭证、仅有当事人陈述无书面证据等）。\n"
        "若材料正文为空，请基于案件摘要推断可能需要的证据并标注为缺口。\n"
        "严格只输出 JSON，字段为：\n"
        '{"chains":[{"claim":"...","evidence":[{"material":"...","excerpt":"...","support":"直接/间接"}],'
        '"gap":"..."}],"summary":"一句话总评证据充分度"}'
    )
    user_blob = _json.dumps({
        "案件名称": case.title or "",
        "案由": case.cause_of_action or "",
        "案件摘要": (case.summary or "")[:2000],
        "材料": [
            {"filename": b["filename"], "doc_type": b["doc_type"], "正文摘要": b["text"][:1500]}
            for b in mat_blocks
        ],
    }, ensure_ascii=False)

    chain, summ = [], ""
    try:
        data, _ = await llm_client.chat_json(
            [{"role": "system", "content": prompt},
             {"role": "user", "content": user_blob}],
            task="evidence_chain",
        )
        chains = (data or {}).get("chains") or []
        # 清洗：保证每条结构完整
        for c in chains:
            if not isinstance(c, dict):
                continue
            evs = c.get("evidence") or []
            chain.append({
                "claim": str(c.get("claim") or "").strip(),
                "evidence": [
                    {"material": str(e.get("material") or "").strip(),
                     "excerpt": str(e.get("excerpt") or "").strip(),
                     "support": str(e.get("support") or "直接").strip()}
                    for e in evs if isinstance(e, dict)
                ],
                "gap": str(c.get("gap") or "").strip(),
            })
        summ = str((data or {}).get("summary") or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("证据链 LLM 生成失败，退回骨架：%s", exc)
        # 降级骨架：以材料为节点，缺口待补
        for b in mat_blocks:
            chain.append({
                "claim": f"围绕《{b['filename']}》所证明的事实主张",
                "evidence": [{"material": b["filename"], "excerpt": "（未做 LLM 论证，请律师补充）", "support": "待定"}],
                "gap": "模型不可用，未能自动论证；请律师核对材料并补全证据链。",
            })

    now = _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    with session_scope() as db:
        case = _load(db, case_id)
        case.evidence_chain = chain
        case.evidence_chain_at = now
        db.flush()
    return {"case_id": case_id, "chain": chain, "summary": summ, "generated_at": now}


# --------------------------------------------------------------------------- 证据充分度评估
# 读案件已生成的证据链，给出「充分 / 基本充分但有风险 / 证据不足」三档，
# 并拆分致命缺口（核心主张无任何证据）与可补强缺口，附补证建议。
# 纯确定性计算，不依赖模型（避免烧额度、慢、且结果可复现）；缺口文案直接复用证据链里 LLM 已生成的内容。
TIER = {
    "充足": ("ps", "证据较为充分，核心主张均有支撑，主要风险点已在下方列明。"),
    "基本充分但有风险": ("wn", "已具备一定证据基础，但存在缺口或仅间接证据，胜诉存在不确定性，建议补强后再主张。"),
    "证据不足": ("dg", "关键主张缺乏证据支撑，当前难以形成完整证据链，建议优先补证后再评估。"),
}


def assess_evidence_sufficiency(case_id: str, viewer: Any = None) -> dict[str, Any]:
    """依据已生成的证据链评估证据充分度（确定性，不调模型）。

    三档判定：
      - 充足：每条主张都有证据支撑，且无缺口；
      - 证据不足：超过半数主张无任何证据，或根本没生成证据链；
      - 基本充分但有风险：介于两者之间。
    致命缺口 = 主张没有任何证据且带缺口；可补强缺口 = 已有证据（含间接）但仍带缺口。
    """
    import datetime as _dt

    with session_scope() as db:
        case = _load(db, case_id)
        assert_access(viewer, case)
        chain = list(case.evidence_chain or [])

    # 没链：直接给「证据不足」，引导先生成证据链
    if not chain:
        return {
            "case_id": case_id,
            "tier": "证据不足",
            "tier_tag": "dg",
            "tier_reason": "尚未生成证据链（或卷宗暂无材料），无法评估；请先上传材料并生成证据链。",
            "metrics": {"total_claims": 0, "supported_claims": 0, "direct_claims": 0,
                        "gap_claims": 0, "unsupported_claims": 0},
            "fatal_gaps": [],
            "fixable_gaps": [],
            "suggestions": ["先在「证据材料」区上传合同原件、付款凭证、聊天记录等关键材料；",
                            "材料上传后点「生成证据链」；", "再回到此处点「评估」即可获得充分度结论。"],
            "disclaimer": "AI 辅助判断，非法律意见，最终以律师意见为准。",
            "generated_at": _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }

    total = len(chain)
    supported = 0   # 至少 1 条证据
    direct = 0      # 至少 1 条直接证据
    gap_claims = 0
    unsupported = 0  # 0 条证据
    fatal, fixable = [], []
    suggestions = []

    for c in chain:
        evs = c.get("evidence") or []
        gap = (c.get("gap") or "").strip()
        has_ev = len(evs) > 0
        has_direct = any((e.get("support") or "直接") == "直接" for e in evs)
        if has_ev:
            supported += 1
        if has_direct:
            direct += 1
        if gap:
            gap_claims += 1
        if not has_ev:
            unsupported += 1
        claim = c.get("claim") or "（未命名主张）"
        if not has_ev and gap:
            fatal.append({"claim": claim, "gap": gap})
            suggestions.append("针对「%s」补充书面证据/原始凭证——该主张当前无任何证据支撑，属致命缺口。" % claim)
        elif has_ev and gap:
            fixable.append({"claim": claim, "gap": gap})
            ev_word = "直接" if has_direct else "间接"
            suggestions.append("「%s」已有%s证据，但仍有缺口，建议补强：%s"
                               % (claim, ev_word, gap))

    if unsupported == 0 and gap_claims == 0:
        tier = "充足"
    elif unsupported > total / 2 or supported == 0:
        tier = "证据不足"
    else:
        tier = "基本充分但有风险"

    reason = TIER[tier][1]
    # 补一条通用建议
    if tier != "充足":
        suggestions.append("完善证据链后，再次点「评估」即可刷新结论。")
    if not suggestions:
        suggestions.append("证据链完整、各主张均有支撑，维持当前举证策略即可。")

    return {
        "case_id": case_id,
        "tier": tier,
        "tier_tag": TIER[tier][0],
        "tier_reason": reason,
        "metrics": {"total_claims": total, "supported_claims": supported,
                    "direct_claims": direct, "gap_claims": gap_claims,
                    "unsupported_claims": unsupported},
        "fatal_gaps": fatal,
        "fixable_gaps": fixable,
        "suggestions": suggestions,
        "disclaimer": "AI 辅助判断，非法律意见，最终以律师意见为准。",
        "generated_at": _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }


# --------------------------------------------------------------------------- 胜诉把握
# 依据已生成的证据链算「证据覆盖率」，再让模型补一段法律环境分析。
#
# ⚠ **数字与理由严格分家**：数字只由下面的 ``score_win_rate`` 算，
# 而模型输出 schema 里**没有任何数字字段** —— 这是刻意的：字段不存在模型就碰不到，
# 比在 prompt 里写十句"不要输出分数"可靠得多。模型讲的是举证责任在谁、缺哪个构成要件、
# 下一步该取什么证；它说"解除合法性的举证责任在用人单位"时，读者知道那是**另一层信息**，
# 不是对上面那个数字的修正。
#
# 口径必须写明是「证据覆盖率」而不是「胜诉率预测」：证据链只是一份
# 主张 → 证据 → 缺口的清单，它不知道对方手里有什么、不知道法官怎么裁量、
# 也不知道诉讼时效。把覆盖率说成"胜诉率"是过度承诺。
DIRECT_POINTS = 100    # 有直接证据
INDIRECT_POINTS = 60   # 只有间接证据（含降级骨架里 support="待定" 的那些）
GAP_DISCOUNT = 0.85    # 该条主张仍带缺口时的折扣


def score_win_rate(chain: list | None) -> dict[str, Any]:
    """按证据链算证据覆盖率。**纯函数**：不碰 DB、不调模型，单测直接打它。

    每条主张单独计分再取平均：
        有直接证据 → 100 ／ 只有间接证据 → 60 ／ 没有任何证据 → 0
        该条带 gap 时再 ×0.85

    **单调性**：给某条追加证据只会让它 0→60→100，缺口被填掉还能一并摘掉 0.85 的折扣，
    所以分数只升不降 —— 「加了证据，数字就往上走」这个反馈回路成立，
    用户能看见自己补证的动作有没有用。

    **各条主张等权**：加权重就得判断哪条是"基础事实"（本案里「劳动关系存续」
    一旦塌了整案就塌，不是"少 20%"），那是法律判断，交给代码做只会做出一个
    看着精确实则拍脑袋的权重。宁可各说各的：「哪条是致命的」由致命缺口清单
    单独承载（见 ``assess_evidence_sufficiency`` 的 ``fatal_gaps``），数字只管覆盖率。
    """
    items = [c for c in (chain or []) if isinstance(c, dict)]
    total = len(items)
    if total == 0:
        # **空链不给 0 分**。「0 分」和「还没法算」是两回事，
        # 混在一起用户会以为功能坏了。上层据此显示引导语。
        return {
            "status": "no_chain",
            "score": None,
            "metrics": {"total": 0, "direct": 0, "indirect": 0,
                        "unsupported": 0, "gap": 0},
            "per_claim": [],
        }

    direct = indirect = unsupported = gap_n = 0
    per_claim: list[dict[str, Any]] = []
    acc = 0.0

    for c in items:
        evs = [e for e in (c.get("evidence") or []) if isinstance(e, dict)]
        # 缺 support 字段时按「直接」算 —— 与 ``assess_evidence_sufficiency``
        # 里 ``(e.get("support") or "直接")`` 的既有约定保持一致。
        if not evs:
            level, points = "unsupported", 0.0
            unsupported += 1
        elif any((e.get("support") or "直接") == "直接" for e in evs):
            level, points = "direct", float(DIRECT_POINTS)
            direct += 1
        else:
            level, points = "indirect", float(INDIRECT_POINTS)
            indirect += 1

        gap = (c.get("gap") or "").strip()
        if gap:
            gap_n += 1
            points *= GAP_DISCOUNT

        acc += points
        per_claim.append({
            "claim": c.get("claim") or "（未命名主张）",
            "level": level,
            "points": round(points, 1),
            "gap": gap,
        })

    return {
        "status": "ok",
        "score": int(round(acc / total)),
        "metrics": {"total": total, "direct": direct, "indirect": indirect,
                    "unsupported": unsupported, "gap": gap_n},
        "per_claim": per_claim,
    }


WIN_TASK = "win_rate"
WIN_DISCLAIMER = "AI 辅助判断，非法律意见，最终以律师意见为准。"
WIN_CALIBER = ("本数字是证据覆盖率：主张有没有证据支撑、证据是直接还是间接、缺口填了没有。"
               "不含法律适用与举证责任分配，也不是对判决结果的预测。")
WIN_BOUNDARY = "以下是法律环境分析，不改变上方的证据测算值。"
# 模型只准输出这四类文本，**一个数字字段都没有** —— 见本段开头的说明。
WIN_FIELDS = ("reasons", "risks", "next_moves", "decisions")


def _win_clean_list(v: Any, limit: int = 12) -> list[str]:
    """把模型给的东西压成干净的字符串列表。"""
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, (list, tuple)):
        return []
    out = []
    for item in v:
        if isinstance(item, dict):
            # 模型偶尔会把 {"claim": "...", "point": "..."} 塞进来，取可读的那半边
            item = item.get("point") or item.get("text") or item.get("claim") or ""
        s = str(item or "").strip()
        if s:
            out.append(s[:500])
    return out[:limit]


def _win_result(case_id: str, case_type: str | None, chain: list,
                chain_at: str, stored: dict, stored_at: str) -> dict[str, Any]:
    """把「现算的数字」与「存着的模型分析」拼成一份返回体。

    GET 与 POST 共用它，保证两条路径同构 —— 否则前端得写两套渲染。
    """
    if (case_type or "") != "litigation":
        return {
            "case_id": case_id, "supported": False, "status": "not_supported",
            "message": "胜诉把握评估只对诉讼案件开放。",
            "score": None, "metrics": {}, "per_claim": [],
            "analysis": {}, "analysis_stale": False, "stored_at": "",
            "chain_at": chain_at, "caliber": WIN_CALIBER, "boundary": WIN_BOUNDARY,
            "disclaimer": WIN_DISCLAIMER,
        }

    result = score_win_rate(chain)
    # ⚠ 过期判定靠的是**做结论时**的 evidence_chain_at（存进 stored["chain_at"]），
    # 不是 now()。若存的是 now()，重算一次就永远新鲜，这个标志立刻作废。
    stale = bool(stored) and str(stored.get("chain_at") or "") != (chain_at or "")
    return {
        "case_id": case_id, "supported": True, "status": result["status"],
        "score": result["score"], "metrics": result["metrics"],
        "per_claim": result["per_claim"],
        "analysis": stored, "analysis_stale": stale, "stored_at": stored_at,
        "chain_at": chain_at, "caliber": WIN_CALIBER, "boundary": WIN_BOUNDARY,
        "disclaimer": WIN_DISCLAIMER,
    }


def get_win_assessment(case_id: str, viewer: Any = None) -> dict[str, Any]:
    """读取胜诉把握评估。**数字现算，AI 分析读回上次存的。**

    数字是纯确定性、零成本的，重算即可，不落库；落库只会制造
    "现在显示的是哪一次的分数"的歧义，而且将来调一次权重就得回填全库。

    与 ``assert_evidence_chain_allowed`` 的既定口径一致：**只读 GET 不拦非诉案件**
    （那道闸门拦的是"产生新数据"的写路径），非诉在这里返回 ``not_supported``。
    """
    with session_scope() as db:
        case = _load(db, case_id)
        assert_access(viewer, case)
        return _win_result(
            case_id, case.case_type, list(case.evidence_chain or []),
            case.evidence_chain_at or "",
            # ⚠ 补出来的列在**已有行上是 NULL**（见 db/session.py 的补列逻辑），
            # 不是模型里写的 default={}。不 or 兜底，第一件老案件就炸在 None 上。
            dict(case.win_assessment or {}), case.win_assessment_at or "",
        )


async def generate_win_assessment(case_id: str, viewer: Any = None) -> dict[str, Any]:
    """生成胜诉把握评估：**数字代码算，法律分析交给模型，只把模型那半落库。**

    模型不可用时不留白：法律分析的四个列表退化成
    ``assess_evidence_sufficiency`` 已经算好的确定性建议（补证建议 / 缺口），
    并标 ``degraded=True``。**数字区永远不受影响** ——
    它本来就是代码算的，这正是"能算的必须由代码钉死"的直接体现。
    """
    import datetime as _dt
    import json as _json

    from backend.llm.client import llm_client

    with session_scope() as db:
        case = _load(db, case_id)
        assert_access(viewer, case)
        assert_evidence_chain_allowed(case.case_type)
        chain = list(case.evidence_chain or [])
        chain_at = case.evidence_chain_at or ""
        title = case.title or ""
        summary = case.summary or ""
        case_type = case.case_type

    result = score_win_rate(chain)
    if result["status"] == "no_chain":
        # 没链就不去烧模型额度，也不编一个分析出来。
        return {**_win_result(case_id, case_type, chain, chain_at, {}, ""),
                "status": "no_chain",
                "message": "尚未生成证据链，无法测算。请先上传材料并生成证据链。"}

    # 给模型的输入：主张 / 已有证据 / 缺口 / 该条的覆盖档次。
    # 刻意**不告诉它总分** —— 它知道分数就会去解释分数，那是我们要避免的。
    level_cn = {"direct": "已有直接证据", "indirect": "仅有间接证据", "unsupported": "尚无证据"}
    chain_blob = [
        {"主张": p["claim"], "证据状态": level_cn.get(p["level"], p["level"]),
         "已列出的证据": [e.get("material") or "" for e in (c.get("evidence") or [])
                          if isinstance(e, dict)],
         "缺口": p["gap"]}
        for p, c in zip(result["per_claim"], chain)
    ]

    prompt = (
        "你是资深诉讼律师。下面给出案件的证据链（主张 → 已有证据 → 缺口）与逐条证据状态。\n"
        "请只做**法律环境**层面的分析，输出四个数组：\n"
        "reasons：对我方有利的法律因素，例如举证责任在对方、对方需自证程序合法、"
        "法定标准明确等；\n"
        "risks：不利因素与对方可能的抗辩；\n"
        "next_moves：按优先级排列的下一步取证/举证动作，要具体到「取哪一份材料、找谁开」；\n"
        "decisions：需要律师拍板的事项，例如是否申请法院调查取证、是否追加诉讼请求。\n"
        "**只讲法律与证据。不要输出任何分数、百分比、概率、几成把握之类的数字，"
        "也不要评价证据够不够、多不多** —— 覆盖率由系统另行计算，"
        "你写的数字会和它对不上，用户会看到两套口径。\n"
        "每条 40 字以内，最多 6 条。严格只输出 JSON：\n"
        '{"reasons":["..."],"risks":["..."],"next_moves":["..."],"decisions":["..."]}'
    )
    user_blob = _json.dumps({
        "案件名称": title[:200],
        "案件摘要": summary[:1200],
        "证据链": chain_blob,
    }, ensure_ascii=False)

    fallback = _deterministic_win_analysis(case_id, viewer)
    try:
        data, meta = await llm_client.chat_json(
            [{"role": "system", "content": prompt},
             {"role": "user", "content": user_blob}],
            task=WIN_TASK,
            payload={"chain": chain_blob, "metrics": result["metrics"],
                     "suggestions": fallback.get("next_moves") or []},
            default=fallback,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("胜诉把握·法律分析调用失败，退回确定性建议：%s", exc)
        data, meta = fallback, None

    data = data if isinstance(data, dict) else {}
    analysis = {f: _win_clean_list(data.get(f)) for f in WIN_FIELDS}
    degraded = bool(getattr(meta, "degraded", True)) if meta is not None else True
    # 一个字段都没解析出来 = 模型这趟等于没干活，用确定性的顶上，别给用户一片空白。
    if not any(analysis.values()):
        analysis = {f: list(fallback.get(f) or []) for f in WIN_FIELDS}
        degraded = True

    stored = {
        "chain_at": chain_at,          # ← 过期判定就靠它（不是 now）
        **analysis,
        "generated_at": _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "provider": str(getattr(meta, "provider", "") or ""),
        "model": str(getattr(meta, "model", "") or ""),
        "degraded": degraded,
    }
    now = _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    with session_scope() as db:
        case = _load(db, case_id)
        case.win_assessment = stored
        case.win_assessment_at = now
        db.flush()

    return _win_result(case_id, case_type, chain, chain_at, stored, now)


def _deterministic_win_analysis(case_id: str, viewer: Any = None) -> dict[str, list[str]]:
    """模型不可用时的兜底：复用充分度评估已经算好的确定性结论。

    刻意**只搬运不新编** —— 这里多写一句没有依据的法律判断，
    降级状态下用户也没法分辨哪句是模型说的、哪句是代码编的。
    """
    try:
        sufficiency = assess_evidence_sufficiency(case_id, viewer)
    except Exception as exc:  # noqa: BLE001
        logger.warning("兜底分析取充分度评估失败：%s", exc)
        return {f: [] for f in WIN_FIELDS}

    reasons = []
    if sufficiency.get("tier") == "充足":
        reasons.append(str(sufficiency.get("tier_reason") or ""))
    reasons = [r for r in reasons if r]

    risks = [f"{g.get('claim')}：{g.get('gap')}"
             for g in (sufficiency.get("fatal_gaps") or []) + (sufficiency.get("fixable_gaps") or [])]
    return {
        "reasons": reasons,
        "risks": risks,
        "next_moves": [str(s) for s in (sufficiency.get("suggestions") or [])],
        "decisions": [],
    }


__all__ = [
    "CASE_TYPES",
    "STAGES",
    "CaseError",
    "CaseForbidden",
    "CaseNotFound",
    "add_progress",
    "assert_access",
    "auto_archive_from_state",
    "assess_evidence_sufficiency",
    "case_to_dict",
    "create_case",
    "delete_case",
    "delete_material",
    "find_case_by_task",
    "generate_evidence_chain",
    "generate_win_assessment",
    "get_case",
    "get_evidence_chain",
    "get_material",
    "get_win_assessment",
    "list_cases",
    "list_materials",
    "mark_parsed",
    "material_dir",
    "material_path",
    "material_to_dict",
    "read_material_text",
    "register_material",
    "safe_filename",
    "score_win_rate",
    "sha256_of",
    "stats",
    "update_case",
]
