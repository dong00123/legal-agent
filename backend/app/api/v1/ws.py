"""WebSocket：任务进度实时推送。

与 SSE 的分工：SSE 用于"发起并订阅"（只能服务端往客户端推），
WebSocket 用于**双向**场景 —— 前端可以直接发 ``{"action": "cancel"}`` 或
``{"action": "ping"}``，不必为每个控制指令再开一个 HTTP 接口。

三件必须说清的事：

1. **浏览器无法在 WebSocket 握手上带自定义请求头**，所以令牌走查询串
   （``?token=...``）或握手后的第一条消息。查询串里的令牌会进 Nginx 访问日志 ——
   这是 WebSocket 鉴权的固有取舍，部署时应把 access_log 里的 token 参数脱敏。
   这里两种方式都支持，优先用第一条消息（不进日志）。

2. **鉴权在握手后立刻做，失败即断开。** 不在"连接成功、之后每条消息再校验"——
   那样未授权连接会一直占着资源。

3. **多个客户端看同一个任务要能各自收到。** 用进程内订阅表广播。
   **跨进程（worker 跑的任务）收不到内存事件** —— 那种情况退回轮询落库快照，
   与 ``/tasks/{id}/stream`` 的做法一致。这里通过周期性补发快照兜底，
   所以即使事件全丢，进度也不会卡住不涨。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from backend.app.services.event_bus import event_bus

# 与 ``tasks.py`` 同一条约定：要的是 ``TaskService`` 实例，不是同名模块。
# 详见 ``tasks.py`` 里那段注释。
from backend.app.services.task_service import task_service

logger = logging.getLogger("legal_agent.api.ws")

router = APIRouter()

# 订阅表与发布逻辑在 ``services/event_bus.py``。
# **不在这里再实现一份**：orchestrator（服务层）也要往同一个总线上发布，
# 各写一份的话，两边各有一张订阅表，事件永远送不到对方那边。


async def publish(task_id: str, event: str, data: dict[str, Any]) -> int:
    """转发到事件总线（保持向后兼容的模块级函数）。"""
    return await event_bus.publish(task_id, event, data)


async def _subscribe(task_id: str, ws: WebSocket) -> None:
    await event_bus.subscribe(task_id, ws)


async def _unsubscribe(task_id: str, ws: WebSocket) -> None:
    await event_bus.unsubscribe(task_id, ws)


def _user_from_token(token: str) -> Any:
    """用令牌换登录用户。失败返回 ``None``。"""
    if not token:
        return None
    from backend.app.db.session import session_scope
    from backend.app.security import CurrentUser, decode_token
    from sqlalchemy import select

    from backend.app.db.models import User

    try:
        payload = decode_token(token)
    except Exception:  # noqa: BLE001
        return None
    try:
        with session_scope() as db:
            row = db.execute(
                select(User).where(User.id == payload.get("uid"))
            ).scalar_one_or_none()
            if row is None or not row.is_active:
                return None
            return CurrentUser(row.id, row.username, row.role, row.team_id, row.full_name)
    except Exception as exc:  # noqa: BLE001
        logger.debug("WS 令牌解析失败：%s", exc)
        return None


def _can_watch(user: Any, row: dict[str, Any]) -> bool:
    """与 ``tasks._assert_task_access`` 同一条规则（管理员 / 本人 / 本团队）。

    重复写一遍是有意的取舍：WebSocket 里抛 ``HTTPException`` 拿不到合适的
    握手响应，只能在握手后发一条错误消息再断开，所以判定逻辑无法复用
    HTTP 依赖。**规则本身仍然只有一条**（``security.can_access_case`` 用于案件，
    这里用于任务），改动时两处必须同步 —— 见下方 ``_assert_task_access``
    在 tasks.py 里的注释。
    """
    if user.is_admin:
        return True
    if row.get("user_id") is not None and int(row["user_id"]) == int(user.id):
        return True
    tid = row.get("team_id")
    return bool(tid is not None and user.team_id is not None and int(tid) == int(user.team_id))


@router.websocket("/ws/tasks/{task_id}")
async def ws_task_progress(
    websocket: WebSocket,
    task_id: str,
    token: Annotated[str, Query(description="JWT；也可在握手后首条消息里传")] = "",
) -> None:
    """订阅任务进度。

    客户端协议::

        客户端 → {"action": "subscribe", "token": "<JWT>"}   # 未在查询串带 token 时
        客户端 → {"action": "ping"}
        客户端 → {"action": "snapshot"}
        服务端 → {"event": "progress"|"done"|"error"|"pong"|"snapshot", "data": {...}}

    从未鉴权到鉴权完成之前，**只接受 ``subscribe`` 一条消息**，其余一律断开。
    """
    await websocket.accept()

    user = _user_from_token(token)
    if user is None:
        # 首条消息里可能带令牌（不进访问日志）
        try:
            first = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
            msg = json.loads(first)
            if msg.get("action") != "subscribe":
                await _close(websocket, "未授权：请先发送 subscribe 消息")
                return
            user = _user_from_token(str(msg.get("token") or ""))
        except (asyncio.TimeoutError, json.JSONDecodeError, WebSocketDisconnect):
            await _close(websocket, "未授权：缺少令牌")
            return

    if user is None:
        await _close(websocket, "未授权：令牌无效或已过期")
        return

    row = task_service.get(task_id)
    if row is None:
        await _close(websocket, f"任务不存在：{task_id}")
        return
    if not _can_watch(user, row):
        await _close(websocket, "无权查看该任务")
        return

    await _subscribe(task_id, websocket)
    logger.info("WS 订阅任务进度：%s（%s）", task_id, user.username)

    try:
        await websocket.send_text(json.dumps({
            "event": "snapshot",
            "data": {
                "task_id": task_id, "status": row["status"], "progress": row["progress"],
                "intent": row.get("intent"), "confidence": row.get("confidence"),
                "citation_check": row.get("citation_check"),
                "trace_count": len(row.get("trace") or []),
                "evidence_gap": row.get("evidence_gap") or [],
            },
        }, ensure_ascii=False))

        # 兜底轮询：内存事件只在本进程有效，worker 进程跑的任务发不过来。
        # 定时补发落库快照，保证进度一定在动 —— 事件驱动是优化，快照是保底。
        poller = asyncio.create_task(_poll(task_id, websocket))

        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.send_text(json.dumps(
                        {"event": "error", "data": {"error": "消息必须是 JSON"}},
                        ensure_ascii=False))
                    continue
                action = str(msg.get("action") or "")
                if action == "ping":
                    await websocket.send_text(json.dumps(
                        {"event": "pong", "data": {}}, ensure_ascii=False))
                elif action == "snapshot":
                    current = task_service.get(task_id) or {}
                    await websocket.send_text(json.dumps({
                        "event": "snapshot",
                        "data": {
                            "task_id": task_id, "status": current.get("status"),
                            "progress": current.get("progress"),
                            "confidence": current.get("confidence"),
                            "citation_check": current.get("citation_check"),
                            "trace_count": len(current.get("trace") or []),
                        },
                    }, ensure_ascii=False))
                elif action == "cancel":
                    # 与 SSE 同一取舍：**不真正中断任务**（见 chat.py 的说明）。
                    # 只把"用户已离开/不需要了"这件事记下来，任务继续跑完并落库。
                    logger.info("WS 收到取消请求（任务继续执行并落库）：%s", task_id)
                    await websocket.send_text(json.dumps({
                        "event": "info",
                        "data": {"note": "服务端不中断已开始的 Agent 流程，任务会继续执行并落库，"
                                         "可在任务中心查看结果"},
                    }, ensure_ascii=False))
                else:
                    await websocket.send_text(json.dumps(
                        {"event": "error",
                         "data": {"error": f"未知 action：{action}；支持 ping / snapshot / cancel"}},
                        ensure_ascii=False))
        finally:
            poller.cancel()
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("WS 任务订阅异常：%s | %s", task_id, exc)
    finally:
        await _unsubscribe(task_id, websocket)
        logger.info("WS 退订任务进度：%s", task_id)


async def _poll(task_id: str, websocket: WebSocket, interval: float = 2.0) -> None:
    """定期补发落库快照（跨进程兜底）。"""
    last = (-1, -1)
    try:
        while True:
            await asyncio.sleep(interval)
            row = task_service.get(task_id)
            if row is None:
                await websocket.send_text(json.dumps(
                    {"event": "error", "data": {"error": "任务已被清理"}},
                    ensure_ascii=False))
                return
            marker = (int(row.get("progress") or 0), len(row.get("trace") or []))
            if marker != last:
                last = marker
                await websocket.send_text(json.dumps({
                    "event": "progress",
                    "data": {
                        "task_id": task_id, "status": row["status"],
                        "progress": row["progress"], "confidence": row.get("confidence"),
                        "citation_check": row.get("citation_check"),
                        "trace_count": len(row.get("trace") or []),
                    },
                }, ensure_ascii=False))
            if row["status"] in ("succeeded", "degraded", "failed"):
                await websocket.send_text(json.dumps({
                    "event": "done",
                    "data": {"task_id": task_id, "status": row["status"],
                             "confidence": row.get("confidence"),
                             "citation_check": row.get("citation_check"),
                             "elapsed_ms": row.get("elapsed_ms"),
                             "error": row.get("error") or ""},
                }, ensure_ascii=False))
                return
    except (asyncio.CancelledError, WebSocketDisconnect):
        raise
    except Exception as exc:  # noqa: BLE001
        logger.debug("WS 轮询结束：%s | %s", task_id, exc)


async def _close(websocket: WebSocket, reason: str) -> None:
    """带着原因关闭连接。**必须给原因** —— 只看到 4401 而不知道是"令牌过期"
    还是"没权限"，前端只能弹一句"连接失败"。"""
    try:
        await websocket.send_text(json.dumps({"event": "error", "data": {"error": reason}},
                                             ensure_ascii=False))
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason=reason[:120])
    except Exception:  # noqa: BLE001
        pass
