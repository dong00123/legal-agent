"""Harness Tool Executor：工具执行 + 权限隔离 + 审计 + Trace。

方案第五章原则：**所有案件材料 Tool 做权限隔离与访问审计**；
方案 4.4 节：Tool Executor 是 Harness 里对接 MCP / API / Function 的那一层。

这一层与 :class:`~backend.tools.base.ToolExecutor` 的分工必须说清楚，否则容易混淆：
- :class:`~backend.tools.base.ToolExecutor`：**单次调用内**的超时/重试/退避/fallback；
- :class:`ToolInvoker`（本模块）：**跨调用**的组织级约束 —— 权限、审计留痕、
  Trace 记账、连续失败熔断、把降级结论写进 ``evidence_gap``。

权限拒绝走**终止**路径（不降级）：如果律师越权访问他人案件材料，
正确行为是明确报错并留痕，而不是"拿不到材料就猜"。
"""

from __future__ import annotations

import logging
from typing import Any

from backend.agents.harness.context import RunContext
from backend.agents.harness.error_handler import ErrorHandler, error_handler
from backend.agents.harness.state_manager import add_evidence_gap, record_tool_call
from backend.agents.state import LegalState
from backend.app.config import settings
from backend.tools.base import ToolResult
from backend.tools.registry import get_tool

logger = logging.getLogger("legal_agent.harness.executor")

# 需要案件访问权限的工具
CASE_SCOPED_TOOLS = {
    "material_parse", "material_ocr", "material_cross_compare",
    "case_progress_record", "review_checklist_export",
}

# 本地文件路径参数名：所有涉及磁盘路径的工具都要做目录围栏
PATH_ARGS = ("path", "paths")


class ToolInvoker:
    """带权限 / 审计 / Trace 的工具调用器。"""

    def __init__(self, ctx: RunContext, state: LegalState,
                 handler: ErrorHandler | None = None) -> None:
        self.ctx = ctx
        self.state = state
        self.handler = handler or error_handler

    # ------------------------------------------------------------------ 主入口
    async def call(self, tool: str, agent: str, **kwargs: Any) -> ToolResult:
        """执行一个 MCP 工具。

        始终返回 :class:`ToolResult`（不抛异常），调用方按 ``ok`` / ``degraded``
        决定后续分支 —— 这样节点的异常处理路径只有一条，不会漏。
        """
        # 1) 工具存在性
        try:
            spec = get_tool(tool)
        except KeyError as exc:
            return self._reject(agent, tool, str(exc), kwargs)

        # 2) 连续失败熔断
        if self.handler.should_skip(tool):
            note = f"工具 {tool} 本次任务已熔断（连续失败 {settings.TOOL_MAX_RETRY + 1} 次）"
            add_evidence_gap(self.state, "tool", note, self.handler.impact(tool))
            return ToolResult(ok=False, tool=tool, error=note, degraded=True,
                              fallback_note=note)

        # 3) 权限与路径围栏
        denied = self._guard(tool, kwargs)
        if denied is not None:
            self.ctx.audit("access.denied", resource=tool, resource_id=agent,
                           result="denied", detail=denied)
            self.ctx.tracer.error(agent, f"denied:{tool}", denied)
            add_evidence_gap(self.state, "tool", denied, "本次任务无法继续，请人工处理")
            self.state["error"] = denied
            return ToolResult(ok=False, tool=tool, error=denied, degraded=False,
                              fallback_note="权限校验未通过，未执行")

        # 4) 执行
        try:
            result = await _invoke(spec, kwargs, task_id=self.ctx.task_id)
        except Exception as exc:  # noqa: BLE001 - 统一转成降级结论
            outcome = self.handler.handle(self.ctx, self.state, tool=tool,
                                          agent=agent, exc=exc)
            record_tool_call(self.ctx, self.state, tool=tool, agent=agent, args=kwargs,
                             ok=False, error=outcome["message"])
            return ToolResult(ok=False, tool=tool, error=outcome["message"],
                              degraded=True, fallback_note=outcome["message"])

        # 5) 记账 + 失败/成功状态维护
        record_tool_call(
            self.ctx, self.state, tool=tool, agent=agent, args=kwargs,
            ok=result.ok, elapsed_ms=result.elapsed_ms, retries=result.retries,
            error=result.error, degraded=result.degraded,
            note=f"attempts={result.attempts}",
        )
        if result.ok:
            self.handler.state.note_success(tool)
            self.ctx.audit("agent.retrieval", resource=tool, resource_id=agent,
                           detail=f"{_brief(kwargs)} → hits={_hit_count(result)}")
            if result.degraded and result.fallback_note:
                # 降级但可用：写进证据缺口，让律师看得见"这次结果是打折的"
                add_evidence_gap(self.state, _gap_kind(tool), result.fallback_note,
                                 self.handler.impact(tool))
        else:
            self.handler.handle(self.ctx, self.state, tool=tool, agent=agent,
                                exc=result.error or "工具返回失败")

        return result

    # ------------------------------------------------------------------ 权限
    def _guard(self, tool: str, kwargs: dict[str, Any]) -> str | None:
        """返回拒绝原因；通过则返回 None。"""
        if tool in CASE_SCOPED_TOOLS:
            case_id = str(kwargs.get("case_id") or self.ctx.case_id or "")
            if case_id:
                reason = self._check_case(case_id)
                if reason:
                    return reason
            if tool in ("case_progress_record",) and not case_id:
                return f"工具 {tool} 需要 case_id，但本次任务未关联案件"

        # 路径围栏：只允许访问项目 data 目录下的文件，防止提示注入把工具当文件读取器
        for key in PATH_ARGS:
            if key not in kwargs:
                continue
            value = kwargs[key]
            paths = value if isinstance(value, list) else [value]
            for p in paths:
                bad = _path_escape(str(p))
                if bad:
                    return bad
        return None

    def _check_case(self, case_id: str) -> str | None:
        from backend.app.db.models import Case
        from backend.app.db.session import session_scope
        from backend.app.security import CurrentUser, can_access_case

        try:
            with session_scope() as db:
                case = db.query(Case).filter(Case.case_id == case_id).one_or_none()
                if case is None:
                    return f"案件 {case_id} 不存在或无权限访问"
                owner_id, team_id = case.owner_id, case.team_id
        except Exception as exc:  # noqa: BLE001
            # 权限校验本身失败时必须"失败关闭"，不能放行
            return f"案件权限校验失败（{exc}），已拒绝访问"

        if self.ctx.user_id is None:
            return ""  # 内部任务（如批量入库）无登录主体，跳过隔离
        user = CurrentUser(self.ctx.user_id, self.ctx.username,
                           "admin" if self.ctx.is_admin else "lawyer", self.ctx.team_id)
        if not can_access_case(user, owner_id, team_id):
            self.ctx.note(f"越权访问被拒绝：任务 {self.ctx.task_id} 尝试访问案件 {case_id}")
            return f"无权访问案件 {case_id}（仅限本人或本团队案件）"
        return None

    # ------------------------------------------------------------------ 拒绝
    def _reject(self, agent: str, tool: str, reason: str, kwargs: dict[str, Any]) -> ToolResult:
        self.ctx.tracer.error(agent, f"unknown_tool:{tool}", reason)
        record_tool_call(self.ctx, self.state, tool=tool, agent=agent, args=kwargs,
                         ok=False, error=reason)
        return ToolResult(ok=False, tool=tool, error=reason, degraded=True,
                          fallback_note="工具不存在，已跳过")


