"""Harness Trace：全链路追踪。

方案 4.4 节 Harness 的 Trace 组件。目标是**可追溯到具体检索来源**，
满足律师执业责任追溯要求：谁、在哪个节点、调了什么工具、拿到什么、花了多久。

与 Audit 的分工：
- Trace 面向**调试与排障**（细粒度、含耗时、可丢弃）；
- Audit 面向**合规留痕**（不可删改、落库、含主体与访问对象）。
两者都写，但用途不同，不能互相替代。
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from backend.agents.state import TraceEvent

logger = logging.getLogger("legal_agent.harness.trace")

# 监听器签名：收到一条事件就返回。**必须是同步的、且要快** ——
# 它跑在节点执行的调用栈里，慢监听器会拖慢整条流水线。
TraceListener = Callable[[TraceEvent], None]


class Tracer:
    """一次任务运行的 Trace 收集器。"""

    def __init__(self, trace_id: str = "", task_id: str = "") -> None:
        self.trace_id = trace_id
        self.task_id = task_id
        self.events: list[TraceEvent] = []
        self._t0 = time.perf_counter()
        self._listeners: list[TraceListener] = []

    # ------------------------------------------------------------------ 订阅
    def subscribe(self, listener: TraceListener) -> None:
        """订阅事件流。SSE 进度推送靠它 —— 不需要轮询数据库。

        为什么要订阅而不是让前端轮询 ``/tasks/{id}``：
        一次合同审查要跑十几秒，轮询要么频繁（打库）要么迟钝（体验差）；
        而事件本身就在 ``add()`` 里天然产生，顺手推出去是零成本的。
        """
        self._listeners.append(listener)

    def unsubscribe(self, listener: TraceListener) -> None:
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    # ------------------------------------------------------------------ 写入
    def add(self, node: str, event: str, detail: str = "",
            level: str = "info", elapsed_ms: int = 0) -> TraceEvent:
        ev = TraceEvent(node=node, event=event, detail=detail,
                        level=level, elapsed_ms=elapsed_ms)
        self.events.append(ev)
        if level in ("warning", "error"):
            logger.warning("[%s] %s.%s %s", self.trace_id, node, event, detail)
        else:
            logger.debug("[%s] %s.%s %s", self.trace_id, node, event, detail)
        # 通知订阅者。**异常必须吞掉**：监听器是 SSE 推送这类外围设施，
        # 它炸了不能让业务节点跟着炸 —— 那会把"进度条断了"升级成"任务失败"。
        for listener in list(self._listeners):
            try:
                listener(ev)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Trace 监听器异常（已忽略）：%s", exc)
        return ev

    def info(self, node: str, event: str, detail: str = "") -> TraceEvent:
        return self.add(node, event, detail, "info")

    def warn(self, node: str, event: str, detail: str = "") -> TraceEvent:
        return self.add(node, event, detail, "warning")

    def error(self, node: str, event: str, detail: str = "") -> TraceEvent:
        return self.add(node, event, detail, "error")

    # ------------------------------------------------------------------ 计时
    @contextmanager
    def stage(self, node: str, event: str, detail: str = "") -> Iterator[dict[str, Any]]:
        """节点计时上下文：``with tracer.stage("StatuteNode", "statute_search") as s: ...``

        退出时自动补一条带 ``elapsed_ms`` 的 Trace 事件；
        ``s["result"]`` 可回填结论摘要，异常时 level 自动升级为 error。
        """
        started = time.perf_counter()
        box: dict[str, Any] = {"result": ""}
        try:
            yield box
        except Exception as exc:  # noqa: BLE001
            ms = int((time.perf_counter() - started) * 1000)
            self.add(node, event, f"{detail} 异常：{type(exc).__name__}: {exc}".strip(),
                     "error", ms)
            raise
        ms = int((time.perf_counter() - started) * 1000)
        text = f"{detail} {box['result']}".strip()
        self.add(node, event, text, "info", ms)

    # ------------------------------------------------------------------ 读取
    def to_list(self) -> list[dict[str, Any]]:
        return [e.model_dump(mode="json") for e in self.events]

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self._t0) * 1000)

    def node_timings(self) -> dict[str, int]:
        """按节点聚合耗时，供管理后台「各 Agent 耗时分布」统计。"""
        out: dict[str, int] = {}
        for e in self.events:
            if e.elapsed_ms:
                out[e.node] = out.get(e.node, 0) + e.elapsed_ms
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def errors(self) -> list[dict[str, Any]]:
        return [e.model_dump(mode="json") for e in self.events if e.level == "error"]

    def summary(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "task_id": self.task_id,
            "events": len(self.events),
            "errors": len(self.errors()),
            "elapsed_ms": self.elapsed_ms(),
            "node_timings": self.node_timings(),
        }
