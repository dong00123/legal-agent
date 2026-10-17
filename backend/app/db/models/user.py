"""用户、团队与权限模型（对应方案第十二章「权限隔离到本人/本团队」）。"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.db.base import Base, TimestampMixin

MYSQL_ARGS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"}


class Team(Base, TimestampMixin):
    """律所内部团队。案件按团队隔离。"""

    __tablename__ = "la_team"
    __table_args__ = (UniqueConstraint("name", name="uq_team_name"), MYSQL_ARGS)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), comment="团队名，如「诉讼一组」")
    description: Mapped[str] = mapped_column(String(512), default="", comment="说明")
    case_isolation: Mapped[bool] = mapped_column(
        Boolean, default=True, comment="是否开启案件隔离（本团队可见）"
    )

    users: Mapped[list["User"]] = relationship(back_populates="team")


class User(Base, TimestampMixin):
    """律师 / 管理员账号。EXE 端与 Web 端共用同一账号体系。"""

    __tablename__ = "la_user"
    __table_args__ = (UniqueConstraint("username", name="uq_user_username"), MYSQL_ARGS)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), comment="登录名")
    hashed_password: Mapped[str] = mapped_column(String(256), comment="口令散列（PBKDF2-SHA256）")
    full_name: Mapped[str] = mapped_column(String(64), default="", comment="姓名")
    role: Mapped[str] = mapped_column(
        String(16), default="lawyer", comment="角色：admin / lawyer"
    )
    license_no: Mapped[str] = mapped_column(String(64), default="", comment="执业证号")
    team_id: Mapped[int | None] = mapped_column(ForeignKey("la_team.id"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[str] = mapped_column(String(32), default="")

    team: Mapped[Team | None] = relationship(back_populates="users")

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"
