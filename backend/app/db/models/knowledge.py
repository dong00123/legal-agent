"""知识库 / 法规库 / 文书草稿 / 系统配置模型。

- ``KnowledgeDoc``：律所知识库（办案指引、审查清单、合同模板），带版本与生效控制；
- ``StatuteDoc``：法规库版本与更新包管理，支撑「现行有效/已修订/已废止」标注；
- ``DraftRecord``：文书草稿，支撑双端（EXE ↔ Web）继续编辑；
- ``SystemConfig``：模型路由 / Agent 参数 / 降级开关，管理员可运行时调整。
"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from backend.app.db.base import Base, TimestampMixin
from backend.app.db.models.user import MYSQL_ARGS


class KnowledgeDoc(Base, TimestampMixin):
    """律所知识库文档。所有引用必须标注指引版本。"""

    __tablename__ = "la_knowledge_doc"
    __table_args__ = (MYSQL_ARGS,)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(256), index=True)
    doc_type: Mapped[str] = mapped_column(
        String(32), default="guideline", comment="guideline 办案指引 / checklist 审查清单 / template 合同模板"
    )
    category: Mapped[str] = mapped_column(String(64), default="", comment="业务领域，如 劳动法 / 知识产权")
    version: Mapped[str] = mapped_column(String(32), default="v1.0")
    effective_date: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(16), default="active", comment="active / draft / archived")
    stored_path: Mapped[str] = mapped_column(String(512), default="")
    content: Mapped[str] = mapped_column(Text, default="", comment="正文（用于入库切分）")
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("la_user.id"), nullable=True)
    indexed: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否已写入索引")


class StatuteDoc(Base, TimestampMixin):
    """法规库文档。离线更新包导入后在此登记版本与效力状态。"""

    __tablename__ = "la_statute_doc"
    __table_args__ = (MYSQL_ARGS,)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_name: Mapped[str] = mapped_column(String(256), index=True, comment="法规名称（含书名号）")
    doc_no: Mapped[str] = mapped_column(String(128), default="", comment="文号")
    category: Mapped[str] = mapped_column(String(64), default="", comment="劳动法 / 知识产权 / 民法典 ...")
    level: Mapped[str] = mapped_column(String(32), default="", comment="法律 / 行政法规 / 司法解释 / 地方性法规")
    publisher: Mapped[str] = mapped_column(String(128), default="")
    publish_date: Mapped[str] = mapped_column(String(32), default="")
    effective_date: Mapped[str] = mapped_column(String(32), default="")
    repealed_date: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(
        String(16), default="现行有效", comment="现行有效 / 已修订 / 已废止 / 尚未生效"
    )
    version: Mapped[str] = mapped_column(String(32), default="")
    stored_path: Mapped[str] = mapped_column(String(512), default="")
    article_count: Mapped[int] = mapped_column(Integer, default=0)
    indexed: Mapped[bool] = mapped_column(Boolean, default=False)
    update_pack: Mapped[str] = mapped_column(String(128), default="", comment="来源更新包")


class DraftRecord(Base, TimestampMixin):
    """文书草稿。Web 端创建的草稿可同步至 EXE 继续编辑（同一账号体系）。"""

    __tablename__ = "la_draft"
    __table_args__ = (MYSQL_ARGS,)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    draft_id: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    task_id: Mapped[str] = mapped_column(String(48), index=True, default="")
    case_id: Mapped[str] = mapped_column(String(48), index=True, default="")
    user_id: Mapped[int | None] = mapped_column(ForeignKey("la_user.id"), nullable=True)
    doc_type: Mapped[str] = mapped_column(
        String(32), default="complaint", comment="complaint 起诉状 / defense 答辩状 / opinion 意见书 / revision 修改稿"
    )
    title: Mapped[str] = mapped_column(String(256), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    citations: Mapped[list] = mapped_column(JSON, default=list)
    exported_path: Mapped[str] = mapped_column(String(512), default="")

    # ---- 模板型文书（编辑页）----------------------------------------------
    # 这四列都是后加的：`reconcile_schema` 只做 `ALTER TABLE … ADD COLUMN`，
    # 对已存在的库**一律补成 NULL**（不填 Python 侧的 default）。所以所有读路径
    # 都必须按 `or ""` / `or []` 的风格吃 None —— 否则"新草稿好用、打开旧草稿 500"。
    template_id: Mapped[str] = mapped_column(
        String(128), default="", comment="命中的模板 id；空表示非模板型文书")
    fills: Mapped[dict] = mapped_column(
        JSON, default=dict, comment="模板字段值 {字段id: 值}，AI 修改改的就是这一层")
    content_html: Mapped[str] = mapped_column(
        Text, default="", comment="编辑页的富文本正文；非空表示律师手动排版过")
    # 列名用 chat_log 而不是 chat：不是保留字冲突，而是紧挨着已有的
    # `la_chat_turn` 表，叫 chat 读起来像同一件事。且列名一旦定下就改不了 ——
    # reconcile_schema 只有加列、没有重命名路径。
    chat_log: Mapped[list] = mapped_column(
        JSON, default=list, comment="编辑页左侧对话（起草需求 + 每次 AI 修改指令）")


class DraftVersion(Base, TimestampMixin):
    """草稿的历史版本快照。

    **为什么不复用 ``la_draft`` 自己。** ``save_draft`` 每次把 ``version`` 加一但
    **覆盖 ``content``** —— 版本号只增，旧正文不留。对"改了两段发现不如原来那版"
    这个高频场景，退路实际是不存在的。这张表把每一版真正存下来。

    ``source`` 区分三来源：首次生成 / AI 改字段 / 手动编辑 —— 律师回看历史时
    "这版是我自己改的还是 AI 改的"很关键，合并成一个值就说不清了。
    """

    __tablename__ = "la_draft_version"
    __table_args__ = (MYSQL_ARGS,)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    draft_id: Mapped[str] = mapped_column(String(48), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    source: Mapped[str] = mapped_column(
        String(16), default="manual", comment="compose 首次生成 / ai_revise AI 改字段 / manual 手动编辑")
    note: Mapped[str] = mapped_column(String(256), default="", comment="摘要：改了哪几栏 / 指令原文")
    content: Mapped[str] = mapped_column(Text, default="")
    content_html: Mapped[str] = mapped_column(Text, default="")
    fills: Mapped[dict] = mapped_column(JSON, default=dict)
    template_id: Mapped[str] = mapped_column(String(128), default="")


class SystemConfig(Base, TimestampMixin):
    """运行时系统配置（管理员可改）：模型路由、Agent 参数、降级开关。"""

    __tablename__ = "la_system_config"
    __table_args__ = (MYSQL_ARGS,)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    value: Mapped[str] = mapped_column(Text, default="")
    value_type: Mapped[str] = mapped_column(String(16), default="str", comment="str / int / float / bool / json")
    group: Mapped[str] = mapped_column(String(32), default="agent", comment="model / agent / degrade / rag")
    description: Mapped[str] = mapped_column(String(256), default="")
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("la_user.id"), nullable=True)


class UpdatePack(Base, TimestampMixin):
    """法规库离线更新包记录（律所定期导入最新法规）。"""

    __tablename__ = "la_update_pack"
    __table_args__ = (MYSQL_ARGS,)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pack_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    filename: Mapped[str] = mapped_column(String(256), default="")
    stored_path: Mapped[str] = mapped_column(String(512), default="")
    doc_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="imported", comment="imported / failed / rolled_back")
    detail: Mapped[str] = mapped_column(Text, default="")
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("la_user.id"), nullable=True)
