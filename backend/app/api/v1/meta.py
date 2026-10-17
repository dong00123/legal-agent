"""健康检查与元信息接口：``/health``、工具清单、Agent 目录、技能清单、版本。

``/health`` 是运维唯一会反复看的一页，所以它的设计目标是**一眼能分辨
"真的坏了"与"降级运行但可用"**。因此：

1. **每个子系统都报「实际生效的后端」+「降级到了什么」**，
   而不是一个笼统的 ``status: ok``。Milvus 连不上时系统仍能跑（走本地向量索引），
   但那句 "向量检索已降级" 必须出现在这里 —— 否则性能与召回质量悄悄变差，
   而所有人以为一切正常。

2. **``degraded`` 与 ``ok`` 分开。** 降级不是故障：内网 MySQL 抖一下、
   Redis 没开，系统照常出报告，只是慢一点或记忆不跨进程。
   混在一起会让 ``/health`` 要么长期红、要么掩盖真故障。

3. **结构健康（外键是否指向 legacy 备份表）必须在这里可见。**
   那类问题不会自己暴露，直到某天某个接口报 1452 ——
   放在健康检查里才能在"一眼"之内发现。

``/health`` **不需要登录**：它是给负载均衡与运维探针用的，
要登录的话探针本身就成了一个新的失败点。所以它不返回任何敏感信息 ——
数据库 URL 只留主机与库名（``db_status`` 已经做了 ``split("@")[-1]``）。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from typing import Annotated

from backend.app.api.deps import RequireAdmin
from backend.app.config import settings

logger = logging.getLogger("legal_agent.api.meta")

router = APIRouter(tags=["元信息"])

_STARTED_AT = time.time()


# --------------------------------------------------------------------------- 健康
@router.get("/health", summary="健康检查（无需登录）")
async def health(
    request: Request,
    full: Annotated[bool, Query(description="是否附带各子系统详细状态")] = True,
) -> dict[str, Any]:
    """健康检查。

    ``status`` 三态：
    - ``ok`` —— 全部子系统按设计运行；
    - ``degraded`` —— 有子系统降级但业务可用（例如向量库不可用、回落本地索引）；
    - ``error`` —— 关键路径不可用（关系库连不上且 SQLite 也建不起来）。
    """
    from backend.app.db import session as db_session

    db = db_session.db_status()
    schema = db_session.schema_health()
    problems: list[str] = []
    degraded: list[str] = []

    if db.get("backend") in ("none", ""):
        problems.append("关系库未初始化")

    # ---- 结构：外键是否还指向 legacy 备份表（本项目管的表才算问题）
    if schema.get("stale_fks"):
        problems.append(
            f"{len(schema['stale_fks'])} 条外键仍指向 legacy 备份表，"
            f"相关子表插入会报 1452（调用 init_db() 自动修复）"
        )

    backends = settings.runtime_backends()
    for name, info in backends.items():
        if not info.get("enabled"):
            degraded.append(f"{name} 已关闭，回落：{info.get('fallback')}")

    body: dict[str, Any] = {
        "status": "error" if problems else ("degraded" if degraded else "ok"),
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "env": settings.ENV,
        "uptime_seconds": int(time.time() - _STARTED_AT),
        "database": db,
        "schema": {
            "ok": schema.get("ok"),
            "stale_fks": schema.get("stale_fks") or [],
            "legacy_tables": len(schema.get("legacy_tables") or []),
            "foreign_tables_fks": len(schema.get("stale_fks_foreign_tables") or []),
            "note": "foreign_tables_fks 是库里历史遗留表的外键，不归本项目管，不影响 ok",
        },
        "problems": problems,
        "degraded": degraded,
        "disclaimer": settings.DISCLAIMER,
        # **预热状态不受 ``full`` 折叠**：它回答的是"第一个请求要不要等十几秒"，
        # 这恰恰是容器刚起来时运维唯一想知道的事。
        "warmup": _warmup(request),
    }
    if not full:
        return body

    body["backends"] = backends
    body["subsystems"] = await _subsystems()
    return body


def _warmup(request: Request) -> dict[str, Any]:
    """启动时的检索链路预热结果（见 ``main.run_warmup``）。

    **没有预热记录时，既不能少这个键，也不能假报 ok。**
    少键在前端看起来像"没这个功能"，假报 ok 会让人以为第一个请求也是快的
    —— 两种都比如实返回"还没预热过"更糟。

    （顺带修掉一处长期的注释失真：``run_startup_check`` 的 docstring
    一直声称它的结果会被 ``/health`` 读到，但此前根本没有任何地方读它。
    现在 ``/health.warmup`` 是真的从 ``app.state.startup_check`` 取的。）
    """
    check = getattr(request.app.state, "startup_check", None) or {}
    warm = check.get("warmup")
    if warm is None:
        return {
            "ok": None,
            "note": "本次启动没有预热记录（WARMUP_ENABLED=false 或启动未完成）"
                    "—— 首个检索请求需自行承担模型加载时间",
        }
    return warm


async def _subsystems() -> dict[str, Any]:
    """各子系统的运行态。

    **每一项单独 try**：健康检查自身不能因为某个可选组件没装就整体 500 ——
    那样探针会一直失败，反而把真正的问题掩盖在一堆"探针挂了"里。
    取不到就如实写 ``{"error": ...}``，而不是悄悄少一个键
    （少一个键在前端看起来像"这个子系统不存在"，和"它出错了"是两回事）。
    """
    out: dict[str, Any] = {}

    def _safe(label: str, module: str, attr: str, *args: Any) -> Any:
        try:
            return _call(module, attr, *args)
        except Exception as exc:  # noqa: BLE001
            logger.debug("健康检查子项 %s 失败：%s", label, exc)
            return {"error": f"{type(exc).__name__}: {exc}"}

    out["tools"] = _safe("tools", "backend.tools.registry", "registry_status")
    out["tool_executor"] = _safe("tool_executor", "backend.tools.registry", "executor_stats")
    out["retrieval"] = _safe("retrieval", "backend.rag.pipeline", "retrieval_status")
    out["agents"] = _safe("agents", "backend.agents.protocol", "health")
    out["skills"] = _safe("skills", "backend.skills", "skills_status")
    out["mcp"] = _safe("mcp", "backend.skills.mcp_server", "mcp_server_status")
    out["config"] = _safe("config", "backend.app.services.config_service", "health")
    # 队列积压必须在这里能看到。"任务提交了但一直不动"在只查进程存活时
    # 是看不出来的：API 正常、数据库正常、worker 可能根本没起，
    # 或者起了但一条都没认领成功（缺依赖/快照坏）。``queued`` 长期不降
    # 就是这两个信号里唯一能发现的那个。
    out["task_queue"] = _safe("task_queue", "backend.app.services.task_queue", "queue_stats")

    try:
        from backend.rag.pipeline import embedding_status

        out["embedding"] = await embedding_status()
    except Exception as exc:  # noqa: BLE001
        out["embedding"] = {"error": f"{type(exc).__name__}: {exc}"}

    return out


def _call(module: str, attr: str, *args: Any) -> Any:
    import importlib

    return getattr(importlib.import_module(module), attr)(*args)


@router.get("/health/ready", summary="就绪探针（只查关键依赖）")
def ready() -> dict[str, Any]:
    """廉价探针：只查关系库能否查询。给 K8s readiness / Nginx upstream check 用。

    与 ``/health`` 的区别：``/health`` 会去碰向量库、技能注册表等一堆东西，
    不适合每 5 秒被探一次。
    """
    from sqlalchemy import text

    from backend.app.db import session as db_session

    try:
        engine = db_session.init_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        return {"ready": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ready": True, "backend": db_session.ACTIVE_DB_BACKEND}


# --------------------------------------------------------------------------- 元信息
@router.get("/meta/version", summary="版本信息")
def version() -> dict[str, Any]:
    return {
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "env": settings.ENV,
        "api_prefix": settings.API_PREFIX,
        "disclaimer": settings.DISCLAIMER,
    }


@router.get("/meta/tools", summary="MCP 工具清单")
def tools(category: Annotated[str | None, Query(description="按类别过滤")] = None) -> dict[str, Any]:
    """工具清单。数据源是注册表本身，管理后台的「MCP 工具」页也读它。"""
    from backend.tools.registry import CATEGORIES, categorize, list_tools

    return {"items": list_tools(category), "categories": CATEGORIES, "grouped": categorize()}


@router.get("/meta/tools/manifest", summary="MCP tools/list 响应体")
def tools_manifest() -> dict[str, Any]:
    from backend.tools.registry import mcp_manifest

    return mcp_manifest()


@router.get("/meta/agents", summary="Agent 目录（A2A 卡片）")
def agents() -> dict[str, Any]:
    """A2A 协议的 Agent 目录。

    卡片**从 planner / router 的元数据表派生**，不另写一份 ——
    手抄的卡片一定会和实际路由分叉，然后"目录里说它能做 X，实际它不做"。
    """
    from backend.agents.protocol import directory, health as a2a_health

    return {"items": directory(), "health": a2a_health()}


@router.get("/meta/skills", summary="技能清单")
def skills() -> dict[str, Any]:
    """Skills 清单（判据表）。用于说明"一次交付应当包含哪些步骤"。"""
    from backend.skills import list_skills, skills_status

    return {"items": list_skills(), "status": skills_status()}


@router.get("/meta/intents", summary="意图与路由表")
def intents() -> dict[str, Any]:
    from backend.agents.harness.router import INTENT_ROUTES

    return {
        "items": [
            {"intent": k, "label": v.get("label", k), "nodes": list(v.get("nodes") or []),
             "description": v.get("description", "")}
            for k, v in INTENT_ROUTES.items()
        ]
    }


@router.get("/meta/config", summary="运行后端与配置（管理员）")
def config_snapshot(admin: RequireAdmin, request: Request) -> dict[str, Any]:
    """启动自检里打印的那份「实际生效后端」。管理员可见详细值。

    **完整的启动自检结果只在这里给，不进 ``/health``。** 它含有初始账号名
    （``seed.created``）、未映射的 ``.env`` 键、结构对账明细 —— 而 ``/health``
    是**免登录**的探针，把这份东西挂上去等于对外提供一份用户名枚举表。
    本接口要管理员，所以可以如实呈现（这也是 ``run_startup_check`` 的
    docstring 一直声称、此前却没有任何地方实现的那件事）。
    """
    check = settings.unmapped_env_keys()
    return {
        "backends": settings.runtime_backends(),
        "unmapped_env_keys": check,
        "app": {"name": settings.APP_NAME, "version": settings.APP_VERSION, "env": settings.ENV},
        "limits": {
            "max_steps": settings.MAX_STEPS,
            "tool_timeout": settings.TOOL_TIMEOUT,
            "statute_search_max_calls": settings.STATUTE_SEARCH_MAX_CALLS,
            "case_search_max_calls": settings.CASE_SEARCH_MAX_CALLS,
            "max_upload_mb": settings.MAX_UPLOAD_MB,
        },
        "compliance": {
            "desensitize_enabled": settings.DESENSITIZE_ENABLED,
            "audit_enabled": settings.AUDIT_ENABLED,
            "trust_proxy_headers": settings.TRUST_PROXY_HEADERS,
            "disclaimer": settings.DISCLAIMER,
        },
        # 启动自检的完整结果（建表对账、初始账号、工具/技能自检、预热）。
        # 没有的话写 None 而不是省掉这个键 —— 省掉会被读成"这次没做自检"。
        "startup_check": getattr(request.app.state, "startup_check", None),
    }


class HealthResponse(BaseModel):
    """供 OpenAPI 文档展示的响应模型（实际返回是 dict，字段可能随降级而变）。"""

    status: str
    app: str
    version: str
    env: str


# --------------------------------------------------------------------------- 根路径探针
# 负载均衡 / K8s / Nginx upstream check 探的是 ``/health``，不是 ``/api/v1/health``。
# 这里把它挂到根上，**复用上面同一个函数**而不是再写一份实现 ——
# 两份健康检查必然分叉，然后出现"LB 说健康、我们看接口是红的"这种最难查的故障。
#
# ``include_in_schema=False``：``/health`` 与 ``/health/ready`` 在
# ``/api/v1/...`` 那份里已经有文档了，根路径这份只是别名，不必在 OpenAPI 里重复出现
# （重复会让生成的客户端 SDK 里多出两个语义相同的方法）。
probe_router = APIRouter(include_in_schema=False, tags=["元信息"])
probe_router.add_api_route("/health", health, methods=["GET"], summary="健康检查（根路径别名）")
probe_router.add_api_route("/health/ready", ready, methods=["GET"], summary="就绪探针（根路径别名）")


__all__ = ["probe_router", "router"]
