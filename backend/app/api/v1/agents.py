"""Agent 目录与 A2A 委派接口（旁路，零侵入既有案件流程）。

本文件**不读写任何案件、材料、任务数据**，只做三件事：
- ``GET /agents`` 暴露 11 个 Agent 的能力卡片 + 一致性自检（只读）；
- ``POST /a2a/delegate`` 在本进程隔离的临时 ``State``/``Harness`` 上用
  ``A2AClient`` 的 IN_PROCESS 通道真实委派某个 Agent 跑一次；
- **A2A 远端入口**（``/a2a/remote/...``）：接住**别的实例**发来的跨进程委派，
  在本进程跑对应节点，把 ``A2AArtifact`` 序列化回去 —— 这一组接口与
  ``A2AClient`` 的 HTTP 通道配套，两个进程各跑一半就组成一次真正的 A2A 协作。

所有新增代码都局限在本文件 + 路由汇聚表两行，不触碰 ``cases.py``、各节点、
前端案件弹层，因此不会影响证据链、证据充分度评估、材料上传等既有功能。
"""

from __future__ import annotations

import hmac
import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from backend.agents.harness import Harness
from backend.agents.protocol.client import A2AClient
from backend.agents.protocol.message import A2AAction, A2AArtifact, A2AMessage, A2ATransport
from backend.agents.protocol.registry import (
    card_for,
    directory,
    health,
    product_health,
    products,
)
from backend.agents.state import new_task_id
from backend.app.api.deps import RequireUser
from backend.app.config import settings
from backend.app.db.session import get_db
from backend.app.security import CurrentUser, bearer_scheme, get_current_user

logger = logging.getLogger("legal_agent.api.agents")

router = APIRouter(tags=["Agent 与 A2A 协作"])

VALID_ACTIONS = ("ping", "run", "verify")


class DelegateRequest(BaseModel):
    agent: str = Field(..., min_length=1, max_length=64, description="Agent 名或节点名")
    action: str = Field("ping", description="ping=探活 / run=真实委派 / verify=核对")
    intent: str = Field("consult", description="意图（决定检索式）")
    query: str = Field("", max_length=4000, description="委派输入：用户问题/检索式")
    material_text: str = Field("", description="可选：材料正文（ReviewNode 等需要）")
    doc_type: str = Field("unknown", description="材料类型")
    task_id: str = Field("", max_length=48, description="可选：自定义任务编号")
    payload: dict[str, Any] = Field(default_factory=dict, description="透传给被调 Agent 的额外输入")


@router.get("/agents")
async def list_agents(user: RequireUser) -> dict[str, Any]:
    """Agent 目录：能力卡片 + 一致性自检。只读，不影响任何业务数据。

    ⚠ 这里的 ``agents`` 是**节点级**实现清单（11 个），不是律师在侧边栏看到的
    那 4 个。对外的那 4 个走 ``GET /agents/products``。两个都保留：
    这个回答"每一步是谁干的、它允许调哪些工具"，那个回答"律师点的入口背后钉了什么"。
    """
    return {"agents": directory(), "health": health()}


@router.get("/agents/products", summary="对外的 4 个产品 Agent（侧边栏那 4 个入口）")
async def list_products(user: RequireUser) -> dict[str, Any]:
    """产品 Agent 目录 + 一致性自检。

    每个入口给出：界面上的名字、所属页面、**钉住的意图**、该意图的节点路线、
    以及客户端应当调用的端点。前端的「能力/降级」页与验收脚本据此对齐，
    不必再各自硬编一份名单 —— 两份名单早晚分叉，而分叉的表现是
    "界面上有这个入口、后端却没有对应路径"，点下去只得到一个沉默的空页面。
    """
    return {"products": products(), "health": product_health()}


