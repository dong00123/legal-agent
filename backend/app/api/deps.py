"""接口层公共依赖：登录主体、客户端识别、分页、业务异常到 HTTP 的映射。

三条贯穿全接口层的约定：

1. **业务异常统一在这里转 HTTP。** 服务层抛 ``ValueError`` 子类
   （``UserError`` / ``CaseError`` / ``DraftError`` …），接口层不逐个 try/except，
   而是在 ``main.py`` 注册异常处理器。否则每个路由都要写一遍
   ``except XxxError: raise HTTPException(400, ...)``，漏一个就变成 500。

2. **客户端标识从请求头取，不从查询串取。** ``X-Client: exe|web``。
   审计与任务列表都要按它区分（方案 FR13 双端任务互通），
   而查询串里的来源是用户可随便改的，写进审计就没有可信度。

3. **"可选登录"与"必须登录"是两个依赖，不共用一个。**
   ``get_viewer`` 返回 ``None`` 而不是抛错 —— 它只用于极少数确实允许匿名的场景
   （健康检查等）。业务接口一律用 ``require_user``。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Query, Request, status

from backend.app.config import settings
from backend.app.security import (
    CurrentUser,
    get_current_user,
    get_optional_user,
    require_admin,
)

logger = logging.getLogger("legal_agent.api")

# 客户端标识白名单。``api`` 是兜底：脚本、curl、MCP 调用方都不会带 X-Client。
KNOWN_CLIENTS = ("exe", "web", "api", "worker", "mcp")


# --------------------------------------------------------------------------- 主体
# 三个依赖，语义各不相同，**不要合并**：
#   require_user —— 必须登录（绝大多数业务接口）
#   require_admin —— 必须管理员（管理后台全部接口）
#   optional_user —— 允许匿名（只有极少数场景；返回 None 而不是抛错，
#                    调用方必须显式处理 None，不能假设一定有主体）
#
# 直接复用 ``security`` 里的实现，不在这里再写一遍 —— 鉴权规则写两份必然分叉，
# 而分叉的那一份就是越权入口。
RequireUser = Annotated[CurrentUser, Depends(get_current_user)]
RequireAdmin = Annotated[CurrentUser, Depends(require_admin)]
OptionalUser = Annotated[CurrentUser | None, Depends(get_optional_user)]


# --------------------------------------------------------------------------- 请求元信息
def client_of(
    x_client: Annotated[str | None, Header(alias="X-Client")] = None,
    user_agent: Annotated[str | None, Header()] = None,
) -> str:
    """客户端标识：``exe`` / ``web`` / ``api``。"""
    value = (x_client or "").strip().lower()
    if value in KNOWN_CLIENTS:
        return value
    ua = (user_agent or "").lower()
    if "legal-agent-exe" in ua:
        return "exe"
    if "mozilla" in ua:
        return "web"
    return "api"


ClientTag = Annotated[str, Depends(client_of)]


def client_ip(request: Request) -> str:
    """取真实来源 IP。

    只在**确实有反向代理**（部署里默认走 Nginx）时才信 ``X-Forwarded-For``，
    否则任何人都能伪造审计里的 IP。判定依据是配置里是否声明了代理 ——
    这里用 ``TRUST_PROXY_HEADERS`` 环境变量控制，默认关。
    取 XFF 的第一个地址（最接近客户端的那个）。
    """
    if getattr(settings, "TRUST_PROXY_HEADERS", False):
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
    real = request.headers.get("x-real-ip", "")
    if real and getattr(settings, "TRUST_PROXY_HEADERS", False):
        return real.strip()
    return request.client.host if request.client else ""


class RequestMeta:
    """请求元信息（审计与任务记录共用）。"""

    def __init__(self, request: Request, client: str) -> None:
        self.client = client
        self.ip = client_ip(request)
        self.ua = request.headers.get("user-agent", "")[:200]
        self.request_id = request.headers.get("x-request-id", "")

    def as_dict(self) -> dict[str, Any]:
        return {"client": self.client, "ip": self.ip, "user_agent": self.ua,
                "request_id": self.request_id}


def request_meta(request: Request, client: ClientTag) -> RequestMeta:
    return RequestMeta(request, client)


Meta = Annotated[RequestMeta, Depends(request_meta)]


# --------------------------------------------------------------------------- 分页
class Pagination:
    """分页参数。``limit`` 上限 200 —— 一次拉几千条会把响应体和前端一起拖垮。"""

    def __init__(
        self,
        limit: Annotated[int, Query(ge=1, le=200, description="每页条数")] = 10,
        offset: Annotated[int, Query(ge=0, description="偏移量")] = 0,
    ) -> None:
        self.limit = limit
        self.offset = offset

    def as_dict(self) -> dict[str, int]:
        return {"limit": self.limit, "offset": self.offset}


Page = Annotated[Pagination, Depends(Pagination)]


def apply_page_params(
    page: Pagination,
    page_number: int | None = None,
    page_size: int | None = None,
) -> tuple[int, int]:
    """页码式分页适配：把 ``page/page_size`` 换算成 ``(limit, offset)``。

    - 传了 ``page_number`` 或 ``page_size`` 任一 → 按页码式计算：
      ``limit = page_size``（缺省取 ``page.limit``），``offset = (page_number - 1) * limit``。
    - 两者均为 ``None`` → 保持旧的 ``limit/offset`` 语义，旧调用方零改动。
    """
    if page_number is None and page_size is None:
        return page.limit, page.offset
    pn = max(1, int(page_number or 1))
    ps = max(1, min(int(page_size or page.limit), 200))
    return ps, (pn - 1) * ps


def effective_page(page: Pagination, limit: int, offset: int) -> Pagination:
    """用实际生效的 ``limit/offset`` 构造分页对象，供 ``page_of`` 计算 ``has_more``。"""
    return Pagination(limit=limit, offset=offset)


def page_of(items: list[Any], page: Pagination, *, total: int | None = None) -> dict[str, Any]:
    """统一的分页响应体。

    ``total`` 未知时按"本页已满即可能还有更多"报 ``has_more``，
    而不是写死 ``False`` —— 后者会让前端在第一页就不再翻页。
    """
    return {
        "items": items,
        "limit": page.limit,
        "offset": page.offset,
        "count": len(items),
        "total": total if total is not None else None,
        "has_more": (len(items) >= page.limit) if total is None else (page.offset + len(items) < total),
    }


# --------------------------------------------------------------------------- 异常
def not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


__all__ = [
    "KNOWN_CLIENTS",
    "ClientTag",
    "CurrentUser",
    "Meta",
    "OptionalUser",
    "Page",
    "Pagination",
    "RequestMeta",
    "RequireAdmin",
    "RequireUser",
    "bad_request",
    "client_ip",
    "client_of",
    "forbidden",
    "not_found",
    "apply_page_params",
    "effective_page",
    "page_of",
]
