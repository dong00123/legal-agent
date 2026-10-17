"""案件档案与材料模型（对应方案第九章「长期结构化记忆」）。"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from backend.app.db.base import Base, TimestampMixin
from backend.app.db.models.user import MYSQL_ARGS


class Case(Base, TimestampMixin):
    """案件档案。存储脱敏后的案件摘要与争议焦点归档。"""

    __tablename__ = "la_case"
    __table_args__ = (MYSQL_ARGS,)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(String(48), unique=True, index=True, comment="案件编号")
    title: Mapped[str] = mapped_column(String(256), default="", comment="案件名称")
    case_type: Mapped[str] = mapped_column(
        String(32), default="litigation", comment="litigation / contract / consult"
    )
    cause_of_action: Mapped[str] = mapped_column(String(128), default="", comment="案由")
    client_name: Mapped[str] = mapped_column(String(128), default="", comment="委托人（脱敏）")
    opponent_name: Mapped[str] = mapped_column(String(128), default="", comment="对方当事人（脱敏）")
    stage: Mapped[str] = mapped_column(String(32), default="intake", comment="案件阶段")
    status: Mapped[str] = mapped_column(String(32), default="active", comment="active / closed")

    owner_id: Mapped[int | None] = mapped_column(ForeignKey("la_user.id"), nullable=True, index=True)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("la_team.id"), nullable=True, index=True)

    summary: Mapped[str] = mapped_column(Text, default="", comment="脱敏案件摘要")
    dispute_issues: Mapped[list] = mapped_column(
        JSON, default=list, comment="争议焦点归档 [{title, description}]"
    )
    outcome: Mapped[str] = mapped_column(Text, default="", comment="办理结果")
    tags: Mapped[list] = mapped_column(JSON, default=list, comment="标签")
    # 证据链结构化结果 [{claim, evidence:[{material, excerpt, support}], gap}]
    # 由「生成证据链」功能写入，前端案件详情页的证据链标签读取展示。
    evidence_chain: Mapped[list] = mapped_column(JSON, default=list, comment="证据链结构化结果")
    evidence_chain_at: Mapped[str] = mapped_column(
        String(32), default="", comment="证据链最近生成时间（ISO 秒级）"
    )
    # 胜诉把握评估 —— **只存模型写的那半，不存数字**。
    #
    # 数字（证据覆盖率）是 ``case_service.score_win_rate`` 按证据链现算的，纯确定性、
    # 零成本，所以每次 GET 重算即可 —— 落库反而制造"现在显示的是哪一次的分数"的歧义，
    # 而且将来调一次权重就得回填全库。落库的只有贵的、带 provider/model 元信息的
    # AI 法律分析。
    # 形状：{chain_at, reasons[], risks[], next_moves[], generated_at, provider, model, degraded}
    # 其中 ``chain_at`` 记的是**做这次结论时**的 evidence_chain_at（不是 now），
    # 读回时跟当前 evidence_chain_at 比对就能判出「证据链变了，分析过期了」。
    win_assessment: Mapped[dict] = mapped_column(JSON, default=dict, comment="胜诉把握·AI 法律分析")
    win_assessment_at: Mapped[str] = mapped_column(
        String(32), default="", comment="胜诉把握分析最近生成时间（ISO 秒级）"
    )
    # 办理进度流水 [{stage, note, operator, task_id, result, recorded_at}]
    # 由 business_tools.case_progress_record 追加写入，用于案件管理系统对接与进度追溯。
    progress: Mapped[list] = mapped_column(JSON, default=list, comment="办理进度流水")

    materials: Mapped[list["CaseMaterial"]] = relationship(
        back_populates="case",
        cascade="all, delete-orphan",
        # 因未建数据库外键，这里必须显式指定 join 条件，否则 SQLAlchemy 会抛
        # NoForeignKeysError: Could not determine join condition ...
        primaryjoin="foreign(CaseMaterial.case_id) == Case.case_id",
        foreign_keys="CaseMaterial.case_id",
    )


class CaseMaterial(Base, TimestampMixin):
    """案件材料。EXE 端本地不落盘原文，材料统一存于服务端并按权限隔离。"""

    __tablename__ = "la_case_material"
    __table_args__ = (MYSQL_ARGS,)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 注意：这里**不建外键**。case_id 是字符串业务键，MySQL 对「非主键字符串列建外键」
    # 有额外要求（且跨字符集/排序规则时容易失败），实测报
    # "Failed to add the foreign key constraint. Missing column 'case_id' ..."。
    # 归属关系由应用层维护，CaseMaterial 的查询都带 case_id 过滤。
    case_id: Mapped[str] = mapped_column(String(48), index=True)
    filename: Mapped[str] = mapped_column(String(256), default="")
    stored_path: Mapped[str] = mapped_column(String(512), default="", comment="服务端存储路径")
    doc_type: Mapped[str] = mapped_column(String(32), default="unknown")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    parsed: Mapped[bool] = mapped_column(Integer, default=0)
    parsed_path: Mapped[str] = mapped_column(String(512), default="")
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("la_user.id"), nullable=True)

    case: Mapped[Case] = relationship(
        back_populates="materials",
        primaryjoin="Case.case_id == foreign(CaseMaterial.case_id)",
        foreign_keys="CaseMaterial.case_id",
    )