def _a2a_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Any = Depends(get_db),
) -> CurrentUser:
    """A2A 远端入口的调用方身份：**服务令牌** 或 **正常登录用户** 二选一。

    ⛔ 为什么不能只沿用 ``RequireUser``：跨实例委派的调用方是**另一台机器**，
    它没有律师的登录态。而让它持有某个律师的 JWT 更糟 ——
    那等于把一个真人账号的权限整体借给别的进程，事后无法区分
    "这条委派是机器发的"还是"这个律师点的"。

    所以额外开一条**服务级令牌**（``A2A_TOKEN``，双方同一份配置）：
    - 配置了且对得上 ⇒ 记为 ``a2a-service`` 服务账号（不占任何真人身份）；
    - 没配置 ⇒ 该通道**关闭**，只剩正常登录态，
      避免"没配令牌 = 谁都能调"（这是最容易留的门）。

    令牌比对用常数时间比较，防止按响应时间逐字节试探。
    """
    token = (credentials.credentials if credentials else "") or ""
    configured = str(getattr(settings, "A2A_TOKEN", "") or "")
    if configured and token:
        if hmac.compare_digest(token.encode(), configured.encode()):
            return CurrentUser(0, "a2a-service", "lawyer", None, "A2A 服务令牌")
    return get_current_user(credentials, db)


RequireA2A = Annotated[CurrentUser, Depends(_a2a_principal)]


class RemoteDelegateRequest(BaseModel):
    """远端委派请求体：与 ``A2AMessage.to_dict()`` 同构。

    ⛔ 刻意**不另造一层 DTO**：多一层就多一处两端不同步的机会，
    而 A2A 的价值恰恰是"两边形状一致"。
    """

    action: str = Field("run", description="run / verify")
    payload: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict, description="委派方传来的只读证据快照")
    task_id: str = Field("", max_length=48)
    from_agent: str = Field("", max_length=64)
    message_id: str = Field("", max_length=64)
    intent: str = Field("consult", description="证据快照里没有 intent 时的兜底")


@router.get("/a2a/remote/cards", summary="A2A 能力发现：本实例全部 Agent 卡片")
async def remote_cards(user: RequireA2A) -> dict[str, Any]:
    """对端实例拉目录用：确认"我能委派给你哪些 Agent、版本是否一致"。"""
    return {"ok": True, "agents": directory(), "health": health()}


@router.get("/a2a/remote/{agent}/card", summary="A2A 能力发现：单个 Agent 卡片")
async def remote_card(agent: str, user: RequireA2A) -> dict[str, Any]:
    """单个 Agent 的能力卡（A2A AgentCard）。未知 Agent 给 404，让对端明确知道配错了。"""
    card = card_for(agent)
    if card is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"未注册的 Agent：{agent}")
    return card.to_dict()


@router.post("/a2a/remote/{agent}", status_code=status.HTTP_200_OK,
             summary="A2A 远端入口：接住别的实例的委派")
