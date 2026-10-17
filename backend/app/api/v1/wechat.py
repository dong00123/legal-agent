"""微信机器人接入接口（2026-10-11）。

两类调用方，**鉴权方式刻意不同**：

- ``/wechat/bind-code`` 等由**律师本人**在 Web 工作台点，走普通 JWT
  （``RequireUser``）。绑定码是"我本人授权某个微信号以我的名义办事"的凭证，
  只有本人能取。
- ``/wechat/events`` 由**微信机器人服务**调用，走共享密钥（``X-Bridge-Token``）。
  机器人不是任何一位律师，给它签发 JWT 等于给它一个万能身份；
  而真正以谁的名义办事由 ``wechat_service`` 里的绑定关系决定。

这道桥的边界很清楚：机器人只负责"谁发的、说了什么、带了什么附件"，
所有法律判定（案件归属、诉讼/非诉准入、材料登记）都在服务层，
与 Web 工作台共用同一套实现。

**未开启时接口返回 503 而不是 404。** 404 会让人以为路径写错了，
而真正的原因是 ``WECHAT_ENABLED=false`` 或缺 ``WECHAT_BRIDGE_TOKEN`` ——
这两种"配了没生效"正是排查时最费时间的一类。
"""

from __future__ import annotations

import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from backend.app.api.deps import RequireUser
from backend.app.config import settings
from backend.app.services import wechat_service

router = APIRouter(prefix="/wechat", tags=["微信机器人接入"])

# 单次事件里附件的 base64 体量上限（字节）。比 ``WECHAT_MAX_ATTACHMENT_MB``
# 宽松一档：那个是"单文件"上限，这里是"整条消息"上限，
# 挡的是"一次塞二十张原图把两端内存一起顶穿"。
_MAX_BODY_CHARS = 48 * 1024 * 1024


def require_bridge(
    x_bridge_token: Annotated[str | None, Header(alias="X-Bridge-Token")] = None,
) -> None:
    """机器人桥接鉴权。**三项都过才算通过**，任一项不过都明确说明原因。"""
    if not settings.WECHAT_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="微信接入未开启：请在后端设置 WECHAT_ENABLED=true",
        )
    expected = (settings.WECHAT_BRIDGE_TOKEN or "").strip()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="微信接入缺少 WECHAT_BRIDGE_TOKEN，接口已关闭（不设空密钥放行）",
        )
    if not x_bridge_token or not secrets.compare_digest(x_bridge_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="桥接密钥无效",
        )


BridgeAuth = Annotated[Any, Depends(require_bridge)]


# --------------------------------------------------------------------------- 模型
class WechatAttachment(BaseModel):
    """一个附件。``data_base64`` 是原始字节的 base64（可带 data: 前缀）。"""

    filename: str = Field("", max_length=256, description="文件名（可为空）")
    file_name: str = Field("", max_length=256, description="兼容字段：微信侧文件名")
    mime: str = Field("", max_length=128, description="MIME，用于没有文件名时推后缀")
    data_base64: str = Field("", description="原始字节的 base64 编码")


class WechatEvent(BaseModel):
    """一条入站事件。"""

    peer_id: str = Field(..., min_length=1, max_length=128, description="微信用户 id")
    bot_id: str = Field("", max_length=128, description="机器人账号 id")
    display_name: str = Field("", max_length=128, description="微信昵称（展示用）")
    # 给得比微信单条上限宽一档：超出这个长度的多半是机器人拼进来的上下文，
    # 而 422 在机器人侧的兜底是"落回本机对话链路"，不是我们想要的结果。
    text: str = Field("", max_length=8000, description="文字内容")
    attachments: list[WechatAttachment] = Field(default_factory=list)


# --------------------------------------------------------------------------- 律师侧
@router.get("/status", summary="微信接入状态与我的绑定")
def wechat_status(user: RequireUser) -> dict[str, Any]:
    """Web 工作台用：功能开没开、我绑了哪些微信号。"""
    return {
        "enabled": bool(settings.WECHAT_ENABLED),
        "ready": settings.wechat_ready,
        "bind_code_ttl_minutes": max(1, int(settings.WECHAT_BIND_CODE_TTL_MINUTES)),
        "max_attachment_mb": max(1, int(settings.WECHAT_MAX_ATTACHMENT_MB)),
        "bindings": wechat_service.list_bindings(user),
    }


@router.post("/bind-code", summary="签发微信绑定码")
def create_bind_code(user: RequireUser) -> dict[str, Any]:
    """取一个一次性绑定码：在微信里发 ``/绑定 <码>`` 即完成绑定。

    绑定码是**授权凭证**，所以只由本人取、只生效一次、有有效期。
    """
    if not settings.WECHAT_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="微信接入未开启：请在后端设置 WECHAT_ENABLED=true",
        )
    return wechat_service.issue_bind_code(user)


@router.delete("/bindings/{peer_id}", summary="解除某个微信号的绑定")
def delete_binding(peer_id: str, user: RequireUser) -> dict[str, Any]:
    """律师在 Web 端撤销某个微信号的授权（手机丢了、离职交接）。"""
    bindings = wechat_service.list_bindings(user)
    if not any(b["peer_id"] == peer_id for b in bindings):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="该微信号未绑定到当前账号")
    return wechat_service.unbind_peer(peer_id)


# --------------------------------------------------------------------------- 机器人侧
@router.post("/events", summary="微信入站事件（机器人调用）")
async def wechat_events(payload: WechatEvent, _: BridgeAuth) -> dict[str, Any]:
    """机器人转发进来的入站消息。

    返回 ``{"handled": bool, "replies": [str]}``：
    - ``handled=false`` —— 这条不是给法律助手的（既不是命令也没有附件），
      机器人应把它交回自己的对话链路；
    - ``replies`` —— 要按顺序发回微信的几句话。
    """
    body_chars = sum(len(a.data_base64 or "") for a in payload.attachments)
    if body_chars > _MAX_BODY_CHARS:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"附件总量过大（{body_chars // (1024 * 1024)}MB），请分开几次发送",
        )
    return await wechat_service.handle_event(
        peer_id=payload.peer_id,
        bot_id=payload.bot_id,
        text=payload.text,
        attachments=[a.model_dump() for a in payload.attachments],
        display_name=payload.display_name,
    )
