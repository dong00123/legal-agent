"""A2A 客户端：把委派消息变成一次真实的 Agent 调用。

两条通道（``A2ATransport``），都由 ``settings.A2A_TRANSPORT`` 决定：

- **IN_PROCESS（默认）**：委派就是"拿着同一个 Harness 调另一个节点函数"，
  没有序列化、没有网络 —— 单机部署时零开销。
- **HTTP**：把 ``A2AMessage`` 序列化后 POST 到 **独立部署** 的 Agent 实例
  （``A2A_REMOTE_BASE``），对方进程用 ``POST /api/v1/a2a/remote/{agent}``
  接住、在**自己进程内**跑完再把 ``A2AArtifact`` 序列化回来（见
  ``backend/app/api/v1/agents.py``）。能力发现走
  ``GET /api/v1/a2a/remote/{agent}/card``。

跨进程边界就三条约定，写死在这里以免两边慢慢分叉：

1. **线上格式就是 ``A2AMessage.to_dict()`` / ``A2AArtifact.to_dict()``**，
   不另造传输结构 —— 多一层 DTO 就多一处两边不同步的机会；
2. **只传证据快照与显式 payload**，不传整个 State：State 里挂着 Harness、
   连接、上万条证据，既不能 JSON 化也不该被别的进程改；
3. **失败显式返回 ``ok=False``**，不静默退回进程内 ——
   静默降级会让运维以为"已经拆出去了"，排查时方向全错。

为什么值得做这层：节点函数直接互调时，调用方会不知不觉依赖被调方的内部字段
（比如 Knowledge Agent 顺手读了 ``risk_points``）。包一层 A2A 之后，
"只通过 ``A2AArtifact`` 交流"是可检查的约束，拆进程时才不用重写。
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import quote

import httpx

from backend.app.config import settings

from backend.agents.protocol.message import (
    A2AAction,
    A2AArtifact,
    A2AMessage,
    A2ATransport,
)
from backend.agents.protocol.registry import card_for, node_of
from backend.agents.nodes import NODE_FUNCS

logger = logging.getLogger("legal_agent.a2a")


class A2AUnavailable(RuntimeError):
    """委派无法完成（Agent 未注册 / 通道未配置）。"""


# 各节点产出里"算得上结论"的字段。用于把节点跑完之后的 State 增量
# 收敛成一份可读的返回物 —— 直接把整个 State 抛回去等于没包装。
FIELD_KEYS: dict[str, tuple[str, ...]] = {
    "IntentNode": ("intent", "doc_type"),
    "IntakeNode": ("parties", "facts", "issues", "clauses"),
    "StatuteNode": ("statutes",),
    "CaseNode": ("cases",),
    "KnowledgeNode": ("knowledges",),
    "ReviewNode": ("risk_points", "missing_clauses", "revision_draft"),
    "AnalysisNode": ("issues", "strategy", "limitation_check"),
    "ValidatorNode": ("citations", "citation_summary", "citation_check"),
    "DraftNode": ("draft", "draft_type"),
    "ReportNode": ("report", "confidence", "evidence_gap"),
    "MemoryNode": ("memory_written",),
}

# 提供核对能力的 Agent（A2AAction.VERIFY 只对它们成立）
VERIFIER_NODES = ("ValidatorNode",)


class A2AClient:
    """A2A 委派客户端。

    ``harness`` 为 None 时只能发 PING —— 没有运行时就没有 State 可操作，
    这时应当直接告诉调用方，而不是自己造一个空 State 跑出"什么都没捞到"的结果。
    """

    def __init__(
        self,
        harness: Any = None,
        *,
        transport: A2ATransport = A2ATransport.IN_PROCESS,
        remote_base: str = "",
        timeout_ms: int = 0,
    ) -> None:
        self.harness = harness
        self.transport = transport
        self.remote_base = (remote_base or "").rstrip("/")
        self.timeout_ms = timeout_ms

    # ------------------------------------------------------------------ 对外
    async def send(self, message: A2AMessage) -> A2AArtifact:
        """发送一次委派并等回执。**不抛异常给调用方** —— 委派失败也是一条要落进
        证据缺口的事实，让调用方自己 try/except 的结果往往是"这个 Agent 挂了，
        整条流水线就断了"，而正确行为通常是"记一条缺口、继续跑别的"。"""
        started = time.monotonic()
        try:
            if message.action is A2AAction.PING:
                # PING 只查本地注册表（"这个 Agent 在不在目录里"），不依赖通道，
                # 所以它对 HTTP 通道同样成立 —— 探活不该因为网络抖动就变成"未注册"。
                return self._ping(message, started)
            if self.transport is A2ATransport.HTTP:
                return await self._http(message, started)
            return await self._in_process(message, started)
        except A2AUnavailable as exc:
            return self._fail(message, str(exc), started)
        except Exception as exc:  # noqa: BLE001
            logger.exception("A2A 委派异常：%s", message.to_agent)
            return self._fail(message, f"委派 {message.to_agent} 失败：{exc}", started)

    async def call(
        self,
        agent: str,
        *,
        action: A2AAction = A2AAction.RUN,
        payload: dict[str, Any] | None = None,
    ) -> A2AArtifact:
        """便捷入口：只关心"让某个 Agent 干活"时用这个。"""
        evidence = {}
        if self.harness is not None:
            evidence = self._snapshot()
        return await self.send(
            A2AMessage(
                to_agent=agent,
                action=action,
                payload=dict(payload or {}),
                evidence=evidence,
                task_id=getattr(getattr(self.harness, "ctx", None), "task_id", "") or "",
            )
        )

    # ------------------------------------------------------------------ 通道
    def _remote_url(self, agent_id: str, suffix: str = "") -> str:
        """拼远端 URL。**兼容两种写法**：``http://host:8000`` 与
        ``http://host:8000/api/v1`` 都接受 —— 让对方地址少写前缀就直接 404
        是很难排查的一类配置错误，而补前缀是无歧义的。"""
        base = (self.remote_base or "").rstrip("/")
        prefix = str(getattr(settings, "API_PREFIX", "/api/v1") or "/api/v1")
        if prefix and not base.endswith(prefix):
            base = f"{base}{prefix}"
        return f"{base}/a2a/remote/{quote(agent_id, safe='')}{suffix}"

    def _ping(self, message: A2AMessage, started: float) -> A2AArtifact:
        card = card_for(message.to_agent)
        if card is None:
            return self._fail(message, f"未注册的 Agent：{message.to_agent}", started)
        return A2AArtifact(
            message_id=message.message_id,
            from_agent=card.id,
            summary=f"{card.name} 在线：{card.description}",
            fields={"card": card.to_dict()},
            transport=self.transport.value,
            elapsed_ms=_ms(started),
        )

    async def _http(self, message: A2AMessage, started: float) -> A2AArtifact:
        """跨进程委派：把消息 POST 到独立部署的 Agent 实例。

        ⛔ **失败不静默回退进程内**。理由写在模块 docstring 里：
        静默回退会让运维以为"已经拆出去了"，而实际还在同一进程里跑，
        排查方向全错。要么成功，要么带着明确的 ``ok=False`` 返回。
        """
        if not self.remote_base:
            return self._fail(
                message,
                "HTTP 通道未配置远端地址（A2A_REMOTE_BASE 为空）。"
                "要把 Agent 独立部署，须配置 A2A_TRANSPORT=http 与 A2A_REMOTE_BASE",
                started,
            )
        card = card_for(message.to_agent)
        if card is None:
            return self._fail(message, f"未注册的 Agent：{message.to_agent}", started)

        url = self._remote_url(card.id)
        timeout_s = (self.timeout_ms or settings.A2A_TIMEOUT_MS or 30_000) / 1000.0
        headers = {"Content-Type": "application/json"}
        if settings.A2A_TOKEN:
            headers["Authorization"] = f"Bearer {settings.A2A_TOKEN}"
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as cli:
                resp = await cli.post(url, json=message.to_dict(), headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            return self._fail(
                message,
                f"远端 {card.id} 返回 {exc.response.status_code}：{_body_hint(exc.response)}",
                started,
            )
        except Exception as exc:  # noqa: BLE001 - 网络类异常一律收敛成回执，不打断流水线
            return self._fail(message, f"远端 {card.id} 不可达：{exc}", started)

        return A2AArtifact(
            message_id=data.get("message_id") or message.message_id,
            from_agent=data.get("from_agent") or card.id,
            ok=bool(data.get("ok", True)),
            summary=data.get("summary") or "",
            fields=data.get("fields") or {},
            gaps=data.get("gaps") or [],
            transport=A2ATransport.HTTP.value,
            elapsed_ms=data.get("elapsed_ms") or _ms(started),
            error=data.get("error") or "",
        )

    async def fetch_remote_card(self, agent: str) -> dict[str, Any]:
        """从远端实例拉对方的能力卡（A2A 能力发现），供运维核对两端是否同一版本。"""
        card = card_for(agent)
        if card is None:
            return {"ok": False, "error": f"未注册的 Agent：{agent}"}
        if not self.remote_base:
            return {"ok": False, "error": "A2A_REMOTE_BASE 为空"}
        url = self._remote_url(card.id, suffix="/card")
        headers = {"Authorization": f"Bearer {settings.A2A_TOKEN}"} if settings.A2A_TOKEN else {}
        try:
            async with httpx.AsyncClient(timeout=10.0) as cli:
                resp = await cli.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"拉取远端卡片失败：{exc}"}
        return {"ok": True, "card": data}

    async def _in_process(self, message: A2AMessage, started: float) -> A2AArtifact:
        card = card_for(message.to_agent)
        if card is None:
            return self._fail(message, f"未注册的 Agent：{message.to_agent}", started)
        node = node_of(message.to_agent)
        fn = NODE_FUNCS.get(node)
        if fn is None:
            return self._fail(message, f"节点 {node} 未注册函数", started)

        if message.action is A2AAction.VERIFY and node not in VERIFIER_NODES:
            return self._fail(
                message,
                f"{card.name} 不提供核对能力（可核对的只有 "
                f"{'、'.join(VERIFIER_NODES)}）",
                started,
            )

        if self.harness is None:
            return self._fail(message, "无运行时（Harness）可用，无法委派执行", started)

        state = self.harness.state
        self._apply_payload(state, message.payload)
        before_gaps = len(state.get("evidence_gap") or [])

        await fn(state, self.harness)

        fields = {k: state.get(k) for k in FIELD_KEYS.get(node, ()) if state.get(k) is not None}
        gaps = list(state.get("evidence_gap") or [])[before_gaps:]
        return A2AArtifact(
            message_id=message.message_id,
            from_agent=card.id,
            ok=True,
            summary=_summarize(node, fields, gaps),
            fields=fields,
            gaps=gaps,
            transport=A2ATransport.IN_PROCESS.value,
            elapsed_ms=_ms(started),
        )

    # ------------------------------------------------------------------ 工具
    def _snapshot(self) -> dict[str, Any]:
        """只读证据快照（浅拷贝）。

        **不做深拷贝**：State 里的 evidence 动辄上千条法条，每委派一次就深拷一遍
        会让 A2A 成为性能瓶颈，而实际的跨 Agent 误写几乎都发生在一层列表上
        （``state["cases"].append(...)``），浅拷贝已经能挡住。
        """
        state = getattr(self.harness, "state", None) or {}
        return {k: (list(v) if isinstance(v, list) else v) for k, v in state.items()}

    @staticmethod
    def _apply_payload(state: dict[str, Any], payload: dict[str, Any]) -> None:
        """把委派方显式给出的输入写进 State（如替换检索式）。

        只写 payload 里出现的键 —— 不覆盖调用方已有的证据。
        """
        for key, value in (payload or {}).items():
            if value is not None:
                state[key] = value

    def _fail(self, message: A2AMessage, error: str, started: float) -> A2AArtifact:
        art = A2AArtifact.failure(message, error)
        # 回执必须说清"这次委派走的是哪条通道"：跨机排障时第一件事就是
        # 确认请求到底有没有真的发出去，transport 写死成进程内会直接误导。
        art.transport = self.transport.value
        art.elapsed_ms = _ms(started)
        logger.warning("A2A 委派未完成：%s -> %s (%s)", message.from_agent, message.to_agent, error)
        return art


