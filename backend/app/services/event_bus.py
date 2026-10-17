"""进程内事件总线：把任务进度广播给所有在看的人。

**为什么要有这么一层**，而不是让 orchestrator 直接调 WebSocket：

- 依赖方向。``services`` 是最底层，``api`` 依赖它；反过来让 orchestrator
  去 import WebSocket 就形成了环，而且会让"跑一次任务"这件事在没有 HTTP
  的环境（worker 进程、脚本、测试）里也需要一个 WebSocket 才能工作。
- 一处发布、多处消费。同一个任务可能同时被 SSE 连接（发起者）
  和若干 WebSocket 连接（同事围观、EXE 与 Web 双端同开）看着。

**能力边界必须说清：这是进程内总线，不跨进程。**
RabbitMQ 未启用时，worker 进程里跑的任务事件发不到 API 进程的订阅者。
所以订阅方（见 ``api/v1/ws.py``）必须还有一层"定时读落库快照"的兜底 ——
事件驱动是优化，落库快照是保底。把这件事写在这里，是为了避免以后有人
"把兜底轮询删掉，因为已经有事件总线了"。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol

logger = logging.getLogger("legal_agent.services.event_bus")


class Subscriber(Protocol):
    """订阅者协议：任何有 ``send_text`` 的对象都能订阅。

    用 Protocol 而不是绑死 ``WebSocket``，是为了让测试可以传一个假对象进来
    （不需要真的起一个 WebSocket 服务就能验证"发布有没有送到"）。
    """

    async def send_text(self, data: str) -> None: ...


def _key(task_id: str, event: str = "*") -> str:
    """订阅键。``event="*"`` 表示订阅该任务的全部事件。"""
    return f"{task_id}|{event}"


class EventBus:
    """极简发布/订阅。"""

    def __init__(self) -> None:
        self._subs: dict[str, set[Subscriber]] = {}
        self._lock = asyncio.Lock()
        self._published = 0
        self._dropped = 0

    # ------------------------------------------------------------------ 订阅
    async def subscribe(self, task_id: str, sub: Subscriber, *, event: str = "*") -> None:
        async with self._lock:
            self._subs.setdefault(_key(task_id, event), set()).add(sub)
            self._subs.setdefault(_key(task_id), set()).add(sub)

    async def unsubscribe(self, task_id: str, sub: Subscriber, *, event: str = "*") -> None:
        async with self._lock:
            for k in (_key(task_id, event), _key(task_id)):
                peers = self._subs.get(k)
                if peers:
                    peers.discard(sub)
                    if not peers:
                        self._subs.pop(k, None)

    async def subscribers(self, task_id: str) -> int:
        async with self._lock:
            return len(self._subs.get(_key(task_id)) or ())

    # ------------------------------------------------------------------ 发布
    async def publish(self, task_id: str, event: str, data: dict[str, Any]) -> int:
        """广播给该任务的订阅者。返回成功送达的连接数。

        发送失败的订阅者**立刻摘除** —— 留着会让之后每次广播都多一次异常
        与超时等待，而它已经不可能再收到东西了。
        """
        async with self._lock:
            peers = set(self._subs.get(_key(task_id)) or ())
            peers |= set(self._subs.get(_key(task_id, event)) or ())
        if not peers:
            return 0

        payload = json.dumps({"event": event, "data": data}, ensure_ascii=False, default=str)
        dead: list[Subscriber] = []
        sent = 0
        for sub in peers:
            try:
                await sub.send_text(payload)
                sent += 1
            except Exception:  # noqa: BLE001
                dead.append(sub)
        self._published += 1
        if dead:
            self._dropped += len(dead)
            async with self._lock:
                for sub in dead:
                    for key, group in list(self._subs.items()):
                        group.discard(sub)
                        if not group:
                            self._subs.pop(key, None)
        return sent

    def publish_soon(self, task_id: str, event: str, data: dict[str, Any]) -> None:
        """从**同步回调**里发布（Trace 监听器就是同步的）。

        没有正在运行的事件循环时静默跳过：脚本/测试环境里没有订阅者，
        为它专门起一个循环是浪费。跳过不是错误，但计数要能看见。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            task = loop.create_task(self.publish(task_id, event, data))
            # 任务异常必须有人收，否则 asyncio 会在 GC 时打一句
            # "Task exception was never retrieved"，而上下文完全对不上
            task.add_done_callback(_swallow)
        except Exception as exc:  # noqa: BLE001
            logger.debug("事件发布失败（已忽略）：%s | %s", task_id, exc)

    def stats(self) -> dict[str, Any]:
        return {
            "published_batches": self._published,
            "dropped_subscribers": self._dropped,
            "active_tasks": len({k.split("|")[0] for k in self._subs}),
            "scope": "进程内；跨进程需靠落库快照兜底",
        }

    def reset(self) -> None:
        """测试用：清空订阅表。"""
        self._subs.clear()


def _swallow(task: asyncio.Task) -> None:
    try:
        task.result()
    except Exception:  # noqa: BLE001
        pass


event_bus = EventBus()

__all__ = ["EventBus", "Subscriber", "event_bus"]
