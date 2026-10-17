"""用户与团队服务（方案第十二章「用户权限管理」）。

三个刻意的决定，都不是"顺手这么写"：

1. **删除用户是停用，不是 DELETE 行。**
   ``la_case.owner_id``、``la_task.user_id``、``la_audit_log.user_id`` 都指向用户。
   真删会让这些引用悬空 —— 案件查不到承办人、审计记录里的 user_id 变成孤儿，
   而审计按合规要求本来就不可删改。所以 ``deactivate()`` 改 ``is_active=False``：
   登录被拒、新任务不接，历史记录完整保留。

2. **口令只进不出。** 所有对外返回的 dict 都不含 ``hashed_password``，
   靠 ``_to_dict`` 一处收敛。散着写"记得别带密码"迟早会漏一次。

3. **种子账号只在启用了默认口令时创建**（``seed_defaults``）。
   生产环境把 ``ADMIN_PASSWORD`` 留成 ``admin123`` 是最典型的失守方式，
   所以这里在 ``env=prod`` 且口令仍是默认值时**拒绝创建**并大声告警，
   而不是"先把账号建好，让运维记得改"。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from backend.app.config import settings
from backend.app.db.models import Team, User
from backend.app.db.session import session_scope
from backend.app.security import hash_password, verify_password

logger = logging.getLogger("legal_agent.services.user")

VALID_ROLES = ("admin", "lawyer")
# 口令最短长度。律师账号能看案件原文，弱口令等于把卷宗挂在门上。
MIN_PASSWORD_LENGTH = 8
# 这两个是 .env 里的出厂值，出现在生产环境即为配置事故
DEFAULT_PASSWORDS = {"admin123", "lawyer123", "123456", "password"}


class UserError(ValueError):
    """业务校验失败（由接口层转成 400）。"""


# --------------------------------------------------------------------------- 转换
def user_to_dict(user: User) -> dict[str, Any]:
    """对外表示。**不含口令散列** —— 这是唯一的收敛点。

    ``is_admin`` 是**必须有的派生字段**，不是可有可无的补充：
    ``/auth/login`` 与 ``/auth/me`` 都走这里，而前端用
    ``user.is_admin`` 决定要不要显示「管理后台」入口。
    只给 ``role`` 的话前端得自己判 ``role === "admin"`` —— 那等于把
    "谁是管理员"这条规则复制到客户端，服务端将来加角色（如 ``auditor``）
    时前端会静默判错，而表现是"管理员看不到管理入口"这种最难想到的原因。
    权限的语义只应有一份，就是 :attr:`User.is_admin`。
    """
    return {
        "id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "role": user.role,
        "is_admin": bool(user.is_admin),
        "license_no": user.license_no,
        "team_id": user.team_id,
        "is_active": bool(user.is_active),
        "last_login_at": user.last_login_at or "",
        "created_at": user.created_at.isoformat(timespec="seconds") if user.created_at else "",
        "updated_at": user.updated_at.isoformat(timespec="seconds") if user.updated_at else "",
    }


def team_to_dict(team: Team, *, user_count: int | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": team.id,
        "name": team.name,
        "description": team.description or "",
        "case_isolation": bool(team.case_isolation),
        "created_at": team.created_at.isoformat(timespec="seconds") if team.created_at else "",
    }
    if user_count is not None:
        d["user_count"] = user_count
    return d


# --------------------------------------------------------------------------- 团队
def list_teams(*, with_count: bool = True) -> list[dict[str, Any]]:
    with session_scope() as db:
        teams = db.execute(select(Team).order_by(Team.id)).scalars().all()
        counts: dict[int, int] = {}
        if with_count:
            rows = db.execute(
                select(User.team_id, func.count()).group_by(User.team_id)
            ).all()
            counts = {int(t): int(c) for t, c in rows if t is not None}
        return [team_to_dict(t, user_count=counts.get(t.id, 0)) for t in teams]


def get_team(team_id: int) -> dict[str, Any] | None:
    with session_scope() as db:
        team = db.get(Team, team_id)
        return team_to_dict(team) if team else None


def create_team(name: str, *, description: str = "", case_isolation: bool = True) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise UserError("团队名不能为空")
    try:
        with session_scope() as db:
            team = Team(name=name, description=description or "", case_isolation=bool(case_isolation))
            db.add(team)
            db.flush()
            return team_to_dict(team)
    except IntegrityError as exc:
        raise UserError(f"团队名已存在：{name}") from exc


def update_team(team_id: int, **fields: Any) -> dict[str, Any] | None:
    with session_scope() as db:
        team = db.get(Team, team_id)
        if team is None:
            return None
        if "name" in fields and fields["name"]:
            team.name = str(fields["name"]).strip()
        if "description" in fields and fields["description"] is not None:
            team.description = str(fields["description"])
        if "case_isolation" in fields and fields["case_isolation"] is not None:
            team.case_isolation = bool(fields["case_isolation"])
        db.flush()
        return team_to_dict(team)


def delete_team(team_id: int, *, reassign_to: int | None = None) -> dict[str, Any]:
    """删除团队。**有成员时默认拒绝**，除非显式指定改派目标。

    为什么不静默把成员的 ``team_id`` 置空：那样律师会在毫无提示的情况下
    失去对本团队案件的访问权（``can_access_case`` 要求 ``team_id`` 相等），
    表现是"昨天还能打开的案件今天 403"，而没人知道原因。
    """
    with session_scope() as db:
        team = db.get(Team, team_id)
        if team is None:
            return {"ok": False, "reason": "团队不存在"}
        members = db.execute(
            select(func.count()).select_from(User).where(User.team_id == team_id)
        ).scalar_one()
        members = int(members or 0)
        if members and reassign_to is None:
            return {
                "ok": False,
                "reason": f"该团队下仍有 {members} 名成员，需先指定改派团队或调整成员归属",
                "user_count": members,
            }
        if members and reassign_to is not None:
            if reassign_to == team_id:
                return {"ok": False, "reason": "改派目标不能是自身"}
            target = db.get(Team, reassign_to)
            if target is None:
                return {"ok": False, "reason": f"改派目标团队不存在：{reassign_to}"}
            for u in db.execute(select(User).where(User.team_id == team_id)).scalars():
                u.team_id = reassign_to
            logger.info("团队 %s 删除前将 %d 名成员改派至团队 %s", team_id, members, reassign_to)
        db.delete(team)
        return {"ok": True, "reassigned": members if reassign_to is not None else 0}


# --------------------------------------------------------------------------- 用户
def get_user(user_id: int) -> dict[str, Any] | None:
    with session_scope() as db:
        user = db.get(User, user_id)
        return user_to_dict(user) if user else None


def get_by_username(username: str) -> dict[str, Any] | None:
    with session_scope() as db:
        user = db.execute(
            select(User).where(User.username == (username or "").strip())
        ).scalar_one_or_none()
        return user_to_dict(user) if user else None


def list_users(
    *,
    keyword: str = "",
    role: str = "",
    team_id: int | None = None,
    active_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 50), 200))
    stmt = select(User)
    if keyword:
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(or_(User.username.like(like), User.full_name.like(like),
                              User.license_no.like(like)))
    if role:
        stmt = stmt.where(User.role == role)
    if team_id is not None:
        stmt = stmt.where(User.team_id == team_id)
    if active_only:
        stmt = stmt.where(User.is_active.is_(True))
    stmt = stmt.order_by(User.id).limit(limit).offset(max(0, int(offset)))
    with session_scope() as db:
        return [user_to_dict(u) for u in db.execute(stmt).scalars().all()]


def _validate(username: str, password: str, role: str) -> tuple[str, str]:
    username = (username or "").strip()
    if len(username) < 3:
        raise UserError("登录名至少 3 个字符")
    if role not in VALID_ROLES:
        raise UserError(f"角色只能是 {' / '.join(VALID_ROLES)}")
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise UserError(f"口令至少 {MIN_PASSWORD_LENGTH} 位")
    return username, role


def create_user(
    username: str,
    password: str,
    *,
    role: str = "lawyer",
    full_name: str = "",
    license_no: str = "",
    team_id: int | None = None,
    is_active: bool = True,
) -> dict[str, Any]:
    username, role = _validate(username, password, role)
    try:
        with session_scope() as db:
            if db.execute(select(User).where(User.username == username)).scalar_one_or_none():
                raise UserError(f"登录名已存在：{username}")
            if team_id is not None and db.get(Team, team_id) is None:
                raise UserError(f"团队不存在：{team_id}")
            user = User(
                username=username,
                hashed_password=hash_password(password),
                full_name=full_name or username,
                role=role,
                license_no=license_no or "",
                team_id=team_id,
                is_active=bool(is_active),
            )
            db.add(user)
            db.flush()
            logger.info("已创建用户：%s（role=%s team=%s）", username, role, team_id)
            return user_to_dict(user)
    except IntegrityError as exc:
        raise UserError(f"登录名已存在：{username}") from exc


def update_user(user_id: int, **fields: Any) -> dict[str, Any] | None:
    """更新用户。``password`` 单独处理（走散列），其余按白名单赋值。"""
    with session_scope() as db:
        user = db.get(User, user_id)
        if user is None:
            return None

        if fields.get("password"):
            pwd = str(fields["password"])
            if len(pwd) < MIN_PASSWORD_LENGTH:
                raise UserError(f"口令至少 {MIN_PASSWORD_LENGTH} 位")
            user.hashed_password = hash_password(pwd)
            logger.info("用户 %s 口令已重置", user.username)

        if fields.get("role"):
            if fields["role"] not in VALID_ROLES:
                raise UserError(f"角色只能是 {' / '.join(VALID_ROLES)}")
            # 不允许把最后一个管理员降级 —— 否则管理后台再也进不去
            if user.role == "admin" and fields["role"] != "admin" and _admin_count(db) <= 1:
                raise UserError("系统内仅剩一名管理员，不能降级；请先指定另一名管理员")
            user.role = fields["role"]

        for src, dst in (("full_name", "full_name"), ("license_no", "license_no")):
            if fields.get(src) is not None:
                setattr(user, dst, str(fields[src]))
        if "team_id" in fields:
            if fields["team_id"] is not None and db.get(Team, int(fields["team_id"])) is None:
                raise UserError(f"团队不存在：{fields['team_id']}")
            user.team_id = int(fields["team_id"]) if fields["team_id"] is not None else None
        if fields.get("is_active") is not None:
            user.is_active = bool(fields["is_active"])

        db.flush()
        return user_to_dict(user)


def deactivate(user_id: int) -> dict[str, Any]:
    """停用（见模块 docstring 第 1 条：不做物理删除）。"""
    with session_scope() as db:
        user = db.get(User, user_id)
        if user is None:
            return {"ok": False, "reason": "用户不存在"}
        if user.role == "admin" and user.is_active and _admin_count(db) <= 1:
            return {"ok": False, "reason": "系统内仅剩一名启用中的管理员，不能停用"}
        user.is_active = False
        return {"ok": True, "username": user.username}


def reactivate(user_id: int) -> dict[str, Any]:
    with session_scope() as db:
        user = db.get(User, user_id)
        if user is None:
            return {"ok": False, "reason": "用户不存在"}
        user.is_active = True
        return {"ok": True, "username": user.username}


def _admin_count(db: Any) -> int:
    return int(db.execute(
        select(func.count()).select_from(User)
        .where(User.role == "admin", User.is_active.is_(True))
    ).scalar_one() or 0)


# --------------------------------------------------------------------------- 登录
def authenticate(username: str, password: str) -> dict[str, Any] | None:
    """口令校验。**失败不区分「用户不存在」与「口令错误」**（对外措辞一致）。

    但内部要区分：用户不存在时也要跑一次散列校验（``_dummy_verify``），
    否则响应时间会泄露"这个账号存不存在"——等于是免费的账号枚举接口。
    """
    name = (username or "").strip()
    with session_scope() as db:
        user = db.execute(select(User).where(User.username == name)).scalar_one_or_none()
        if user is None:
            _dummy_verify(password)
            return None
        if not verify_password(password or "", user.hashed_password):
            return None
        if not user.is_active:
            return {"_inactive": True, "username": user.username}
        user.last_login_at = datetime.now().isoformat(timespec="seconds")
        db.flush()
        return user_to_dict(user)


def _dummy_verify(password: str) -> None:
    """跑一次等价开销的散列运算，抹平"存在/不存在"的响应时间差。"""
    verify_password(password or "", "pbkdf2_sha256$120000$AAAAAAAAAAAAAAAAAAAAAA==$AAAA")


# --------------------------------------------------------------------------- 种子
def seed_defaults(*, force: bool = False) -> dict[str, Any]:
    """创建默认团队与管理员/演示律师账号（幂等）。

    生产环境拒绝用出厂口令建号 —— 见模块 docstring 第 3 条。
    """
    created: list[str] = []
    skipped: list[str] = []

    if settings.ENV == "prod":
        weak = [
            name for name, pwd in (("ADMIN_PASSWORD", settings.ADMIN_PASSWORD),
                                   ("DEMO_LAWYER_PASSWORD", settings.DEMO_LAWYER_PASSWORD))
            if pwd in DEFAULT_PASSWORDS
        ]
        if weak:
            logger.error(
                "生产环境（ENV=prod）仍在使用出厂口令（%s），"
                "已拒绝创建种子账号。请在 .env 中设置强口令后重启。",
                "、".join(weak),
            )
            return {"created": [], "skipped": ["生产环境出厂口令未修改，拒绝创建种子账号"],
                    "weak": weak}

    with session_scope() as db:
        default_team = db.execute(select(Team).order_by(Team.id)).scalars().first()
        if default_team is None:
            default_team = Team(name="诉讼一组", description="默认团队（系统初始化）",
                                case_isolation=True)
            db.add(default_team)
            db.flush()
            created.append("团队:诉讼一组")

        for username, password, role, full_name in (
            (settings.ADMIN_USERNAME, settings.ADMIN_PASSWORD, "admin", "系统管理员"),
            (settings.DEMO_LAWYER_USERNAME, settings.DEMO_LAWYER_PASSWORD, "lawyer", "演示律师"),
        ):
            exists = db.execute(
                select(User).where(User.username == username)
            ).scalar_one_or_none()
            if exists is not None:
                skipped.append(f"用户:{username}")
                continue
            if not force and len(password or "") < MIN_PASSWORD_LENGTH and settings.ENV != "dev":
                skipped.append(f"用户:{username}（口令短于 {MIN_PASSWORD_LENGTH} 位）")
                continue
            db.add(User(
                username=username,
                hashed_password=hash_password(password),
                full_name=full_name,
                role=role,
                team_id=default_team.id,
                is_active=True,
            ))
            created.append(f"用户:{username}({role})")

    if created:
        logger.info("种子数据已创建：%s", "、".join(created))
    return {"created": created, "skipped": skipped}


def stats() -> dict[str, Any]:
    with session_scope() as db:
        total = int(db.execute(select(func.count()).select_from(User)).scalar_one() or 0)
        active = int(db.execute(
            select(func.count()).select_from(User).where(User.is_active.is_(True))
        ).scalar_one() or 0)
        by_role = {
            str(r or ""): int(c or 0)
            for r, c in db.execute(select(User.role, func.count()).group_by(User.role)).all()
        }
        teams = int(db.execute(select(func.count()).select_from(Team)).scalar_one() or 0)
    return {"users": total, "active_users": active, "by_role": by_role, "teams": teams}


__all__ = [
    "DEFAULT_PASSWORDS",
    "MIN_PASSWORD_LENGTH",
    "VALID_ROLES",
    "UserError",
    "authenticate",
    "create_team",
    "create_user",
    "deactivate",
    "delete_team",
    "get_by_username",
    "get_team",
    "get_user",
    "list_teams",
    "list_users",
    "reactivate",
    "seed_defaults",
    "stats",
    "team_to_dict",
    "update_team",
    "update_user",
    "user_to_dict",
]
