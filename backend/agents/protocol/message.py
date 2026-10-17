"""A2A 消息契约：Agent 卡片、委派消息、返回物。

方案 4.6：
> MCP 解决 Agent 和工具之间怎么连接，A2A 解决 Agent 和 Agent 之间怎么协作。

落地的边界（必须说清楚，否则会把"协议"做成"又一层没用的包装"）：

**默认通道是进程内的**。11 个 Agent 通常跑在同一个 FastAPI 进程里，
"A2A 请求"就是一次普通的函数调用 —— 收益不在于省掉网络开销，
而在于**契约**：

1. **消息形状固定**。委派方只说"我要什么"（``A2AAction`` + ``payload``），
   不碰被调方的内部实现。想换掉 Statute Agent 的检索实现，
   只改 ``statute_node`` 一个文件，调用方零改动。
2. **证据快照随消息传递**。被调方拿到的是一份**证据的只读视图**，
   不是整个可变 State。避免出现"Knowledge Agent 顺手改了 cases"这类
   跨 Agent 的隐式耦合 —— 那种 bug 在单进程里能跑通，拆进程时才炸。
3. **返回物带缺口**。每个 Agent 的产出都附 ``gaps``，
   汇总方不需要知道"哪个 Agent 可能失败"就能把风险收集齐。

**跨机部署（``A2ATransport.HTTP``）已接线**：消息整体 JSON 化后 POST 到
对端实例的 ``/a2a/remote/{agent}``，对方在**自己进程内**跑完再把
``A2AArtifact`` 序列化回来（见 ``app/api/v1/agents.py``）。跨进程的
序列化边界就是这三条：

1. **只传 JSON 能表达的东西**：``payload`` + ``evidence``（证据快照），
   State 里的 Harness / 连接 / 工具注册这类运行时对象由**接收方本地重建**；
2. **回程只回 ``A2AArtifact``**，不回整个 State —— 否则对方的字段会反向
   灌回调用方，等于又把两个进程耦回一个 State；
3. **失败显式回 ``ok=False``**，不静默退回进程内。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class A2ATransport(str, Enum):
    """传输通道。"""

    IN_PROCESS = "in_process"   # 同进程直接调用节点函数（默认，零网络开销）
    HTTP = "http"               # 独立部署的 Agent：POST /a2a/remote/{agent} 跨进程协作


class A2AAction(str, Enum):
    """委派动作。刻意只有三个 —— 动作一多，调用方就会开始依赖被调方细节。"""

    RUN = "run"           # 跑一次该 Agent 的主能力（如"检索适用法条"）
    VERIFY = "verify"     # 只做校验，不产出新内容（如案号存在性复核）
    PING = "ping"         # 探活（独立部署时用于健康检查）


# --------------------------------------------------------------------------- 卡片
@dataclass
class AgentCard:
    """Agent 能力卡。**这是被调方唯一的公开承诺**。

    ``produces`` 必须与节点实际写入 State 的字段一致：
    调用方是照着它决定"要不要等这个 Agent"的，写多了会让调用方空等。
    """

    id: str
    name: str
    description: str
    node: str
    intents: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    version: str = "1.0.0"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- 消息
@dataclass
class A2AMessage:
    """一次 Agent 间委派。

    ``evidence`` 是**只读证据快照**（dict 的浅拷贝），不是 State 本身。
    被调方读它、不写它 —— 想回传东西必须走 ``A2AArtifact.fields``，
    这样"谁改了什么"在 Trace 里始终可查。
    """

    to_agent: str
    action: A2AAction = A2AAction.RUN
    payload: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    task_id: str = ""
    from_agent: str = "harness"
    message_id: str = field(default_factory=lambda: f"msg_{uuid.uuid4().hex[:12]}")
    created_at: float = field(default_factory=time.time)
    deadline_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["action"] = self.action.value
        return d


@dataclass
class A2AArtifact:
    """Agent 返回物。

    ``gaps`` 与 ``fields`` 分开：前者是"我这次没能拿到的证据"（要进报告的
    证据缺口清单），后者是"我拿到的结论"。混在一起会让汇总方把
    "没检索到"当成"检索结论为空"。
    """

    message_id: str
    from_agent: str
    ok: bool = True
    summary: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    gaps: list[dict[str, Any]] = field(default_factory=list)
    transport: str = A2ATransport.IN_PROCESS.value
    elapsed_ms: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def failure(cls, message: A2AMessage, error: str) -> "A2AArtifact":
        return cls(message_id=message.message_id, from_agent=message.to_agent,
                   ok=False, error=error)


__all__ = [
    "A2AAction",
    "A2AArtifact",
    "A2AMessage",
    "A2ATransport",
    "AgentCard",
]