async def _invoke(spec: Any, kwargs: dict[str, Any], *, task_id: str = "") -> ToolResult:
    """通过 registry 的统一入口调用（保证限额与 fallback 生效）。

    必须带上 ``task_id``：限额是**任务级**的，丢了它就会退回"按进程累计"，
    于是同一进程里第 4 个咨询请求的法规检索必然被拦死（而错误信息看起来
    还像是防死循环正常生效）。这个 bug 只在连续发起多个任务时才会暴露，
    单次调试永远发现不了。
    """
    from backend.tools.registry import call_tool

    return await call_tool(spec.name, task_id=task_id, **kwargs)


def _path_escape(path: str) -> str | None:
    """校验路径是否落在允许的目录内。"""
    from pathlib import Path

    from backend.app.config import DATA_DIR

    if not path:
        return None
    try:
        p = Path(path).resolve()
    except Exception:  # noqa: BLE001
        return f"路径无法解析：{path}"

    allowed_roots = [Path(settings.UPLOAD_DIR).resolve(),
                     Path(settings.RAW_DIR).resolve(),
                     Path(settings.PROCESSED_DIR).resolve(),
                     Path(settings.KNOWLEDGE_DIR).resolve(),
                     Path(DATA_DIR).resolve()]
    for root in allowed_roots:
        try:
            p.relative_to(root)
            return None
        except ValueError:
            continue
    return f"路径越界：{path} 不在允许的材料目录内（可能是恶意提示注入），已拒绝"


def _brief(kwargs: dict[str, Any]) -> str:
    for k in ("query", "path", "case_id", "doc_name"):
        if kwargs.get(k):
            return f"{k}={str(kwargs[k])[:120]}"
    return ",".join(kwargs)[:120]


def _hit_count(result: ToolResult) -> int:
    data = result.data
    if not isinstance(data, dict):
        return 0
    if isinstance(data.get("count"), int):
        return data["count"]
    for key in ("statutes", "cases", "knowledges", "results"):
        if isinstance(data.get(key), list):
            return len(data[key])
    return 0


def _gap_kind(tool: str) -> str:
    # 顺序有讲究：material_ocr 要在 startswith("material") 之前判断，
    # 否则会被误归到 knowledge 类，前端"证据缺口"分类就会失真。
    if tool == "material_ocr":
        return "vlm"
    if tool.startswith("statute"):
        return "statute"
    if tool.startswith("case"):
        return "case"
    if tool.startswith("material"):
        return "knowledge"
    return "tool"
