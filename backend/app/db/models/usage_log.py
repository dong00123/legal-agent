"""专家 / 技能使用记录表（监控中心）。

一行 = 一次专家对话或一次 AI 助手 / 产品入口问答（orchestrator 链路或纯 RAG 链路）。

与 ``la_task`` / ``la_chat_turn`` 独立：
- ``/chat/rag`` 不写任务表（``tests/test_api.py`` 的
  ``test_rag_chat_does_not_write_a_task`` 把这条钉成了契约），本表只做**轻量使用留痕**，
  不进任务中心、不参与相似问题缓存。
- ``expert_id`` 记录的是专家对话入口（``AgentAskRequest.expert_id``）传的专家 id；
  普通 AI 助手 / 产品入口为空串。
- 技能按意图命中（``backend.skills.skill_for_intent``）：命中时 ``skill_id`` 写技能 id、
  ``intent`` 写本次意图；未命中（普通提问）也记一行，两个字段均为空串 ——
  这样"技能使用率 = 命中技能行数 / 全部行数"可以直接在库里算出来。
"""

from __future__ import annotations

from sqlalchemy import Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base, TimestampMixin
from backend.app.db.models.user import MYSQL_ARGS


class UsageLog(Base, TimestampMixin):
    """专家 / 技能使用记录。``created_at`` 由 ``TimestampMixin`` 提供。"""

    __tablename__ = "la_usage_log"
    __table_args__ = (MYSQL_ARGS,)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True, comment="用户ID"
    )
    expert_id: Mapped[str] = mapped_column(
        String(64), default="", index=True, comment="专家 id；普通 AI 助手为空串"
    )
    skill_id: Mapped[str] = mapped_column(
        String(64), default="", index=True, comment="命中的技能 id；未命中为空串"
    )
    intent: Mapped[str] = mapped_column(
        String(32), default="", index=True, comment="本次对话意图（技能按它反查命中）"
    )
    confidence: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="回答置信度（RAG 链路无此值）"
    )
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0, comment="本次执行耗时（毫秒）")