async def remote_delegate(agent: str, body: RemoteDelegateRequest, user: RequireA2A) -> dict[str, Any]:
    """**独立部署**的 Agent 在这一端接住跨进程委派。

    与 ``/a2a/delegate`` 的区别：那个是给人调试用的（自己拼 query/material_text），
    这个是对端机器发来的 —— State 由 **对方传来的 evidence + payload** 重建，
    本进程不持有也不写任何案件数据，跑完即弃。

    为什么这样重建 State 是安全的：
    - 只取 JSON 能表达的字段，Harness / 连接 / 工具注册这类运行时对象由本地重建；
    - 结果只回 ``A2AArtifact``（结构化回执），不回整个 State，
      避免把对方进程的字段反向灌回调用方。
    """
    if card_for(agent) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"未注册的 Agent：{agent}")
    if body.action not in ("run", "verify"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"远端委派只支持 run / verify，收到：{body.action}")

    uid = getattr(user, "id", None)
    uname = getattr(user, "username", "") or ""
    state: dict[str, Any] = {
        "task_id": body.task_id or new_task_id(),
        "trace_id": f"trace_{uuid.uuid4().hex[:12]}",
        "user_id": uid,
        "username": uname,
        "intent": (body.evidence.get("intent") or body.intent or "consult"),
        "query": body.evidence.get("query") or "",
        "material_text": body.evidence.get("material_text") or "",
        "doc_type": body.evidence.get("doc_type") or "unknown",
        "evidence_gap": [],
        "trace": [],
        "tool_calls": [],
    }
    # 证据快照里的结构化证据（法条/类案/条款…）一并带过来，
    # 否则被委派方只能看到一句 query，等于让它在没有证据的前提下重新判一遍。
    for key in ("statutes", "cases", "knowledges", "clauses", "parties", "facts",
                "issues", "citations", "risk_points", "missing_clauses"):
        value = body.evidence.get(key)
        if value is not None:
            state[key] = value

    harness = Harness.start(state, client="a2a", username=uname, user_id=uid)
    try:
        artifact = await A2AClient(harness, transport=A2ATransport.IN_PROCESS).send(
            A2AMessage(
                to_agent=agent,
                action=A2AAction(body.action),
                payload=dict(body.payload or {}),
                evidence=dict(body.evidence or {}),
                task_id=body.task_id,
                from_agent=body.from_agent or "remote",
                message_id=body.message_id or "",
            )
        )
    except Exception as exc:  # noqa: BLE001 - 对端不该收到 500，收一条可解释的失败回执
        logger.exception("远端 A2A 委派异常：%s", agent)
        return A2AArtifact(message_id=body.message_id or "", from_agent=agent, ok=False,
                           error=f"委派 {agent} 失败：{exc}",
                           transport=A2ATransport.HTTP.value).to_dict()
    finally:
        try:
            harness.finish()
        except Exception:  # noqa: BLE001 - 收尾失败不该盖掉委派结果
            pass
    return artifact.to_dict()


@router.post("/a2a/delegate", status_code=status.HTTP_200_OK)
async def delegate(body: DelegateRequest, user: RequireUser) -> dict[str, Any]:
    """旁路 A2A 委派：在隔离运行时真实调用某个 Agent 一次。

    - ping：仅校验 Agent 是否注册并回卡片，**不消耗模型额度**；
    - run/verify：构造临时 State/Harness，经 IN_PROCESS 通道跑节点，
      返回结构化回执（含产出字段与证据缺口）；不写入案件、不落库。
    """
    if body.action not in VALID_ACTIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"非法 action：{body.action}（仅支持 {VALID_ACTIONS}）",
        )
    if card_for(body.agent) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未注册的 Agent：{body.agent}",
        )

    # ping 不需要运行时，零成本探活
    if body.action == "ping":
        client = A2AClient()
        artifact = await client.call(body.agent, action=A2AAction.PING)
        return artifact.to_dict()

    # run / verify：构造隔离运行时（临时 State，跑完即弃，不落库）
    uid = getattr(user, "id", None)
    uname = getattr(user, "username", "") or ""
    state: dict[str, Any] = {
        "task_id": body.task_id or new_task_id(),
        "trace_id": f"trace_{uuid.uuid4().hex[:12]}",
        "user_id": uid,
        "username": uname,
        "intent": body.intent,
        "query": body.query,
        "material_text": body.material_text,
        "doc_type": body.doc_type,
        "evidence_gap": [],
        "trace": [],
        "tool_calls": [],
    }
    harness = Harness.start(state, client="api", username=uname, user_id=uid)
    client = A2AClient(harness)
    try:
        artifact = await client.call(
            body.agent,
            action=A2AAction(body.action),
            payload=body.payload,
        )
    except Exception as exc:  # noqa: BLE001 - A2AClient 已兜底，这里再兜一层防意外
        logger.exception("A2A 委派未预期异常：%s", body.agent)
        return {
            "ok": False,
            "from_agent": body.agent,
            "summary": "",
            "fields": {},
            "gaps": [{"kind": "tool", "reason": f"委派异常：{exc}", "impact": "该步骤未完成"}],
            "transport": "in_process",
            "elapsed_ms": 0,
            "error": str(exc),
        }
    return artifact.to_dict()


__all__ = ["router"]
