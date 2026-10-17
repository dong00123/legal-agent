"""采集存储模型（Playwright 浏览器自动化 —— 存储层）。

三张表各管一件事，**刻意不合并**：

- ``la_crawl_doc``    采集到的文书正文。双去重：``doc_id`` 唯一键（来源级唯一，
  防同一篇反复入库）+ ``content_hash``（正文级唯一，防同一篇在不同来源/不同
  doc_id 下重复入库）。
- ``la_crawl_quota``  单日配额计数，按 ``source × quota_date`` 一行。放在库里而不是
  进程内存：worker 可能多副本，内存计数会让实际抓取量翻倍，且进程重启即清零 ——
  配额失效是"对外不礼貌"里最难排查的一种。
- ``la_crawl_cursor`` 断点游标，按 ``source × cursor_key`` 一行。断点的意义是
  "任务被中断后从上次的位置继续"，所以它必须落盘；进程内变量做不到。

为什么不复用 ``la_knowledge``：采集内容**未经人工核验**，必须与已入库的法条/案例
在物理上分开。混进去的后果是未核验内容进入检索召回，而界面上看不出来 ——
这种静默污染比抓不到更危险。
"""

from __future__ import annotations

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from backend.app.db.base import Base, TimestampMixin
from backend.app.db.models.user import MYSQL_ARGS

# 判决书正文单篇可达数万字，MySQL 的 ``TEXT`` 只有 64KB，会在**写入时**静默截断
# （严格模式下报错、非严格模式下截尾）。``with_variant`` 只在 MySQL 上放大到
# LONGTEXT，SQLite 侧仍是 TEXT —— 同一套模型两个方言都不会丢正文。
_LONG_TEXT = Text().with_variant(LONGTEXT(), "mysql")


class CrawlDoc(Base, TimestampMixin):
    """一篇采集到的文书。"""

    __tablename__ = "la_crawl_doc"
    __table_args__ = (
        # ``content_hash`` 单独建索引：判重时按它查，是最热的一条查询路径。
        # 不加唯一约束，是因为"同一正文出现在两个来源"要允许并存（来源不同，
        # 引用价值不同），去重判断放在应用层（见 storage.save_doc），
        # 这里只保证查询快。
        Index("ix_crawl_doc_content_hash", "content_hash"),
        MYSQL_ARGS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 来源级唯一键：``{source}:{case_no}`` 或 ``{source}:{url 摘要}``。
    # 唯一约束是双去重的第一重 —— 没有它，同一次失败重试就会插第二行。
    doc_id: Mapped[str] = mapped_column(String(160), unique=True, index=True, comment="来源级唯一键")

    # 正文归一化后的 sha256，双去重的第二重。
    content_hash: Mapped[str] = mapped_column(String(64), default="", comment="正文 sha256")
    body_len: Mapped[int] = mapped_column(Integer, default=0, comment="正文字符数（判重辅助与展示）")

    source: Mapped[str] = mapped_column(String(32), index=True, default="", comment="来源站点标识")
    url: Mapped[str] = mapped_column(String(1024), default="", comment="原文地址")
    title: Mapped[str] = mapped_column(String(512), default="")
    doc_type: Mapped[str] = mapped_column(String(32), default="judgment", comment="judgment/ruling/notice...")

    # 解析层抽出的结构化字段（DOM 优先、正则兜底）。缺失留空而非编造。
    case_no: Mapped[str] = mapped_column(String(128), index=True, default="", comment="案号")
    court: Mapped[str] = mapped_column(String(128), index=True, default="", comment="法院/仲裁机构")
    judge_date: Mapped[str] = mapped_column(String(32), default="", comment="裁判/发布日期（YYYY-MM-DD）")

    body: Mapped[str] = mapped_column(_LONG_TEXT, default="", comment="正文纯文本")
    # 结构化附属信息：当事人、各字段的抽取来源（dom/regex）、采集批次等。
    # 用 JSON 而不是拆列：字段集合会随来源变化，逐列演进会不断改表结构。
    extra: Mapped[dict] = mapped_column(JSON, default=dict, comment="结构化附属字段")

    fetched_at: Mapped[str] = mapped_column(String(32), default="", comment="最近一次抓取时间（ISO 秒级）")
    # 同一 doc_id 被重复抓取时累加，用于判断"内容是否在悄悄变化"。
    seen_count: Mapped[int] = mapped_column(Integer, default=1, comment="同 doc_id 被抓到的次数")


class CrawlQuota(Base, TimestampMixin):
    """单日每源配额。一行 = 一个「来源 × 日期」的计数桶。"""

    __tablename__ = "la_crawl_quota"
    __table_args__ = (
        UniqueConstraint("source", "quota_date", name="uq_crawl_quota_source_date"),
        MYSQL_ARGS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    # ``YYYY-MM-DD``。存字符串而不是 DATE，是为了跨 SQLite/MySQL 行为一致
    # （SQLite 没有原生日期类型，用 DATE 列会按字符串比较，反而更绕）。
    quota_date: Mapped[str] = mapped_column(String(10), index=True)
    used: Mapped[int] = mapped_column(Integer, default=0, comment="当日已用条数")
    limit: Mapped[int] = mapped_column(Integer, default=0, comment="当日上限；0=不限")


class CrawlCursor(Base, TimestampMixin):
    """断点游标。一行 = 一个采集目标在某来源上的进度。"""

    __tablename__ = "la_crawl_cursor"
    __table_args__ = (
        UniqueConstraint("source", "cursor_key", name="uq_crawl_cursor_key"),
        MYSQL_ARGS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    # 采集目标标识（如"劳动争议-一审-2024"）。与 source 一起构成游标坐标。
    cursor_key: Mapped[str] = mapped_column(String(128), index=True)

    # pending / running / paused / done。
    # paused 是**验证码转人工**与**配额触顶**的落点：任务没失败，只是等人/等次日，
    # 断点必须保留，否则续跑会从头再来（而"从头"会立刻再撞一次同一道墙）。
    status: Mapped[str] = mapped_column(String(16), default="pending", comment="pending/running/paused/done")
    # 进度明细：{page, last_doc_id, finished_pages:[...], next_url}。
    payload: Mapped[dict] = mapped_column(JSON, default=dict, comment="断点明细")
    note: Mapped[str] = mapped_column(String(512), default="", comment="暂停原因（给人看）")

    finished_at: Mapped[str] = mapped_column(String(32), default="", comment="完成时间（ISO 秒级）")
