"""Harness Error Handler：异常分类、降级决策与收敛。

方案第十三章的链路：``Tool Call → Timeout → Retry（指数退避）→ Fallback → 律师手动 / Agent 继续``。
重试与退避在 :class:`~backend.tools.base.ToolExecutor` 里已经做了，本模块负责**更高一层**的判断：

- 这次失败是"可绕过的"（检索超时）还是"必须中止的"（权限拒绝、参数非法）？
- 绕过时如何写出**律师看得懂**的降级说明（而不是把堆栈丢给律师）？
- 同一个工具反复失败时，是否应该停止再试它（避免把 max_steps 全耗在一个坏工具上）？

法律场景的底线在 :data:`FATAL_ERRORS` 里：涉及权限与合规的异常**不允许降级继续**。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from backend.agents.harness.context import RunContext
from backend.agents.state import LegalState
from backend.agents.harness.state_manager import add_evidence_gap

logger = logging.getLogger("legal_agent.harness.error")


# --------------------------------------------------------------------------- 分类
FATAL_ERRORS: tuple[type[BaseException], ...] = (
    PermissionError,
    FileNotFoundError,
    ValueError,
)

# 这些工具的失败只影响结论完整度，可以降级继续
SOFT_TOOLS = {
    "statute_search", "case_search", "case_summarize", "case_no_verify",
    "material_ocr", "material_cross_compare", "statute_version_compare",
}

# 同一个工具连续失败多少次后不再尝试
MAX_CONSECUTIVE_FAILURES = 2


@dataclass
class FailureState:
    """一次运行内的失败统计。"""

    counts: dict[str, int] = field(default_factory=dict)
    consecutive: dict[str, int] = field(default_factory=dict)
    skipped: set[str] = field(default_factory=set)

    def note_failure(self, tool: str) -> None:
        self.counts[tool] = self.counts.get(tool, 0) + 1
        self.consecutive[tool] = self.consecutive.get(tool, 0) + 1
        if self.consecutive[tool] >= MAX_CONSECUTIVE_FAILURES:
            self.skipped.add(tool)

    def note_success(self, tool: str) -> None:
        self.consecutive[tool] = 0

    def should_skip(self, tool: str) -> bool:
        return tool in self.skipped


class ErrorHandler:
    """把异常翻译成"人话降级说明"，并决定是否还能继续。"""

    def __init__(self) -> None:
        self.state = FailureState()

    # ------------------------------------------------------------------ 判定
    @staticmethod
    def is_fatal(exc: BaseException) -> bool:
        return isinstance(exc, FATAL_ERRORS)

    def is_soft(self, tool: str) -> bool:
        """软失败工具：失败只降级，不中断。未知工具按软处理（宁可缩小结论范围）。"""
        return tool not in ("case_progress_record",)

    def should_skip(self, tool: str) -> bool:
        return self.state.should_skip(tool)

    # ------------------------------------------------------------------ 处理
    def handle(self, ctx: RunContext, state: LegalState, *, tool: str, agent: str,
               exc: BaseException | str, elapsed_ms: int = 0) -> dict[str, Any]:
        """登记一次失败，返回给调用方继续决策用的结论。"""
        msg = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
        fatal = isinstance(exc, BaseException) and self.is_fatal(exc)

        self.state.note_failure(tool)
        ctx.tracer.error(agent, f"error:{tool}", msg)

        readable = self.explain(tool, msg)
        if fatal:
            # 权限/参数类问题不允许"降级继续"——继续下去只会产出错误结论
            state["error"] = readable
            add_evidence_gap(state, "tool", readable, "本次任务无法继续，请人工处理")
            ctx.audit("agent.error", resource=tool, resource_id=agent,
                      result="error", detail=readable)
            return {"action": "abort", "message": readable, "fatal": True}

        add_evidence_gap(state, "tool", readable, self.impact(tool))

        action = "skip" if self.state.should_skip(tool) else "continue"
        if action == "skip":
            ctx.note(f"工具 {tool} 连续失败 {self.state.consecutive[tool]} 次，本次任务不再重试")

        ctx.audit("agent.tool_failed", resource=tool, resource_id=agent,
                  result="error", detail=readable)
        return {"action": action, "message": readable, "fatal": False,
                "failures": self.state.counts[tool]}

    @staticmethod
    def explain(tool: str, msg: str) -> str:
        """把技术错误翻译成律师能读懂的降级说明。"""
        table = {
            "statute_search": "法规库检索失败",
            "statute_validity_check": "法规时效性校验失败",
            "statute_version_compare": "法规版本比对失败",
            "case_search": "类案库检索失败",
            "case_summarize": "判例结构化提取失败",
            "case_no_verify": "案号真实性校验失败",
            "material_parse": "案件材料解析失败",
            "material_ocr": "扫描件识别失败",
            "material_cross_compare": "多文档交叉比对失败",
            "draft_generate": "文书草稿生成失败",
            "revision_draft_generate": "修改对照稿生成失败",
            "format_normalize": "文书格式规范化失败",
            "case_progress_record": "案件进度记录失败",
            "review_checklist_export": "审查意见清单导出失败",
        }
        head = table.get(tool, f"工具 {tool} 执行失败")
        return f"{head}（{msg}）"

    @staticmethod
    def impact(tool: str) -> str:
        """失败对结论的影响说明——写进 evidence_gap 的 impact 字段。"""
        table = {
            "statute_search": "法条依据可能不完整，结论的法律依据需律师自行核查",
            "case_search": "本次类案检索不可用，分析意见仅基于法规检索结果",
            "case_summarize": "判例细节缺失，仅能依据检索到的裁判要旨",
            "case_no_verify": "案号未能核验，相关判例引用不得作为正式引用",
            "material_parse": "材料要素提取不完整，事实认定需人工复核",
            "material_ocr": "扫描件内容缺失，可能存在未识别的关键事实",
            "material_cross_compare": "多文档一致性未校验，条款冲突可能未被发现",
            "statute_validity_check": "效力状态未能确认，引用前须人工核实是否已废止",
        }
        return table.get(tool, "相关结论的完整性与可靠性下降")

    # ------------------------------------------------------------------ 汇总
    def summary(self) -> dict[str, Any]:
        return {
            "failures": dict(self.state.counts),
            "consecutive": dict(self.state.consecutive),
            "skipped": sorted(self.state.skipped),
            "fatal": False,
        }


error_handler = ErrorHandler()
