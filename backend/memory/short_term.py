"""短期记忆：Redis（TTL 24h），不可用时降级为进程内 TTL 字典。

方案 9.1：Redis 存 task_id、对话、结构化案件要素、工具中间结果，TTL 24h，
任务结束即过期。

降级说明（必须在启动日志可见）：Redis 不可用时用进程内字典兜底，
功能等价但**只在单进程内有效** —— 多 worker 部署下任务会"看不到"另一个
worker 写的短期记忆。所以降级时会在 /health 里明确标注 ``degraded=True``，
避免出现"以为共享了其实是各存各的"这种静默失真。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from backend.app.config import settings

logger = logging.getLogger("legal_agent.memory.short_term")

DEFAULT_TTL = 24 * 3600


class ShortTermMemory:
    """任务级短期记忆。"""

    def __init__(self) -> None:
        self._client: Any = None
        self._memory: dict[str, tuple[float, str]] = {}
        self._backend = "memory"
        self._last_error = ""
        self._init_backend()

    # ------------------------------------------------------------------ 后端
    def _init_backend(self) -> None:
        if not settings.REDIS_ENABLED:
            logger.warning("短期记忆：Redis 未启用（REDIS_ENABLED=false），使用进程内 TTL 字典")
            return
        try:
            import redis

            client = redis.from_url(settings.REDIS_URL, decode_responses=True,
                                    socket_connect_timeout=2, socket_timeout=2)
            client.ping()
            self._client = client
            self._backend = "redis"
            logger.info("短期记忆：Redis 已启用 %s", settings.REDIS_URL)
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            logger.warning("短期记忆：Redis 连接失败，降级为进程内字典：%s", exc)

    @property
    def backend(self) -> str:
        return self._backend

    def _key(self, task_id: str, kind: str = "") -> str:
        return f"legal:{kind}:{task_id}" if kind else f"legal:task:{task_id}"

    # ------------------------------------------------------------------ 读写
    def set(self, task_id: str, value: Any, *, ttl: int = DEFAULT_TTL, kind: str = "") -> bool:
        payload = json.dumps(value, ensure_ascii=False, default=str)
        key = self._key(task_id, kind)
        if self._client is not None:
            try:
                self._client.setex(key, ttl, payload)
                return True
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                logger.warning("Redis 写入失败，本次改用进程内字典：%s", exc)
        self._memory[key] = (time.time() + ttl, payload)
        return True

    def get(self, task_id: str, *, kind: str = "", default: Any = None) -> Any:
        key = self._key(task_id, kind)
        raw: str | None = None
        if self._client is not None:
            try:
                raw = self._client.get(key)
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
        if raw is None:
            entry = self._memory.get(key)
            if entry is None:
                return default
            expire_at, value = entry
            if expire_at < time.time():
                self._memory.pop(key, None)
                return default
            raw = value
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default

    def delete(self, task_id: str, *, kind: str = "") -> None:
        key = self._key(task_id, kind)
        if self._client is not None:
            try:
                self._client.delete(key)
            except Exception:  # noqa: BLE001
                pass
        self._memory.pop(key, None)

    # ------------------------------------------------------------------ 语义封装
    def append_message(self, task_id: str, role: str, content: str) -> None:
        """追加一轮对话。"""
        history = self.get(task_id, kind="messages", default=[]) or []
        history.append({"role": role, "content": content, "ts": time.time()})
        self.set(task_id, history, kind="messages")

    def messages(self, task_id: str) -> list[dict[str, Any]]:
        return self.get(task_id, kind="messages", default=[]) or []

    def put_tool_cache(self, task_id: str, tool: str, args_key: str, value: Any) -> None:
        """工具中间结果缓存（同一任务内同样的检索不重复打库）。"""
        cache = self.get(task_id, kind="tools", default={}) or {}
        cache[f"{tool}::{args_key}"] = value
        self.set(task_id, cache, kind="tools")

    def get_tool_cache(self, task_id: str, tool: str, args_key: str) -> Any:
        cache = self.get(task_id, kind="tools", default={}) or {}
        return cache.get(f"{tool}::{args_key}")

    # ------------------------------------------------------------------ 清理
    def purge_expired(self) -> int:
        now = time.time()
        dead = [k for k, (exp, _) in self._memory.items() if exp < now]
        for k in dead:
            self._memory.pop(k, None)
        return len(dead)

    def status(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "backend": self._backend,
            "degraded": self._backend != "redis",
            "local_keys": len(self._memory),
            "ttl_hours": DEFAULT_TTL // 3600,
        }
        if self._backend == "redis" and self._client is not None:
            try:
                info["redis_keys"] = sum(
                    1 for _ in self._client.scan_iter(match="legal:*", count=200)
                )
            except Exception as exc:  # noqa: BLE001
                info["redis_error"] = str(exc)
        if self._last_error:
            info["last_error"] = self._last_error
        if info["degraded"]:
            info["note"] = ("短期记忆运行在进程内字典上：多 worker 部署时各 worker 不共享，"
                            "仅适用于单进程/开发环境")
        return info


short_term_memory = ShortTermMemory()
