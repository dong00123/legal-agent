"""A2A 多 Agent 协同（方案 4.5 / 4.6）。

分工：
- **MCP** 解决 Agent 和工具之间怎么连接；
- **A2A** 解决 Agent 和 Agent 之间怎么协作。

本包提供三件东西：

| 模块 | 职责 |
|---|---|
| ``message`` | 消息契约：Agent 卡片、委派消息、返回物 |
| ``registry`` | Agent 目录：从 planner/router 的元数据表派生，不另写一份 |
| ``client`` | 委派客户端：进程内通道（默认）与 HTTP 跨进程通道 |

跨进程部署时，本包只占一半：消息由 ``client`` 发到对端实例的
``POST /api/v1/a2a/remote/{agent}``，接住并执行的是
``backend/app/api/v1/agents.py`` 的远端入口；能力发现走
``GET /api/v1/a2a/remote/{agent}/card``。两边形状都来自 ``A2AMessage`` /
``A2AArtifact``，不存在各自的传输结构。

典型用法（在一个节点里委派另一个 Agent 的专项能力）::

    from backend.agents.protocol import A2AClient, A2AAction

    art = await A2AClient(harness).call("StatuteNode", A2AAction.RUN)
    if not art.ok:
        harness.gaps("statute", art.error, "该部分证据未采集")

**返回物里既有 ``fields`` 也有 ``gaps``**，两者必须分开看：
``fields`` 是"拿到了什么"，``gaps`` 是"没拿到什么"。混在一起会把
"没检索到"误读成"检索结论为空"。
"""

from backend.agents.protocol.client import A2AClient, A2AUnavailable, client
from backend.agents.protocol.message import (
    A2AAction,
    A2AArtifact,
    A2AMessage,
    A2ATransport,
    AgentCard,
)
from backend.agents.protocol.registry import (
    CARDS,
    card_for,
    cards_for_intent,
    directory,
    health,
    node_of,
)

__all__ = [
    "A2AAction",
    "A2AArtifact",
    "A2AClient",
    "A2AMessage",
    "A2ATransport",
    "A2AUnavailable",
    "AgentCard",
    "CARDS",
    "card_for",
    "cards_for_intent",
    "client",
    "directory",
    "health",
    "node_of",
]
