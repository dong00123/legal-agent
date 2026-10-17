"""引用记录表。Citation Validator 的核对结果全部留痕，满足执业责任追溯。"""

from __future__ import annotations

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base, TimestampMixin
from backend.app.db.models.user import MYSQL_ARGS


class Citation(Base, TimestampMixin):
    """单条引用（法条 / 判例 / 知识库）的终审记录。"""

    __tablename__ = "la_citation"
    __table_args__ = (MYSQL_ARGS,)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(48), index=True, comment="所属任务")
    case_id: Mapped[str] = mapped_column(String(48), index=True, default="")

    ctype: Mapped[str] = mapped_column(String(16), default="statute", comment="statute / case / knowledge")
    name: Mapped[str] = mapped_column(String(256), default="", comment="法规名 / 判例标题")
    article: Mapped[str] = mapped_column(String(64), default="", comment="条文号")
    case_no: Mapped[str] = mapped_column(String(96), default="", comment="案号")
    court: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(16), default="", comment="效力状态")
    version: Mapped[str] = mapped_column(String(64), default="")
    source_path: Mapped[str] = mapped_column(String(512), default="")
    raw: Mapped[str] = mapped_column(Text, default="", comment="原始引用文本")

    verified: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否通过存在性校验")
    reason: Mapped[str] = mapped_column(String(512), default="", comment="校验结论")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
