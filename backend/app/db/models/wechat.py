"""微信机器人接入的绑定模型（2026-10-11）。

为什么需要一张表而不是把绑定写进 Redis：绑定关系决定「微信里那个 peer 以谁的
身份动案件」，是**权限的来源**。权限来源放在会过期的缓存里，一次 Redis 清库
就会让已绑定的律师突然"变成未绑定"（或者更糟：如果实现里默认放行，就变成越权）。
所以它跟用户表一样落在关系库里。

两张表分工明确：

- :class:`WechatBindCode` —— 一次性绑定码。律师在 Web 工作台取码，在微信里发
  ``/绑定 <码>``。**用掉即失效**（``used``），且有 TTL，防止码被反复使用或长期
  留在聊天记录里被翻出来。
- :class:`WechatBinding` —— 绑定关系本身。一个微信 peer 只能绑一个律师账号
  （``peer_id`` 唯一）；一个律师可以绑多个 peer（工作号 + 个人号是常见情形）。
  ``case_id`` 是"当前正在办理的案件"，让后续直接发图片就落进对的那个案件。

⚠️ 与 ``la_case`` 一样，``case_id`` **不建外键**：它是字符串业务键（见
``case.py`` 里那段说明，MySQL 对非主键字符串列建外键有额外要求且容易失败）。
归属关系由应用层维护，查询一律带 ``case_id`` 过滤。
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base, TimestampMixin
from backend.app.db.models.user import MYSQL_ARGS


class WechatBinding(Base, TimestampMixin):
    """微信 peer ↔ 律师账号的绑定关系。"""

    __tablename__ = "la_wechat_binding"
    __table_args__ = (MYSQL_ARGS,)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 微信侧的用户 id（iLink 的 ``from_user_id``）。**唯一**：一个 peer 只认一个律师。
    peer_id: Mapped[str] = mapped_column(String(128), unique=True, index=True,
                                         comment="微信用户 id（iLink from_user_id）")
    # 机器人账号 id。多机器人时用它区分"是哪台机器人在服务这个 peer"，
    # 也便于按机器人侧停用检查。留空表示未记录（单机器人部署）。
    bot_id: Mapped[str] = mapped_column(String(128), default="", comment="微信机器人账号 id")
    user_id: Mapped[int] = mapped_column(ForeignKey("la_user.id"), index=True,
                                         comment="绑定的律师账号")
    # 当前办理的案件编号（空 = 还没选）。发图片直接落到这个案件。
    case_id: Mapped[str] = mapped_column(String(48), default="", comment="当前选择的案件编号")
    # 微信侧显示名（昵称）。只用于让律师在 Web 端认出"这是我的哪个号"，不参与鉴权。
    display_name: Mapped[str] = mapped_column(String(128), default="", comment="微信昵称")


class WechatBindCode(Base, TimestampMixin):
    """一次性绑定码：Web 端取码 → 微信里 ``/绑定 <码>``。"""

    __tablename__ = "la_wechat_bind_code"
    __table_args__ = (MYSQL_ARGS,)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, comment="绑定码")
    user_id: Mapped[int] = mapped_column(ForeignKey("la_user.id"), index=True,
                                         comment="取码的律师账号")
    # ISO 秒级字符串，与项目其它时间字段（case.evidence_chain_at）口径一致。
    # 用字符串而不是 DateTime 是为了让"过期判定"在 SQLite / MySQL 上完全相同。
    expires_at: Mapped[str] = mapped_column(String(32), default="", comment="过期时间（ISO）")
    used: Mapped[int] = mapped_column(Integer, default=0, comment="0 未用 / 1 已用")
