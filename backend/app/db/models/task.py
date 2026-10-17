"""异步任务记录。支撑 FR13「双端任务互通」与任务中心。"""

from __future__ import annotations

from sqlalchemy import Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from backend.app.db.base import Base, TimestampMixin
from backend.app.db.models.user import MYSQL_ARGS


class TaskRecord(Base, TimestampMixin):
    """一次 Agent 任务。EXE 端发起的任务可在 Web 端查看进度与结果。"""

    __tablename__ = "la_task"
    __table_args__ = (MYSQL_ARGS,)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    case_id: Mapped[str] = mapped_column(String(48), index=True, default="")
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    team_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    kind: Mapped[str] = mapped_column(
        String(32), default="chat", comment="chat / contract_review / doc_draft / batch_parse ..."
    )
    intent: Mapped[str] = mapped_column(String(32), default="", comment="Router 判定的意图")
    query: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(
        String(16), default="pending", comment="pending / running / succeeded / failed / degraded"
    )
    progress: Mapped[int] = mapped_column(Integer, default=0, comment="0-100")

    result: Mapped[dict] = mapped_column(JSON, default=dict, comment="最终输出（Report + 引用清单）")
    state: Mapped[dict] = mapped_column(JSON, default=dict, comment="State 快照（可回放）")
    tool_calls: Mapped[list] = mapped_column(JSON, default=list, comment="工具调用 Trace")
    trace: Mapped[list] = mapped_column(JSON, default=list, comment="全链路 Trace")
    evidence_gap: Mapped[list] = mapped_column(JSON, default=list, comment="证据缺口声明")

    citation_check: Mapped[str] = mapped_column(String(16), default="pending")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    source_client: Mapped[str] = mapped_column(String(16), default="web", comment="exe / web")
    error: Mapped[str] = mapped_column(Text, default="")
    finished_at: Mapped[str] = mapped_column(String(32), default="")
