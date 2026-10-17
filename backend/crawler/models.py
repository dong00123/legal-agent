"""采集四层之间流转的数据结构。

刻意做成**纯 dataclass、零外部依赖**：四层里只有访问层碰 playwright、只有存储层碰
数据库，中间这两组结构是它们之间唯一的契约。让契约为纯数据，单测才能在不装
playwright、不连库的前提下把解析层与调度层整条验完。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- 保存结果
SAVE_CREATED = "created"            # 新入库
SAVE_UPDATED = "updated"            # doc_id 命中且正文有修订 → 覆盖并累加 seen_count
SAVE_DUPLICATE = "duplicate"        # doc_id 命中且正文一致 → 完全跳过
SAVE_HASH_DUPLICATE = "hash_duplicate"  # doc_id 是新的，但正文与库中另一篇相同 → 不重复入库

# 采集任务终态
JOB_DONE = "done"
JOB_PAUSED = "paused"               # 验证码转人工 / 配额触顶：没失败，等条件满足再续
JOB_FAILED = "failed"
JOB_SKIPPED = "skipped"             # 条件不满足，本轮没跑（仍在队列里）


@dataclass
class CrawlTarget:
    """一个采集目标（一个站点的某类文书列表）。

    ``cursor_key`` 是断点的坐标。**必须显式给**：用 ``list_url`` 兜底只适合单目标，
    但当同一个列表页因筛选条件不同而有多个游标时，用 URL 会撞车 ——
    表现是"两个任务互相覆盖进度"，看起来像各自从中间开始跑。
    """

    source: str
    cursor_key: str = ""
    list_url: str = ""
    page_param: str = "page"
    start_page: int = 1
    max_pages: int = 0                                  # 0 = 不限
    doc_link_selector: str = ""                         # DOM 取详情链接（CSS 选择器）
    doc_link_pattern: str = r"""href=["'](?P<u>/[^"']+)["']"""  # 正则兜底取链接
    # 详情页结构化字段的 CSS 选择器映射，如 {"body": "div#content", "title": "h1"}。
    # 取不到就走解析层的正文正则兜底，而不是报失败。
    dom_selectors: dict[str, str] = field(default_factory=dict)
    # 用解析出的哪个字段做主键主体（默认案号，取不到时回落到 URL 摘要）。
    doc_id_field: str = "case_no"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def coord(self) -> tuple[str, str]:
        return self.source, (self.cursor_key or self.list_url or self.source)

    @property
    def cursor_name(self) -> str:
        return self.coord[1]


@dataclass
class FetchResult:
    """访问层一次取页的结果。"""

    ok: bool
    url: str = ""
    status: int = 0
    html: str = ""
    text: str = ""
    dom: dict[str, str] = field(default_factory=dict)   # 选择器抽出的字段（DOM 提取）
    captcha: bool = False                               # 命中验证码/人机校验
    error: str = ""
    elapsed_ms: int = 0
    attempts: int = 1
    proxy: str = ""
    title: str = ""


@dataclass
class ParsedDoc:
    """解析层输出。字段缺失一律留空，**绝不编造**（法律场景的硬约束）。"""

    doc_id: str
    source: str
    url: str = ""
    title: str = ""
    doc_type: str = "judgment"
    case_no: str = ""
    court: str = ""
    judge_date: str = ""
    body: str = ""
    content_hash: str = ""
    # 每个字段的抽取来源：dom / regex / 空。写进 extra 后，一条数据是怎么来的可回溯。
    fields: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SaveOutcome:
    """存储层写入结果。"""

    status: str
    doc_id: str
    content_hash: str = ""
    detail: str = ""

    @property
    def inserted(self) -> bool:
        return self.status == SAVE_CREATED

    @property
    def duplicate(self) -> bool:
        return self.status in (SAVE_DUPLICATE, SAVE_HASH_DUPLICATE)
