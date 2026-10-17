"""MCP Tool 基类与执行器。

对应方案第五章与第十三章：
- MCP Tool 只负责能力执行，不做复杂推理；推理与调度交给 Agent；
- 所有案件材料 Tool 做权限隔离与访问审计；
- 执行链路：Tool Call → Timeout → Retry（有限次数 + 指数退避）→ Fallback → 降级继续。

法律场景降级原则：**宁可缩小结论范围，也不假装有证据**。所以 Tool 失败时返回的
``degraded=True`` + ``fallback_note`` 会被上层写进 ``evidence_gap``，而不是静默吞掉。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from backend.app.config import settings

logger = logging.getLogger("legal_agent.tools")


@dataclass
class ToolResult:
    ok: bool = True
    tool: str = ""
    data: Any = None
    error: str = ""
    degraded: bool = False
    fallback_note: str = ""
    elapsed_ms: int = 0
    retries: int = 0
    attempts: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "tool": self.tool,
            "error": self.error,
            "degraded": self.degraded,
            "fallback_note": self.fallback_note,
            "elapsed_ms": self.elapsed_ms,
            "retries": self.retries,
        }


# --------------------------------------------------------------------------- 参数声明
_MISSING = object()

# JSON Schema 类型映射：内部简写 -> MCP 客户端认识的类型
_JSON_TYPES: dict[str, Any] = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "list": "array",
    "dict": "object",
}


def param(type_: str, desc: str = "", default: Any = _MISSING) -> dict[str, Any]:
    """声明一个工具参数。

    ``type_`` 用简写（``str``/``int``/``float``/``bool``/``list``/``dict``，
    数组元素类型写 ``list[str]``）；给了 ``default`` 即为可选参数，
    否则为必填 —— **必填/可选由是否有默认值决定，不靠人肉约定**。
    这样 ``mcp_manifest()`` 生成的 inputSchema 里的 ``required`` 数组是可信的。
    """
    d: dict[str, Any] = {"type": type_, "desc": desc}
    if default is _MISSING:
        d["required"] = True
    else:
        d["default"] = default
        d["required"] = False
    return d


def _json_type(type_: str) -> Any:
    """``list[str]`` -> ``{"type": "array", "items": {"type": "string"}}``。"""
    t = (type_ or "str").strip()
    if t.startswith("list[") and t.endswith("]"):
        inner = t[5:-1].strip()
        return {"type": "array", "items": {"type": _JSON_TYPES.get(inner, "string")}}
    if t.startswith("dict[") and t.endswith("]"):
        return {"type": "object"}
    return _JSON_TYPES.get(t, "string")


@dataclass
class ToolSpec:
    """工具声明。"""

    name: str
    category: str                       # statute / case / material / document / business / knowledge
    description: str
    handler: Callable[..., Any]
    idempotent: bool = True             # 幂等（检索类）可安全重试
    timeout: int | None = None
    max_retry: int | None = None
    requires_auth: bool = True
    requires_audit: bool = True
    fallback: Callable[..., Any] | None = None
    params: dict[str, dict[str, Any]] = field(default_factory=dict)

    # ---------------------------------------------------------------- Schema
    def input_schema(self) -> dict[str, Any]:
        """MCP ``tools/list`` 里每个工具的 ``inputSchema``（JSON Schema）。"""
        props: dict[str, Any] = {}
        required: list[str] = []
        for name, spec in self.params.items():
            prop: dict[str, Any] = {"description": spec.get("desc") or ""}
            jtype = _json_type(spec.get("type") or "str")
            if isinstance(jtype, dict):
                prop.update(jtype)
            else:
                prop["type"] = jtype
            if "default" in spec:
                prop["default"] = spec["default"]
            props[name] = prop
            if spec.get("required"):
                required.append(name)
        schema: dict[str, Any] = {"type": "object", "properties": props}
        if required:
            schema["required"] = required
        return schema

    def param_list(self) -> list[dict[str, Any]]:
        """扁平化的参数清单，供管理后台表格渲染。"""
        return [
            {
                "name": name,
                "type": spec.get("type") or "str",
                "desc": spec.get("desc") or "",
                "required": bool(spec.get("required")),
                "default": spec.get("default", None) if not spec.get("required") else None,
            }
            for name, spec in self.params.items()
        ]

    def to_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "idempotent": self.idempotent,
            "timeout": self.timeout or settings.TOOL_TIMEOUT,
            "max_retry": self.max_retry if self.max_retry is not None else settings.TOOL_MAX_RETRY,
            "params": self.param_list(),
        }



class ToolExecutor:
    """统一执行器：超时 / 重试 / 指数退避 / 降级 / 审计。

    **计数按任务隔离**（``call_counts[task_id][tool]``）。方案 4.3 的工具次数限额
    是"防死循环第二层"，它约束的是**一次任务内**的检索次数；如果按进程累计，
    表现就是"同一个服务进程里，第 4 个咨询请求的法规检索必然被拦死"，
    而错误信息还是「已达调用上限（3/3）」—— 看起来像防死循环生效了，
    其实是把所有后续任务一起误杀了。配额的作用域必须是任务。
    """

    # 同时保留的任务桶上限。每个桶只有几个键，但服务是长驻的，
    # 不设上限就是一条稳定的内存泄漏；超出后按插入顺序淘汰最旧的桶。
    MAX_TASK_BUCKETS = 500

    def __init__(self) -> None:
        self.call_counts: dict[str, dict[str, int]] = {}
        self.failures: dict[str, int] = {}

    # ---------------------------------------------------------------- 任务生命周期
    def begin_task(self, task_id: str) -> None:
        """开新任务时清零该任务的计数（幂等）。

        为什么在"开始"而不是"结束"清：任务可能中途异常退出、进程被重启，
        清理逻辑不保证被执行；而开始时的清零是无条件的，不依赖上一次是否善终。
        """
        if not task_id:
            return
        self.call_counts[task_id] = {}
        while len(self.call_counts) > self.MAX_TASK_BUCKETS:
            oldest = next(iter(self.call_counts))
            if oldest == task_id:
                break
            self.call_counts.pop(oldest, None)

    def end_task(self, task_id: str) -> None:
        """任务结束后回收计数（失败不影响调用方）。"""
        if task_id:
            self.call_counts.pop(task_id, None)

    # ---------------------------------------------------------------- 限额
    def count(self, name: str, task_id: str = "") -> int:
        return self.call_counts.get(task_id, {}).get(name, 0)

    def guard_quota(self, name: str, task_id: str = "") -> tuple[bool, str]:
        """工具次数白名单限额（方案 4.3 防死循环第二层）。"""
        limits = {
            "statute_search": settings.STATUTE_SEARCH_MAX_CALLS,
            "case_search": settings.CASE_SEARCH_MAX_CALLS,
        }
        limit = limits.get(name)
        if limit is None:
            return True, ""
        used = self.count(name, task_id)
        if used >= limit:
            return False, f"工具 {name} 已达本次任务调用上限（{used}/{limit}）"
        return True, ""

    # ---------------------------------------------------------------- 执行
    async def run(self, spec: ToolSpec, *, task_id: str = "", **kwargs: Any) -> ToolResult:
        if task_id and task_id not in self.call_counts:
            # 未经 begin_task 就直接调用（脚本、单测）：按需建桶。
            # 用 task_id 建桶而不是落到全局桶，保证配额永远是任务级的。
            self.begin_task(task_id)

        allowed, why = self.guard_quota(spec.name, task_id)
        if not allowed:
            logger.info("工具限额拦截：%s（task=%s）", why, task_id or "-")
            return ToolResult(ok=False, tool=spec.name, error=why, degraded=True,
                              fallback_note="调用次数已达上限，跳过该检索")

        timeout = spec.timeout or settings.TOOL_TIMEOUT
        max_retry = spec.max_retry if spec.max_retry is not None else settings.TOOL_MAX_RETRY
        # 非幂等操作（写入类）不重试，避免重复执行
        if not spec.idempotent:
            max_retry = 0

        started = time.perf_counter()
        last_error = ""
        attempts = 0

        self.call_counts.setdefault(task_id, {})[spec.name] = self.count(spec.name, task_id) + 1

        for attempt in range(max_retry + 1):
            attempts = attempt + 1
            try:
                result = spec.handler(**kwargs)
                if inspect.isawaitable(result):
                    result = await asyncio.wait_for(result, timeout=timeout)
                elapsed = int((time.perf_counter() - started) * 1000)
                if isinstance(result, ToolResult):
                    result.tool = result.tool or spec.name
                    result.elapsed_ms = elapsed
                    result.retries = attempt
                    result.attempts = attempts
                    if result.ok:
                        self.failures.pop(spec.name, None)
                    return result
                return ToolResult(ok=True, tool=spec.name, data=result, elapsed_ms=elapsed,
                                  retries=attempt, attempts=attempts)
            except asyncio.TimeoutError:
                last_error = f"超时（{timeout}s）"
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"

            self.failures[spec.name] = self.failures.get(spec.name, 0) + 1
            if attempt < max_retry:
                backoff = settings.TOOL_RETRY_BACKOFF * (2 ** attempt)
                logger.warning("工具 %s 第 %d 次失败（%s），%.1fs 后重试", spec.name, attempt + 1, last_error, backoff)
                await asyncio.sleep(backoff)

        # ---- Fallback
        elapsed = int((time.perf_counter() - started) * 1000)
        if spec.fallback is not None:
            try:
                fb = spec.fallback(**kwargs)
                if inspect.isawaitable(fb):
                    fb = await fb
                if isinstance(fb, ToolResult):
                    # ⚠ 降级通道**自己就是返回 ToolResult 的**（``_fb_statute_search`` /
                    # ``_fb_case_search`` / ``_fb_material_parse`` …），这里若再包一层
                    # ``data=fb``，`res.data` 就成了一整个 ToolResult —— 调用方
                    # `(res.data or {}).get("statutes")`（``statute_node.py:65``）当场抛
                    # ``AttributeError: 'ToolResult' object has no attribute 'get'``，
                    # **整个 StatuteNode 判失败**（``node_failed``、处理方式 continue），
                    # 于是证据池一条法条都没有，交付物看上去像"检索没查到"。
                    # 真机上 2026-09-29 一条、2026-10-07 五条，全部集中在主通道超时
                    # （30s）走降级的那几次 —— 平时降级通道不触发，所以长期没被发现。
                    # 正确做法与上面成功路径逐字同理：**认它是结果，不认它是数据**。
                    fb.tool = fb.tool or spec.name
                    fb.degraded = True
                    fb.elapsed_ms = int((time.perf_counter() - started) * 1000)
                    fb.retries = max_retry
                    fb.attempts = attempts
                    if not fb.fallback_note:
                        fb.fallback_note = f"工具 {spec.name} 失败（{last_error}），已走降级路径"
                    return fb
                return ToolResult(
                    ok=True, tool=spec.name, data=fb, degraded=True,
                    fallback_note=f"工具 {spec.name} 失败（{last_error}），已走降级路径",
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    retries=max_retry, attempts=attempts,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = f"{last_error}；降级亦失败：{exc}"

        logger.warning("工具 %s 最终失败：%s", spec.name, last_error)
        return ToolResult(
            ok=False, tool=spec.name, error=last_error, degraded=True,
            fallback_note=f"工具 {spec.name} 不可用，相关证据缺失",
            elapsed_ms=elapsed, retries=max_retry, attempts=attempts,
        )

    def stats(self) -> dict[str, Any]:
        return {
            "tasks_tracked": len(self.call_counts),
            "calls": {t: dict(c) for t, c in self.call_counts.items()},
            "failures": dict(self.failures),
        }


tool_executor = ToolExecutor()
