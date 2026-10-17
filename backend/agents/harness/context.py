"""Harness 运行上下文（RunContext）。

一次 Agent 任务运行期间需要到处传递的东西：身份、审计主体、Trace 收集器、
工具执行器、记忆。如果靠函数参数一个个往下传，节点签名会膨胀到无法维护；
用全局变量又会让并发任务互相污染。

折中方案：**显式传递一个 RunContext 对象**——节点签名只多一个 ``ctx``，
但并发安全、可测试、可在日志里一眼看出是谁在跑。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.agents.harness.trace import Tracer


@dataclass
class RunContext:
    """一次任务运行的上下文。"""

    task_id: str = ""
    trace_id: str = ""
    case_id: str = ""
    user_id: int | None = None
    username: str = ""
    team_id: int | None = None
    client: str = "api"          # exe / web / api
    ip: str = ""
    is_admin: bool = False

    tracer: Tracer = field(default_factory=Tracer)
    notes: list[str] = field(default_factory=list)
    # 已访问过的材料路径，供审计去重（同一次任务里重复读同一材料只记一次）
    visited_materials: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.tracer.task_id != self.task_id or self.tracer.trace_id != self.trace_id:
            self.tracer.task_id = self.task_id
            self.tracer.trace_id = self.trace_id

    # ---------------------------------------------------------------- 审计
    def audit(self, action: str, **kw: Any) -> dict[str, Any]:
        """带上下文的审计快捷入口。"""
        from backend.agents.harness.audit import audit_writer

        return audit_writer.record(
            action,
            trace_id=self.trace_id,
            task_id=self.task_id,
            case_id=self.case_id,
            user_id=self.user_id,
            username=self.username,
            team_id=self.team_id,
            client=self.client,
            ip=self.ip,
            **kw,
        )

    def audit_material(self, material: str, *, resource: str = "case_material") -> None:
        """材料访问审计（同任务内同一材料只记一次，避免审计被刷爆）。"""
        key = f"{resource}:{material}"
        if key in self.visited_materials:
            return
        self.visited_materials.add(key)
        self.audit("material.read", resource=resource, resource_id=material,
                   detail=f"任务 {self.task_id} 读取材料")

    # ---------------------------------------------------------------- Trace
    def note(self, text: str) -> None:
        """运行期备注，最终会出现在报告与任务记录里。"""
        if text and text not in self.notes:
            self.notes.append(text)

    def identity(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "trace_id": self.trace_id,
            "case_id": self.case_id,
            "username": self.username,
            "client": self.client,
        }


def make_context(state: dict[str, Any] | None = None, **overrides: Any) -> RunContext:
    """从 State 构造 RunContext（State 里的字段优先，overrides 覆盖）。"""
    state = state or {}
    kwargs: dict[str, Any] = {
        "task_id": state.get("task_id", ""),
        "trace_id": state.get("trace_id", ""),
        "case_id": state.get("case_id", ""),
    }
    uid = state.get("user_id")
    if uid not in (None, ""):
        try:
            kwargs["user_id"] = int(uid)
        except (TypeError, ValueError):
            kwargs["user_id"] = None
    kwargs.update({k: v for k, v in overrides.items() if v is not None})
    return RunContext(**kwargs)
