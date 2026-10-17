"""鉴权与口令散列。

- 口令散列：PBKDF2-SHA256（标准库实现，避免 bcrypt 的平台依赖问题）；
- 会话令牌：JWT（HS256），EXE 端与 Web 端共用同一套鉴权服务；
- 权限模型：admin（律所管理员）/ lawyer（律师），案件按「本人 / 本团队」隔离。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import jwt
from fastapi import Depends, HTTPException, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.db.models import User
from backend.app.db.session import get_db

PBKDF2_ITERATIONS = 120_000
ALGORITHM = "HS256"

# 滑动续期阈值：令牌剩余寿命低于该分钟数时，本次响应头里带回一张新令牌，
# 前端拿它续期。这样长时间开着的页面不会被 12 小时到期"突然踢回登录页"。
RENEW_THRESHOLD_MINUTES = 60

bearer_scheme = HTTPBearer(auto_error=False)

Role = Literal["admin", "lawyer"]


# --------------------------------------------------------------------------- 口令
def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode(),
    )


def verify_password(password: str, hashed: str) -> bool:
    try:
        algo, iters, salt_b64, hash_b64 = hashed.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iters))
        return hmac.compare_digest(dk, expected)
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- JWT
def create_access_token(
    subject: str,
    *,
    user_id: int,
    role: Role = "lawyer",
    team_id: int | None = None,
    expires_minutes: int | None = None,
) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=expires_minutes or settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    payload: dict[str, Any] = {
        "sub": subject,
        "uid": user_id,
        "role": role,
        "team_id": team_id,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "iss": "legal-agent",
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM], issuer="legal-agent")


# --------------------------------------------------------------------------- 依赖
class CurrentUser:
    """当前登录用户的轻量视图（避免把 ORM 对象泄漏到业务层）。"""

    def __init__(self, id: int, username: str, role: str, team_id: int | None, full_name: str = "") -> None:
        self.id = id
        self.username = username
        self.role = role
        self.team_id = team_id
        self.full_name = full_name

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "full_name": self.full_name,
            "role": self.role,
            "team_id": self.team_id,
            "is_admin": self.is_admin,
        }


def _credentials_error(detail: str = "未提供有效凭证") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    response: Response,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> CurrentUser:
    if credentials is None or not credentials.credentials:
        raise _credentials_error()
    try:
        payload = decode_token(credentials.credentials)
    except jwt.ExpiredSignatureError as exc:
        raise _credentials_error("登录已过期，请重新登录") from exc
    except jwt.PyJWTError as exc:
        raise _credentials_error("凭证无效") from exc

    user = db.execute(select(User).where(User.id == payload.get("uid"))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise _credentials_error("账号不存在或已停用")

    # 滑动续期：还剩不足 RENEW_THRESHOLD_MINUTES 时签发新令牌，经响应头下发。
    exp = payload.get("exp")
    if isinstance(exp, (int, float)):
        remain = exp - datetime.now(timezone.utc).timestamp()
        if remain < RENEW_THRESHOLD_MINUTES * 60:
            response.headers["X-Renewed-Token"] = create_access_token(
                payload.get("sub") or user.username,
                user_id=user.id,
                role=user.role if user.role in ("admin", "lawyer") else "lawyer",
                team_id=user.team_id,
            )
    return CurrentUser(user.id, user.username, user.role, user.team_id, user.full_name)


def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> CurrentUser | None:
    try:
        return get_current_user(credentials, db)
    except HTTPException:
        return None


def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


def can_access_case(user: CurrentUser, owner_id: int | None, team_id: int | None) -> bool:
    """案件访问判定：管理员全通；律师限本人 / 本团队。"""
    if user.is_admin:
        return True
    if owner_id is not None and owner_id == user.id:
        return True
    return bool(team_id is not None and user.team_id is not None and team_id == user.team_id)
