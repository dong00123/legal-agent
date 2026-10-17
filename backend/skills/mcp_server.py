"""MCP 服务端：把 15 个 Tool 与 3 个 Skill 暴露给外部 MCP 客户端。

方案第五章的分工原文：「**MCP 解决 Agent 和工具之间怎么连接**」。
所以这个模块的定位是**边界适配器** —— 它只做三件事：
把 ``TOOL_REGISTRY`` 的声明翻译成 MCP 的 ``inputSchema``、
把 MCP 的调用转发给 ``call_tool()``、把结果原样返回。
**不做**任何业务逻辑，也**不绕开** Registry 直接调 handler。
好处是外部通过 MCP 调用的每一次检索，和 Agent 内部调用走的是同一条路径，
配额、审计、Trace、降级四条约束一模一样。

两个额外工具（``skill_list`` / ``skill_guide``）是给 A2A 对端用的：
方案说「A2A 解决 Agent 和 Agent 之间怎么协作」，而对端 Agent 要协作，
首先得知道本所的作业标准是几步、哪几步是必要的。把 Skill 也通过 MCP 暴露出去，
比在 A2A 消息里来回传流程描述更可靠。

**依赖是可选降级**：``mcp`` 包未安装时本模块不报错，只是 ``build_server()``
返回 ``None``，可用性由 :func:`mcp_server_status` 如实报告。
不要让一个协议适配层的缺失拖垮整个后端的启动。
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable

from backend.skills import SKILLS, get_skill, skills_status
from backend.tools.registry import TOOL_REGISTRY, call_tool, mcp_manifest

logger = logging.getLogger("legal_agent.skills.mcp")

SERVER_NAME = "legal-agent-mcp"


# --------------------------------------------------------------------------- 类型
_PY_TYPES: dict[str, Any] = {
    "str": str, "int": int, "float": float, "bool": bool, "list": list,
    "dict": dict, "list[str]": list, "list[dict]": list,
}


def _py_type(type_: str) -> Any:
    return _PY_TYPES.get((type_ or "str").strip(), str)


def _synth_signature(spec) -> inspect.Signature:
    """按 Registry 声明合成函数签名。

    为什么要合成：FastMCP 是从**函数签名**推导 ``inputSchema`` 的。
    如果所有工具都写成 ``async def fn(**kwargs)``，客户端看到的就是一个空对象，
    必填项、类型、默认值全部丢失 —— MCP 的"自描述"价值就没了。
    所以这里把 Registry 的声明反向合成成签名，让**注册表仍是唯一真源**，
    同时 MCP 客户端能拿到完整的 schema。
    """
    params: list[inspect.Parameter] = []
    for name, p in spec.params.items():
        ann = _py_type(p.get("type") or "str")
        if p.get("required"):
            params.append(inspect.Parameter(
                name, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=ann))
        else:
            params.append(inspect.Parameter(
                name, inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=ann, default=p.get("default")))
    return inspect.Signature(parameters=params)


def _make_tool_fn(spec) -> Callable[..., Any]:
    """为一个 Tool 生成 MCP 侧的可调用对象。"""

    async def _call(**kwargs: Any) -> dict[str, Any]:
        # 丢掉 None：MCP 客户端常把所有可选字段都填上 null，
        # 而 handler 里的 `if x is None:` 分支语义与"没传"不同（如 material_parse.do_vision）
        clean = {k: v for k, v in kwargs.items() if v is not None}
        result = await call_tool(spec.name, **clean)
        if not result.ok:
            return {
                "ok": False,
                "tool": spec.name,
                "error": result.error,
                "degraded": True,
                "fallback_note": result.fallback_note or "工具不可用，相关证据缺失",
            }
        payload = result.data if isinstance(result.data, dict) else {"value": result.data}
        # 降级信息必须随结果一起返回，不能只留在服务端日志里：
        # 调用方（无论是 Agent 还是外部客户端）需要据此判断结论的可用范围。
        payload["_meta"] = {
            "ok": True,
            "tool": spec.name,
            "degraded": result.degraded,
            "fallback_note": result.fallback_note,
            "elapsed_ms": result.elapsed_ms,
            "retries": result.retries,
        }
        return payload

    _call.__name__ = spec.name
    _call.__qualname__ = f"mcp_tools.{spec.name}"
    _call.__doc__ = f"{spec.description}\n\n类别：{spec.category}；幂等：{spec.idempotent}"
    try:
        _call.__signature__ = _synth_signature(spec)  # type: ignore[attr-defined]
    except (ValueError, TypeError) as exc:  # pragma: no cover
        logger.warning("工具 %s 签名合成失败（%s），MCP 侧将退化为无参数 schema", spec.name, exc)
    return _call


# --------------------------------------------------------------------------- 元工具
async def skill_list() -> dict[str, Any]:
    """列出本所已固化的办案 Skill 及其步骤数（供 A2A 对端了解作业标准）。"""
    return skills_status()


async def skill_guide(skill_id: str, intent: str = "") -> dict[str, Any]:
    """取某个 Skill 的完整作业路径与每步判据说明。

    Args:
        skill_id: Skill 标识（contract_review / case_research / doc_draft）。
        intent: 可替代 skill_id，按意图反查对应 Skill。
    """
    from backend.skills import skill_for_intent

    if not skill_id and intent:
        skill = skill_for_intent(intent)
        if skill is None:
            return {"ok": False, "error": f"意图 {intent} 未绑定任何 Skill"}
        skill_id = skill.id
    try:
        skill = get_skill(skill_id)
    except KeyError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **skill.to_dict()}


# --------------------------------------------------------------------------- 构建
def build_server(name: str = SERVER_NAME, *, host: str = "127.0.0.1", port: int = 8090):
    """构造 FastMCP 服务端。``mcp`` 包未安装时返回 ``None``。"""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        logger.warning("未安装 mcp 包，MCP 服务端不可用；"
                       "工具仍可通过 HTTP 接口 /api/v1/tools 调用")
        return None

    server = FastMCP(name, host=host, port=port)

    for spec in TOOL_REGISTRY.values():
        fn = _make_tool_fn(spec)
        server.tool(name=spec.name, description=spec.description)(fn)

    server.tool(name="skill_list",
                description="列出本所已固化的办案 Skill 及步骤数")(skill_list)
    server.tool(name="skill_guide",
                description="取某个 Skill 的完整作业路径（按 skill_id 或 intent）")(skill_guide)

    logger.info("MCP 服务端已构建：%d 个工具 + 2 个元工具", len(TOOL_REGISTRY))
    return server


def serve_stdio() -> None:
    """以 stdio 方式运行（EXE 端内嵌 MCP 时使用）。"""
    server = build_server()
    if server is None:
        raise RuntimeError("MCP 服务端不可用：请先安装 mcp 包（pip install mcp）")
    server.run(transport="stdio")


def serve_http(host: str = "127.0.0.1", port: int = 8090) -> None:
    """以 SSE / streamable-http 方式运行（供远程 Agent 接入）。

    注意：对外暴露时**必须**配合网络层鉴权（部署侧 nginx + token）。
    MCP 协议本身不含租户身份，本服务端只能做"工具级"的权限声明，
    真正的"这个人能不能看这个案子"必须靠外层网关与本服务的调用上下文。
    """
    server = build_server(host=host, port=port)
    if server is None:
        raise RuntimeError("MCP 服务端不可用：请先安装 mcp 包（pip install mcp）")
    server.run(transport="streamable-http")


# --------------------------------------------------------------------------- 状态
def mcp_server_status() -> dict[str, Any]:
    """供 /health 与管理后台展示的可用性状态。"""
    try:
        import mcp  # noqa: F401

        available, reason = True, ""
    except ImportError:
        available, reason = False, "未安装 mcp 包，MCP 服务端不可用（HTTP 工具接口不受影响）"

    manifest = mcp_manifest()
    return {
        "server": SERVER_NAME,
        "available": available,
        "reason": reason,
        "transport": ["stdio", "streamable-http"] if available else [],
        "tool_count": len(manifest["tools"]),
        "tools": [t["name"] for t in manifest["tools"]],
        "skills": [s.id for s in SKILLS.values()],
        "meta_tools": ["skill_list", "skill_guide"],
        "endpoints": {"manifest": "/api/v1/mcp/manifest", "tools": "/api/v1/tools"},
    }