def _summarize(node: str, fields: dict[str, Any], gaps: list[dict[str, Any]]) -> str:
    """一句话说明这次委派拿到了什么。给 Trace 与调试面板用。"""
    parts: list[str] = []
    for key in ("statutes", "cases", "knowledges", "risk_points", "clauses",
                "parties", "facts", "issues", "strategy", "citations"):
        value = fields.get(key)
        if isinstance(value, list):
            parts.append(f"{key} {len(value)}")
    if fields.get("citation_check"):
        parts.append(f"引用终审 {fields['citation_check']}")
    if fields.get("confidence") is not None:
        parts.append(f"置信度 {fields['confidence']}")
    if gaps:
        parts.append(f"新增缺口 {len(gaps)}")
    return f"{node}：{'｜'.join(parts)}" if parts else f"{node}：无结构化产出"


def _body_hint(resp: Any) -> str:
    """从错误响应体里取一句人话（对方的 detail / error），避免只看到 500。

    截断到 200 字：远端可能把整段 traceback 塞进响应体，全量透传会污染日志。
    """
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return (resp.text or "")[:200]
    if isinstance(data, dict):
        return str(data.get("detail") or data.get("error") or "")[:200]
    return str(data)[:200]


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


_cached: A2AClient | None = None


def client(harness: Any = None) -> A2AClient:
    """按配置取一个客户端：``A2A_TRANSPORT=http`` 时走跨进程通道。

    配置值非法（手改 .env 打错字）时**退回进程内并打日志**，而不是抛异常 ——
    通道配错不该让整条流水线起不来，但必须留痕。
    """
    try:
        transport = A2ATransport(str(getattr(settings, "A2A_TRANSPORT", "in_process")).strip().lower())
    except ValueError:
        logger.warning("A2A_TRANSPORT 配置非法（%s），回退进程内通道",
                       getattr(settings, "A2A_TRANSPORT", ""))
        transport = A2ATransport.IN_PROCESS
    return A2AClient(
        harness,
        transport=transport,
        remote_base=str(getattr(settings, "A2A_REMOTE_BASE", "") or ""),
        timeout_ms=int(getattr(settings, "A2A_TIMEOUT_MS", 30_000) or 30_000),
    )


__all__ = [
    "FIELD_KEYS",
    "VERIFIER_NODES",
    "A2AClient",
    "A2AUnavailable",
    "client",
]
