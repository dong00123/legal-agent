"""鉴权接口：登录、登出、当前用户、改口令、团队下拉。

双端（EXE / Web）共用同一套鉴权服务（方案 12.4）。登录成功返回 JWT，
EXE 端把它存在内存里、Web 端存在 sessionStorage —— **两端都不落盘**。

几个刻意的做法：

1. **登录失败一律返回同一句话。** 不区分"用户不存在"与"口令错误"。
   分开返回等于给了一个免费的账号枚举接口 —— 攻击者能先摸清有哪些账号，
   再针对性地爆破。服务层已经在内部对不存在的用户跑一次等价散列运算，
   把响应时间差也抹平了（见 ``user_service._dummy_verify``）。

2. **登录、登出、改口令全部写审计。** 方案第十四章的审计要求覆盖
   "材料访问、Agent 检索与引用"，而账号侧的事件同样是执业责任追溯的一部分
   （谁在什么时候从哪台机器登录过）。

3. **改口令必须验原口令。** 只验新口令会让"令牌泄露"直接升级为"账号被完全接管"——
   攻击者拿到令牌后改掉口令，真正的律师就被锁在外面了。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, status
from pydantic import BaseModel, Field

from backend.app.api.deps import Meta, RequireUser
from backend.app.security import create_access_token, verify_password
from backend.app.services import user_service

logger = logging.getLogger("legal_agent.api.auth")

router = APIRouter(prefix="/auth", tags=["鉴权"])


# --------------------------------------------------------------------------- 模型
class LoginRequest(BaseModel):
    username: str = Field(..., description="登录名")
    password: str = Field(..., description="口令")


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: dict[str, Any]


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., description="原口令")
    new_password: str = Field(..., min_length=user_service.MIN_PASSWORD_LENGTH,
                              description=f"新口令（至少 {user_service.MIN_PASSWORD_LENGTH} 位）")


# --------------------------------------------------------------------------- 登录
@router.post("/login", response_model=LoginResponse, summary="登录")
def login(payload: LoginRequest, meta: Meta) -> LoginResponse:
    """口令登录，返回 JWT。

    ``admin`` / ``lawyer`` 由 ``.env`` 的种子配置决定，首次启动自动创建
    （生产环境下若口令仍是出厂值会拒绝创建，见 ``user_service.seed_defaults``）。
    """
    from backend.app.config import settings
    from backend.agents.harness.audit import audit_writer

    result = user_service.authenticate(payload.username, payload.password)

    if result is None:
        audit_writer.record("auth.login", username=payload.username,
                            client=meta.client, ip=meta.ip, result="denied",
                            detail="登录失败：账号或口令不正确")
        logger.warning("登录失败：%s（来自 %s）", payload.username, meta.ip or "unknown")
        # 与"用户不存在"完全同一句措辞 —— 见模块 docstring 第 1 条
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="账号或口令不正确")

    if result.get("_inactive"):
        audit_writer.record("auth.login", username=payload.username,
                            client=meta.client, ip=meta.ip, result="denied",
                            detail="登录被拒：账号已停用")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="账号已停用，请联系管理员")

    user = result
    token = create_access_token(
        user["username"], user_id=user["id"], role=user["role"], team_id=user["team_id"],
    )
    audit_writer.record("auth.login", username=user["username"], user_id=user["id"],
                        team_id=user["team_id"], client=meta.client, ip=meta.ip,
                        resource="session", result="ok",
                        detail=f"登录成功（角色 {user['role']}）")
    logger.info("登录成功：%s（%s，来自 %s）", user["username"], user["role"], meta.client)
    return LoginResponse(
        access_token=token,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=user,
    )


@router.post("/logout", summary="登出")
def logout(user: RequireUser, meta: Meta) -> dict[str, Any]:
    """登出。

    JWT 是无状态的，服务端**无法真正吊销已签发的令牌** —— 所以这里只做两件事：
    写审计、告诉客户端自己去删本地令牌。要说清这一点，
    否则会给人"登出后旧令牌立刻失效"的错觉。
    """
    from backend.agents.harness.audit import audit_writer

    audit_writer.record("auth.logout", username=user.username, user_id=user.id,
                        team_id=user.team_id, client=meta.client, ip=meta.ip,
                        resource="session", detail="客户端应清除本地令牌")
    return {
        "ok": True,
        "note": "已记录登出。JWT 为无状态令牌，服务端不维护吊销列表，"
                "请客户端自行清除本地令牌；如需强制失效请更换 SECRET_KEY 或等待令牌过期。",
    }


@router.get("/me", summary="当前用户")
def me(user: RequireUser) -> dict[str, Any]:
    """当前登录用户信息（含权限位，供前端决定是否显示管理入口）。"""
    return user.to_dict()


@router.get("/permissions", summary="当前用户权限位")
def permissions(user: RequireUser) -> dict[str, Any]:
    """权限位。前端据此控制菜单可见性 —— **服务端仍会独立校验**，
    前端隐藏不等于服务端放行。"""
    is_admin = user.is_admin
    return {
        "role": user.role,
        "is_admin": is_admin,
        "can_manage_users": is_admin,
        "can_manage_knowledge": is_admin,
        "can_manage_config": is_admin,
        "can_view_audit": is_admin,
        "can_view_stats": is_admin,
        "can_use_case": True,
        "case_scope": "all" if is_admin else ("team" if user.team_id else "self"),
        "note": "权限由服务端独立校验，前端隐藏仅用于界面简化",
    }


@router.post("/change-password", summary="修改口令")
def change_password(user: RequireUser, meta: Meta,
                    payload: Annotated[ChangePasswordRequest, Body()]) -> dict[str, Any]:
    """修改自己的口令（必须先验原口令，见模块 docstring 第 3 条）。"""
    from backend.agents.harness.audit import audit_writer

    full = user_service.get_by_username(user.username)
    if full is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="账号不存在")

    hashed = _hashed_of(user.username)
    if not verify_password(payload.old_password, hashed):
        audit_writer.record("auth.password", username=user.username, user_id=user.id,
                            client=meta.client, ip=meta.ip, result="denied",
                            detail="修改口令失败：原口令不正确")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="原口令不正确")

    if payload.old_password == payload.new_password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="新口令不能与原口令相同")

    user_service.update_user(user.id, password=payload.new_password)
    audit_writer.record("auth.password", username=user.username, user_id=user.id,
                        client=meta.client, ip=meta.ip, detail="口令已修改",
                        extra={"note": "旧令牌在过期前仍有效"})
    return {
        "ok": True,
        "note": "口令已更新。已签发的令牌在过期前仍然有效，如需立即失效请重新登录。",
    }


def _hashed_of(username: str) -> str:
    """取口令散列。**只在本模块内部使用**，不对外返回。"""
    from sqlalchemy import select

    from backend.app.db.models import User
    from backend.app.db.session import session_scope

    with session_scope() as db:
        row = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
        return row.hashed_password if row else ""


# --------------------------------------------------------------------------- 团队
@router.get("/teams", summary="团队列表（下拉用）")
def teams(user: RequireUser) -> dict[str, Any]:
    """团队列表。普通律师只看得到自己的团队 —— 让他能选到别的团队没有意义，
    而且会暴露律所的组织结构。"""
    if user.is_admin:
        return {"items": user_service.list_teams()}
    mine = [t for t in user_service.list_teams() if t["id"] == user.team_id]
    return {"items": mine}
