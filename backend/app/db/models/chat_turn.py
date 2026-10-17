"""AI 助手页（纯 RAG 链路）的历史会话记录。

**一行 = 一轮问答。** 会话的元数据（``title`` / ``status`` / ``user_id`` / ``case_id``）
冗余写在每一行上，其中 ``title`` 只在 ``turn_index == 0`` 那行非空。
这样「列出全部会话」就是 ``WHERE turn_index = 0 AND status = 'active'`` ——
没有 GROUP BY、没有 join；追加一轮是**纯 INSERT**（无读-改-写竞态），
标记删除整段会话是**一条 UPDATE**。拆成「会话表 + 轮次表」要把这三件事各变复杂一档。

⚠️ 与 ``la_task`` 无关。``/chat/rag`` 不写任务表（``tests/test_api.py`` 的
``test_rag_chat_does_not_write_a_task`` 把这条钉成了契约），本表只是那个入口
自己的留痕，不进任务中心、不参与相似问题缓存。
"""

from __future__ import annotations

from sqlalchemy import Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from backend.app.db.base import Base, TimestampMixin
from backend.app.db.models.user import MYSQL_ARGS


class ChatTurn(Base, TimestampMixin):
    """AI 助手会话的一轮问答。``payload`` 是整份 ``RagAnswer.to_dict()``。"""

    __tablename__ = "la_chat_turn"
    # ``conversation_id`` **绝不能**单独建 unique —— 它是会话号不是轮次号，
    # 建成 unique 之后第二轮追问就会 IntegrityError（表现为 500，且那一轮的答案丢掉）。
    # 唯一性只由 (会话号, 轮次) 这对组合承担。
    __table_args__ = (
        UniqueConstraint("conversation_id", "turn_index", name="uq_chat_turn_conv_seq"),
        MYSQL_ARGS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        String(48), index=True, comment="会话号 conv_<hex12>"
    )
    turn_index: Mapped[int] = mapped_column(Integer, default=0, comment="轮次，从 0 开始")
    title: Mapped[str] = mapped_column(
        String(64), default="", comment="会话标题（首轮提问截断）；只写在 turn_index=0 那行"
    )
    # 与 ``TaskRecord`` 同款：裸 Integer + index，不建外键。
    # 本表从不 join ``la_user``，也没有级联删除的语义需求。
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    case_id: Mapped[str] = mapped_column(String(48), index=True, default="")
    status: Mapped[str] = mapped_column(
        String(16), default="active", index=True, comment="active / deleted（标记删除，不真删）"
    )
    # 整份 to_dict() 原样存：citations（行内 [S1]/[C1] 靠它才能点开）、
    # ref_check.unsupported（「查无此条」告警）、卡片顺序与 occurrences 全在里面。
    # 任何"只存正文"的省法都会让重放出来的历史标签点不开。
    payload: Mapped[dict] = mapped_column(JSON, default=dict, comment="整份 RagAnswer.to_dict()")
