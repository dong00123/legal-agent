"""审计日志表。**只允许插入，不允许删改**（对应方案第十四章）。"""

from __future__ import annotations

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from backend.app.db.base import Base, TimestampMixin
from backend.app.db.models.user import MYSQL_ARGS


class AuditLog(Base, TimestampMixin):
    """谁、何时、访问了什么材料 / 触发了哪次检索与引用。"""

    __tablename__ = "la_audit_log"
    __table_args__ = (MYSQL_ARGS,)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(48), index=True, default="")
    task_id: Mapped[str] = mapped_column(String(48), index=True, default="")
    case_id: Mapped[str] = mapped_column(String(48), index=True, default="")

    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    team_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    action: Mapped[str] = mapped_column(String(48), index=True, comment="动作：material.read / statute.search ...")
    resource: Mapped[str] = mapped_column(String(128), default="", comment="资源类型")
    resource_id: Mapped[str] = mapped_column(String(128), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    client: Mapped[str] = mapped_column(String(32), default="", comment="exe / web / api")
    ip: Mapped[str] = mapped_column(String(64), default="")
    result: Mapped[str] = mapped_column(String(16), default="ok", comment="ok / denied / error")
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
