"""国家法律法规数据库（flk.npc.gov.cn）采集适配器。

**合规边界（重要，先读这段）**

该站的 ``robots.txt`` 是全站 ``Disallow: /``，且首行写明"禁止使用任何自动化工具、
脚本、爬虫程序采集或复制网站数据"；检索侧还部署了验证码。本适配器按使用方的
明确要求实现，并把边界钉死在代码里：

- **不破解验证码**：接口或页面出现验证码/频率提示时抛 ``CaptchaRequired``，
  由上层转人工挂起（``CRAWL_CAPTCHA_*``），不尝试任何绕过；
- **有随机间隔**：每次请求都过 ``fetcher.throttle()``（``CRAWL_DELAY_MIN/MAX``）；
- **有单日配额**：逐条消耗 ``CRAWL_DAILY_QUOTA``，触顶即挂起而不是硬跑；
- **不伪装身份**：沿用站点自己的 UA，不伪造、不轮换身份；
- 只采法规文本本身（《著作权法》第五条：法律、法规不属于著作权保护对象）。

**技术链路**（全部经实测确认）

1. ``POST /law-search/search/list`` → ``{total, rows[{bbbs, title, gbrq, sxrq,
   sxx, flxz, zdjgName, ...}]}``；
2. ``GET /law-search/download/pc?format=docx&bbbs=<bbbs>&fileId=`` →
   ``data.url`` 是站点自己的 OSS 直链
   （``https://flkoss.obs-bj2.cucloud.cn/prod/<日期>/<hash>.docx?...``）；
3. 取该 URL 得到 .docx；文件名用 ``{标题}_{公布日期}.docx``，与该站自身的命名
   一致，也正是本项目 ``parse_statute_meta`` 期望的格式。

**踩过的坑**（写下来免得后人重踩，每条都花了不少探测）

- 关键词字段是 ``searchContent``。用 ``keyword`` 不报错、直接返回**全量**
  （30896 条）—— 表现为"搜了但结果没筛"，很难发现；
- 下载参数是 ``format``：``docx`` = WPS 版本、``pdf`` = 公报原版。
  传 ``type`` / ``fileType`` 一律回 ``{"msg":"系统异常","code":500}``，
  这个报错完全看不出是参数名错；
- 这些接口**必须在浏览器会话内调用**（页面上下文 ``fetch``，或会话的
  ``context.request``）。裸 httpx 会被当成不同来源，同样回"系统异常"；
- 结果列表不是 HTML 表格，结果项里**没有 ``<a>``**，跳详情靠 JS 事件，
  所以"列表页正则抽链接"这条路走不通，必须用接口；
- ``/search?keyword=`` 这个 URL 参数同样不生效（返回全量）。
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.app.config import PROJECT_ROOT, settings
from backend.crawler import storage
from backend.crawler.fetcher import (
    CaptchaRequired,
    CrawlError,
    PlaywrightFetcher,
)
from backend.crawler.topics import (
    classifier,
    clean_doc_name,
    load_topic_rules,
    raw_root,
    topic_for_title,
)

logger = logging.getLogger("legal_agent.crawler.flk")

SOURCE = "flk"
BASE = "https://flk.npc.gov.cn"
WARMUP_URL = BASE + "/search"

# dry-run 时每个分类的翻页上限。dry-run 不落盘，"整页已存在"这个停止条件永不为真，
# 不封顶就会一路翻到全站末尾（三万余条）。
DRY_RUN_PAGE_CAP = 3

SEARCH_JS = """async (payload) => {
  const r = await fetch('/law-search/search/list', {
    method: 'POST',
    headers: {'Content-Type': 'application/json;charset=utf-8'},
    body: JSON.stringify(payload)});
  return {status: r.status, text: await r.text()};
}"""

LINK_JS = """async (payload) => {
  const r = await fetch('/law-search/download/pc?' + new URLSearchParams(payload));
  return {status: r.status, text: await r.text()};
}"""

ENUM_JS = """async () => {
  const r = await fetch('/law-search/search/enumData');
  return {status: r.status, text: await r.text()};
}"""

_TAG_RE = re.compile(r"<[^>]+>")          # 标题里的 <em class='highlight'> 高亮标签
_BAD_NAME_RE = re.compile(r'[\\/:*?"<>|\r\n\t]')


def clean_title(title: str) -> str:
    """去掉检索高亮标签。带标签的标题直接入库会污染法规名与去重键。"""
    return _TAG_RE.sub("", title or "").strip()


def safe_filename(name: str, limit: int = 120) -> str:
    cleaned = _BAD_NAME_RE.sub("_", (name or "").strip()).strip(" ._")
    return cleaned[:limit] or "未命名法规"


def resolve_output_dir(path: str | Path | None = None) -> Path:
    """落盘目录解析成绝对路径，相对路径按**项目根**锚定。

    不能按 cwd：从 ``scripts/`` 目录运行脚本很常见（IDE 的运行按钮默认就是），
    那会把文件写到 ``scripts/data/raw/`` 下，而 ``corpus.ingest`` 扫的是项目根的
    ``data/raw`` —— 结果是**下载全部成功、入库 0 条，且不报任何错**。
    """
    p = Path(path).expanduser() if path else settings.crawl_flk_dir
    return p if p.is_absolute() else PROJECT_ROOT / p


def read_names_file(path: str | Path) -> list[str]:
    """读法规名单文件：每行一个，``#`` 开头为注释，空行忽略，去掉 BOM。"""
    text = Path(path).expanduser().read_text(encoding="utf-8")
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def dedup_names(names: list[str]) -> list[str]:
    """去重且保序。保序是必要的：名单顺序就是用户的优先级。"""
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


def tasks_from_config() -> tuple[list[dict[str, Any]], list[str]]:
    """把 ``CRAWL_FLK_*`` 组装成任务列表。返回 ``(任务, 提示)``。

    关键词 × 级别是**笛卡尔积**：配 3 个关键词 + 3 个级别 = 9 个任务。
    这是有意的 —— "婚姻"在"法律"和"司法解释"里都要采，逐个组合比让脚本
    猜"该配哪个级别"更可控。

    抽成公开函数，是为了让「命令行脚本」与「自动执行器 runner」共用同一套组装
    逻辑：两处各写一份，迟早会在其中一处漏掉某个配置项，而表现恰好是
    "配了却没采"这种最难查的偏差。
    """
    notes: list[str] = []
    names = list(settings.crawl_flk_name_list)
    if settings.CRAWL_FLK_NAMES_FILE:
        try:
            names.extend(read_names_file(settings.CRAWL_FLK_NAMES_FILE))
        except Exception as exc:  # noqa: BLE001
            notes.append(f"CRAWL_FLK_NAMES_FILE 读取失败（已忽略）："
                         f"{type(exc).__name__}: {exc}")
    names = dedup_names(names)

    tasks: list[dict[str, Any]] = []
    if names:
        tasks.append({"kind": "names", "names": names})

    keywords = settings.crawl_flk_keyword_list
    categories = settings.crawl_flk_category_list
    if keywords:
        if categories:
            for kw in keywords:
                for cat in categories:
                    tasks.append({"kind": "search", "keyword": kw, "category": cat})
        else:
            for kw in keywords:
                tasks.append({"kind": "search", "keyword": kw, "category": ""})
    elif categories:
        for cat in categories:
            tasks.append({"kind": "search", "keyword": "", "category": cat})
    return tasks, notes


# --------------------------------------------------------------------------- 从 raw 推导
# 文件名归一只留一份实现（在 topics 里）：**目录记录**、采集检索词、落盘命名必须用同一套
# 规则，各写一份迟早漂移，症状恰好是"同一部法规被当成两个词"（明明采过却又采一遍，
# 或目录里明明有却说没分类）这种最难查的偏差。
_clean_raw_name = clean_doc_name


def _abs_path(p: str | Path) -> Path:
    q = Path(p).expanduser()
    return q if q.is_absolute() else PROJECT_ROOT / q


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def raw_terms(source_dir: str | Path | None = None) -> list[str]:
    """从 ``data/raw/`` 推导采集检索词 —— **"在 raw 里怎么分，就怎么采"**。

    目录名当业务主题、文件名当具体法规，两者都按**关键词（模糊）**检索：
    目录名是主题（"劳动法"）不是法规全称，文件名里也常见简称
    （``劳动法/民法典.pdf``），走精确标题会一律匹配不上。

    内置排除（必须排除，否则自我循环：采下来的文件会变成下一轮的检索词）：
    采集输出目录 ``CRAWL_FLK_DIR``、文书模板目录、名字含"案例/模板"的项、
    以及清洗后过短或纯数字的项。

    默认只取**目录名**（主题级，任务少）；``CRAWL_FLK_RAW_INCLUDE_FILES=true`` 时追加文件名。
    """
    root = _abs_path(source_dir or settings.RAW_DIR)
    if not root.is_dir():
        logger.warning("raw 目录不存在，跳过：%s", root)
        return []

    if settings.CRAWL_FLK_RAW_INCLUDE_FILES and settings.CRAWL_FLK_GROUP_BY_TOPIC:
        # 两个一起开就会自我循环：采下来的文件落进主题目录，下一轮又变成检索词。
        logger.warning(
            "CRAWL_FLK_RAW_INCLUDE_FILES 与 CRAWL_FLK_GROUP_BY_TOPIC 同时开启："
            "采集落盘会写进主题目录，而文件名又会被当成下一轮的检索词（自我循环）。"
            "建议二选一：只按目录名推导，或关掉按主题分组落盘。"
        )

    excludes = [_abs_path(settings.crawl_flk_dir), _abs_path(settings.DOC_TEMPLATE_DIR)]
    extra = [t.strip() for t in (settings.CRAWL_FLK_RAW_EXCLUDE or "").split(",") if t.strip()]
    limit = int(settings.CRAWL_FLK_RAW_MAX_TERMS or 0)

    out: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        t = term.strip()
        if len(t) < 2 or t.isdigit():
            return
        if any(k in t for k in ("案例", "模板")):
            return
        if any(k and k in t for k in extra):
            return
        if t in seen:
            return
        seen.add(t)
        out.append(t)

    for d in sorted(p for p in root.rglob("*") if p.is_dir()):
        if any(_is_under(d, ex) for ex in excludes):
            continue
        add(_clean_raw_name(d.name))
        if limit and len(out) >= limit:
            return out

    if settings.CRAWL_FLK_RAW_INCLUDE_FILES:
        for f in sorted(p for p in root.rglob("*")
                        if p.is_file() and p.suffix.lower() in (".pdf", ".docx", ".doc")):
            if any(_is_under(f, ex) for ex in excludes):
                continue
            add(_clean_raw_name(f.name))
            if limit and len(out) >= limit:
                break
    return out


def tasks_from_raw(source_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """把 :func:`raw_terms` 组装成检索任务（模糊、不限级别）。"""
    return [{"kind": "search", "keyword": t, "category": ""}
            for t in raw_terms(source_dir)]


# --------------------------------------------------------------------------- 业务领域归位
def plan_topic_moves(
    root: str | Path | None = None,
    *,
    rules: list[tuple[str, tuple[str, ...]]] | None = None,
) -> list[tuple[Path, Path]]:
    """列出"把已下载的法规按业务领域归位"的移动清单 ``(源, 目标)``。

    **只列出、不执行**：迁移是不可逆的文件操作，必须让人先看清清单再动手
    （见 scripts/reorganize_statutes.py 的 --dry-run 默认行为）。

    判定与采集落盘共用 :func:`topic_for_title`（模型优先、规则兜底），
    所以"自动分类的结果"和"手工归位的结果"不会对不上。

    注意：模型判定只在**缓存里查**（同步）。要让它生效，调用方需先用
    ``TopicClassifier.classify_batch`` 预热 —— `scripts/reorganize_statutes.py`
    已经这么做了。
    """
    base = _abs_path(root) if root else _abs_path(settings.crawl_flk_dir)
    rs = rules if rules is not None else load_topic_rules()
    out: list[tuple[Path, Path]] = []
    if not base.is_dir():
        return out
    topic_root = raw_root()
    for f in sorted(base.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in (".pdf", ".docx", ".doc"):
            continue
        topic = topic_for_title(_clean_raw_name(f.name), rs)
        if not topic:
            continue
        dst_dir = topic_root / topic
        if f.parent == dst_dir:
            continue                                  # 已经在目标目录里
        out.append((f, dst_dir / f.name))
    return out


# 文件类型魔数。**不能只认 PK / %PDF**：站点上有相当一批地方性法规的原件是
# 老式 .doc（OLE2 复合文档，魔数 d0cf11e0），只认前两种会把它们误判成
# "返回的不是文档"而整条丢弃 —— 实测被丢的正是"某州施行民法典婚姻家庭编的
# 补充规定"这类地方性法规。
_MAGIC = (
    (b"PK", "docx"),                    # docx / ofd / zip 容器
    (b"%PDF", "pdf"),
    (b"\xd0\xcf\x11\xe0", "doc"),       # OLE2：Word 97-2003
)


def sniff_kind(body: bytes) -> str:
    """按魔数判断文件类型，返回 ``docx`` / ``pdf`` / ``doc`` / ``""``（不认识）。"""
    for magic, kind in _MAGIC:
        if body.startswith(magic):
            return kind
    return ""


# 能被项目解析的格式。``doc`` 不在其中：python-docx 只认 .docx，
# 老式 .doc 抽不出文本（见 scripts/import_statute_files.py 的同款判断）。
_PARSABLE = ("docx", "pdf")


def build_search_body(
    *,
    keyword: str = "",
    codes: list[int] | None = None,
    page: int = 1,
    size: int = 20,
    search_type: int = 2,
    sxx: list[Any] | None = None,
    gbrq: list[Any] | None = None,
    sxrq: list[Any] | None = None,
) -> dict[str, Any]:
    """构造检索体。

    字段名与默认值取自站点自己首次加载 ``/search`` 时发出的请求 ——
    这是唯一权威的模板，照抄它就不会踩参数名的坑。
    """
    return {
        "searchRange": 1,
        "sxrq": list(sxrq or []),
        "gbrq": list(gbrq or []),
        "searchType": int(search_type),
        "sxx": list(sxx or []),
        "gbrqYear": [],
        "flfgCodeId": [int(c) for c in (codes or [])],
        "zdjgCodeId": [],
        "searchContent": keyword or "",
        "orderByParam": {"order": "-1", "sort": ""},
        "pageNum": int(page),
        "pageSize": int(size),
    }


@dataclass
class FlkRow:
    """检索结果的一行。"""

    bbbs: str
    title: str
    gbrq: str = ""       # 公布日期 YYYY-MM-DD
    sxrq: str = ""       # 施行日期
    sxx: Any = None      # 时效性编码
    flxz: str = ""       # 法律性质（法律 / 行政法规 / 地方法规 …）
    zdjg: str = ""       # 制定机关

    @staticmethod
    def from_api(raw: dict[str, Any]) -> "FlkRow":
        return FlkRow(
            bbbs=str(raw.get("bbbs") or ""),
            title=clean_title(raw.get("title") or ""),
            gbrq=str(raw.get("gbrq") or ""),
            sxrq=str(raw.get("sxrq") or ""),
            sxx=raw.get("sxx"),
            flxz=str(raw.get("flxz") or ""),
            zdjg=str(raw.get("zdjgName") or ""),
        )

    def filename(self, fmt: str = "docx") -> str:
        """``{标题}_{公布日期}`` —— 与站点自身命名一致，也是 ingest 期望的格式。"""
        date = (self.gbrq or "").replace("-", "")
        base = safe_filename(self.title)
        return f"{base}_{date}.{fmt}" if date else f"{base}.{fmt}"


@dataclass
class FlkOutcome:
    """一次采集的结果摘要。"""

    status: str = "done"            # done / paused / failed
    reason: str = ""
    total: int = 0                  # 站点报的命中总数
    pages: int = 0
    fetched: int = 0                # 实际尝试下载的条数
    saved: int = 0
    skipped: int = 0                # 本地已存在
    failed: int = 0
    # 因"非现行有效"被跳过的条数（sxx 不等于 CRAWL_FLK_SXX_EFFECTIVE）
    filtered: int = 0
    # 被跳过的明细（标题/状态/日期）。**必须报出来**：静默丢弃会让
    # "名单里那部《XX法》怎么没采到"变成一个查不出来的谜。
    ineffective: list[dict[str, Any]] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    categories: list[int] = field(default_factory=list)
    # ---- 按名单采集专用
    requested: list[str] = field(default_factory=list)
    # 名单里站点查不到的名字，连同最接近的候选一起报出来（不静默跳过）
    not_found: list[dict[str, Any]] = field(default_factory=list)
    # 同名多版本：名字 → 各版本公布日期
    versions: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status, "reason": self.reason, "total": self.total,
            "pages": self.pages, "fetched": self.fetched, "saved": self.saved,
            "skipped": self.skipped, "failed": self.failed,
            "filtered": self.filtered, "ineffective": list(self.ineffective),
            "files": list(self.files), "errors": list(self.errors),
            "categories": list(self.categories),
            "requested": list(self.requested), "not_found": list(self.not_found),
            "versions": dict(self.versions),
        }

    def merge(self, other: "FlkOutcome") -> None:
        """并入另一次采集的结果（配置驱动时一个进程会跑多个任务）。

        状态取**最严重**的：failed > paused > done。合并后一律报 done 会把
        "中途挂起了"这个信号盖掉，用户就不会去处理断点，第二天照旧跑不到东西。
        """
        self.total += other.total
        self.pages += other.pages
        self.fetched += other.fetched
        self.saved += other.saved
        self.skipped += other.skipped
        self.failed += other.failed
        self.filtered += other.filtered
        self.ineffective.extend(other.ineffective)
        self.files.extend(other.files)
        self.errors.extend(other.errors)
        self.requested.extend(other.requested)
        self.not_found.extend(other.not_found)
        self.versions.update(other.versions)
        rank = {"done": 0, "paused": 1, "failed": 2}
        if rank.get(other.status, 2) > rank.get(self.status, 0):
            self.status, self.reason = other.status, other.reason


class FlkCollector:
    """国家法律法规数据库采集器。

    ``fetcher`` 可注入（便于测试）；不注入时惰性新建 ``PlaywrightFetcher``。
    """

    def __init__(
        self,
        fetcher: PlaywrightFetcher | None = None,
        *,
        output_dir: str | Path | None = None,
        fmt: str = "docx",
        page_size: int | None = None,
        overwrite: bool = False,
        only_effective: bool | None = None,
    ) -> None:
        self.fetcher = fetcher or PlaywrightFetcher()
        self._owns_fetcher = fetcher is None
        # 默认（回落）落盘目录。按主题分组时，`_active_dir` 会临时指向该主题的目录。
        self.output_dir = resolve_output_dir(output_dir)
        self._active_dir: Path | None = None
        self.fmt = (fmt or "docx").lower()
        self.page_size = int(page_size or settings.CRAWL_FLK_PAGE_SIZE)
        self.overwrite = overwrite
        # None = 跟随配置；命令行 --include-ineffective 会传 False 覆盖
        self.only_effective = (
            settings.CRAWL_FLK_ONLY_EFFECTIVE if only_effective is None
            else bool(only_effective)
        )
        self._categories: dict[str, list[int]] = {}

    # ---------------------------------------------------------------- 时效性
    def is_ineffective(self, row: FlkRow) -> bool:
        """这条要不要因"非现行有效"跳过。

        **sxx 缺失时不跳过**：站点漏给状态时宁多勿少 —— 少采一条法规是"缺依据"
        且无声无息；多采一条还能在入库时由 ``check_statute_status`` 再判一次。
        """
        if not self.only_effective:
            return False
        raw = "" if row.sxx is None else str(row.sxx).strip()
        if not raw:
            return False
        return raw != str(settings.CRAWL_FLK_SXX_EFFECTIVE)

    # ---------------------------------------------------------------- 会话
    async def open_session(self, timeout_ms: int | None = None) -> Any:
        """建立会话并暖场。

        暖场是必需的：这些接口会校验来源，直接在空白上下文里 fetch 会被拒。
        先访问一次 ``/search`` 让站点下发 cookie、并把来源固定为同源。
        """
        await self.fetcher.start()
        page = await self.fetcher.new_page(SOURCE)
        t = int(timeout_ms or self.fetcher.timeout_ms)
        page.set_default_timeout(t)
        await page.goto(WARMUP_URL, wait_until="domcontentloaded", timeout=t)
        # 暖场只需"来源已固定为同源、cookie 已下发"：等加载完成即可。
        # **不再用 networkidle**（该站挂着长轮询/埋点长连接，"网络空闲"未必
        # 到达，只能吃满超时），也去掉固定 2s sleep（纯浪费）。超时不致命 ——
        # 后续接口调用自成一体，取不到就交给上层节流/重试。
        try:
            await page.wait_for_load_state("load", timeout=min(t, 20_000))
        except Exception:  # noqa: BLE001 - 超时不致命
            pass
        return page

    async def close(self) -> None:
        if self._owns_fetcher:
            await self.fetcher.close()

    # ---------------------------------------------------------------- 解析
    @staticmethod
    def _check_blocked(text: str) -> None:
        """接口返回里出现验证码/频率提示 → 转人工（不是重试）。"""
        keywords = settings.crawl_captcha_keyword_list
        if any(k in (text or "") for k in keywords):
            raise CaptchaRequired(f"接口返回疑似验证码/频率限制：{text[:120]}")

    @staticmethod
    def _loads(text: str, what: str) -> dict[str, Any]:
        try:
            data = json.loads(text)
        except Exception as exc:  # noqa: BLE001
            raise CrawlError(f"{what} 返回的不是 JSON（前 120 字）：{text[:120]}") from exc
        if not isinstance(data, dict):
            raise CrawlError(f"{what} 返回结构异常：{type(data).__name__}")
        return data

    # ---------------------------------------------------------------- 接口
    async def load_categories(self, page: Any) -> dict[str, list[int]]:
        """从 ``enumData`` 实时拉分类字典（名字 → 编码列表）。

        实时拉而不是写死：站点调整分类时，写死的表会让 ``--category 法律``
        静默变成"检索不到任何结果"，而实时拉取会直接反映出来。
        """
        if self._categories:
            return self._categories
        await self.fetcher.throttle()
        rec = await page.evaluate(ENUM_JS)
        data = self._loads((rec or {}).get("text") or "", "enumData")
        tree = (data.get("data") or {}).get("flfgfl") or {}
        out: dict[str, list[int]] = {}

        def walk(node: dict[str, Any]) -> None:
            name = (node.get("name") or "").strip()
            ids = [int(i) for i in (node.get("codeIdList") or []) if i]
            if name and ids:
                # 同名节点（"法律"出现两次，一级与二级）合并编码，取并集 ——
                # 只留其中一个会让"法律"漏掉另一半子类。
                merged = set(out.get(name, [])) | set(ids)
                out[name] = sorted(merged)
            for child in node.get("children") or []:
                walk(child)

        walk(tree)
        self._categories = out
        return out

    async def search(
        self, page: Any, *, keyword: str = "", codes: list[int] | None = None,
        page_num: int = 1, size: int | None = None,
        sxx: list[Any] | None = None, gbrq: list[Any] | None = None,
        sxrq: list[Any] | None = None,
    ) -> tuple[int, list[FlkRow]]:
        """检索一页。返回 ``(命中总数, 结果行)``。"""
        payload = build_search_body(
            keyword=keyword, codes=codes, page=page_num,
            size=int(size or self.page_size), sxx=sxx, gbrq=gbrq, sxrq=sxrq,
        )
        rec = await page.evaluate(SEARCH_JS, payload)
        text = (rec or {}).get("text") or ""
        self._check_blocked(text)
        data = self._loads(text, "search/list")
        rows = [FlkRow.from_api(x) for x in (data.get("rows") or []) if x.get("bbbs")]
        return int(data.get("total") or 0), rows

    async def download_link(self, page: Any, bbbs: str, *, fmt: str | None = None) -> str:
        """取某篇法规的文件直链（站点自己的 OSS 地址）。

        ``fmt`` 可覆盖实例默认值。存在的理由是：站点对**部分地方性法规只提供
        老式 .doc**（请求 docx 拿回来的也是 .doc），而项目解析不了 .doc，
        所以拿到 .doc 后要回头再要一次 ``pdf``（公报原版）—— pdf 能解析。
        """
        rec = await page.evaluate(
            LINK_JS, {"format": (fmt or self.fmt), "bbbs": bbbs, "fileId": ""}
        )
        text = (rec or {}).get("text") or ""
        self._check_blocked(text)
        data = self._loads(text, "download/pc")
        if data.get("code") != 200:
            raise CrawlError(f"下载链接接口报错：{data.get('msg') or text[:100]}")
        url = ((data.get("data") or {}).get("url") or "").strip()
        if not url.startswith("http"):
            # code=200 但链接为空，绝大多数情况是"该条没有这个格式的版本"
            # （实测一批地方性法规只有 .doc）。所以报错要说格式，而不是
            # "链接无效" —— 后者会让人去查接口，方向全错。
            raise CrawlError(f"该条不提供 {(fmt or self.fmt)} 版本（下载接口返回空链接）")
        return url

    # ---------------------------------------------------------------- 下载
    def _topic_dir(self, term: str) -> Path | None:
        """检索词对应的主题目录：``data/raw/`` 下有同名目录就用它，没有则 None。

        这就是"按我在 raw 里定义的名字存入"：``raw/婚姻法/`` 存在，则用"婚姻法"
        检索到的法规落进 ``raw/婚姻法/``。
        """
        t = (term or "").strip().strip("/")
        if not t or not settings.CRAWL_FLK_GROUP_BY_TOPIC:
            return None
        cand = raw_root() / t
        return cand if cand.is_dir() else None

    def _dir_for(self, row: FlkRow) -> Path:
        """这一篇该落到哪个目录。

        优先级：**显式/按检索词选定的目录** → **模型判定的业务领域** →
        关键词规则表（兜底） → ``CRAWL_FLK_DIR``。
        顺序不能颠倒：按检索词落盘是用户明确指定的，不该被领域判定改写。
        """
        if self._active_dir is not None:
            return self._active_dir
        topic = topic_for_title(row.title)
        if topic:
            # 记进"目录记录"的内存视图：这一篇马上落进 raw/<领域>/，下一轮查目录就能
            # 认出它已归位，不会再问模型、也不会换到别的领域去（否则会留下两份副本）。
            classifier.remember(row.title, topic)
            return raw_root() / topic
        return self.output_dir

    async def preclassify(self, rows: list[FlkRow]) -> None:
        """先把这一批的**业务领域**判定好（写进缓存），落盘时直接查缓存。

        为什么必须"先判定、后落盘"：落盘路径是同步的，没法在那里等一次网络调用；
        而且按批判定只需一次模型调用（``CRAWL_FLK_AUTO_TOPIC_BATCH`` 个标题一批），
        逐条判定会把调用次数乘以批大小。

        失败不致命：识别不到就落到默认目录，采集与入库照常。
        """
        if not settings.CRAWL_FLK_AUTO_TOPIC:
            return
        titles = [r.title for r in rows if r and r.title]
        if not titles:
            return
        try:
            await classifier.classify_batch(titles)
        except Exception as exc:  # noqa: BLE001 - 识别失败不该中断采集
            logger.warning("领域识别失败（本批按原目录落盘）：%s", exc)

    def target_path(self, row: FlkRow, kind: str | None = None) -> Path:
        """落盘路径。``kind`` 是**实际**类型（docx/pdf/doc），默认用请求的类型。"""
        return self._dir_for(row) / row.filename(kind or self.fmt)

    def existing_path(self, row: FlkRow) -> Path | None:
        """本地已有的这一篇（后缀可能是 docx/pdf/doc 之一），没有则 None。

        **不能只按请求的 fmt 拼一个名字去判**：同一条目在不同批次可能拿到不同
        格式（站点对部分地方性法规只给老式 .doc），只比一种后缀会把"明明下过了"
        判成"没下过"，于是重复请求 —— 配额和站点请求数都白花。
        """
        for kind in ("docx", "pdf", "doc"):
            p = self.target_path(row, kind)
            if p.exists():
                return p
        # 再查**默认目录**：改用主题分组后，早先落在 CRAWL_FLK_DIR 里的文件不该被
        # 重复下载（重复下载既费配额、又在库里撞哈希去重，纯属浪费）。
        if self._dir_for(row) != self.output_dir:
            for kind in ("docx", "pdf", "doc"):
                p = self.output_dir / row.filename(kind)
                if p.exists():
                    logger.debug("《%s》已在默认目录 %s，跳过重复下载", row.title, p)
                    return p
        return None

    async def save_one(self, page: Any, row: FlkRow) -> Path | None:
        """下载并落盘一篇。返回实际写出的路径；已存在则返回 None。

        流程里有两次"按**实际内容**纠正"的机会，都是为了避免让下游拿到假文件：

        1. **按魔数定扩展名**：请求 docx 不等于拿到 docx。实测一批地方性法规的
           原件是老式 .doc（魔数 ``d0cf11e0``），若仍按请求的 docx 命名，就会
           产出"内容是 .doc、后缀是 .docx"的假文件 —— 下游只会报"抽不出文本"。
        2. **.doc 回退 pdf**：老式 .doc 项目解析不了，所以回头再要一次公报 PDF
           版；拿到 pdf 就用它。对检索来说"能解析"比"格式统一"重要得多。
        """
        if self.existing_path(row) is not None and not self.overwrite:
            return None

        await self.fetcher.throttle()
        url = await self.download_link(page, row.bbbs)
        # 取文件走 throttle_file：这条请求打的是站点给的 OSS 直链（对象存储），
        # 不是检索接口，用接口级间隔会让每条采集有大半时间耗在等静态文件上。
        await self.fetcher.throttle_file()
        status, body, _headers = await self.fetcher.fetch_bytes(url, source=SOURCE)
        if status != 200 or not body:
            raise CrawlError(f"下载失败 http={status} bytes={len(body)}")

        kind = sniff_kind(body)
        if kind == "doc" and self.fmt == "docx":
            # 回退取 pdf。**整段必须包在 try 里** —— 这不是主流程，是"如果能
            # 更好就用 pdf"的优化；站点对相当一批地方性法规**只存了 .doc、
            # 没有 pdf**（详情接口里 ossPdfPath 就是 null），此时 download_link
            # 会抛"不提供 pdf 版本"。
            #
            # 曾经没包 try，结果就是：已经下载到手的 .doc 因为"回退也失败了"
            # 被整条丢弃，报错还是"下载链接无效（空）"这种指向接口的说法 ——
            # 用户既拿不到文件，也看不出问题出在格式上。
            try:
                logger.info("flk 该条只提供老式 .doc，尝试改用 pdf 版：%s", row.title)
                await self.fetcher.throttle()
                url2 = await self.download_link(page, row.bbbs, fmt="pdf")
                await self.fetcher.throttle_file()
                status2, body2, _h2 = await self.fetcher.fetch_bytes(url2, source=SOURCE)
                if status2 == 200 and sniff_kind(body2) == "pdf":
                    kind, body = "pdf", body2
                else:
                    logger.info("flk pdf 回退未拿到 pdf，保留已到手的 .doc：%s", row.title)
            except Exception as exc:  # noqa: BLE001 - 回退失败不影响已到手的结果
                logger.info("flk pdf 回退失败，保留已到手的 .doc：%s —— %s",
                            row.title, exc)

        if kind not in _PARSABLE and kind != "doc":
            # 既不认识、也不在可解析清单里 → 多半是 HTML 错误页，落盘没有意义
            raise CrawlError(f"返回的不是文档（前 4 字节 {body[:4].hex()}）")

        target = self.target_path(row, kind)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        if kind == "doc":
            logger.warning("flk 保存为老式 .doc（项目解析不了，入库前需先转 .docx）：%s",
                           target.name)
        return target

    # ---------------------------------------------------------------- 单条处理
    async def _download_row(
        self,
        page: Any,
        row: FlkRow,
        out: FlkOutcome,
        *,
        consume_quota: bool = True,
        dry_run: bool = False,
    ) -> str:
        """处理一条：判重 → 扣配额 → 下载落盘。返回 ``listed/skipped/saved/failed/paused``。

        抽出来是为了让"按分类/关键词"与"按名单"两条入口共用同一套配额与幂等
        语义 —— 两份写法迟早会在其中一条上漏掉配对的分支（少归还一次配额、
        或跳过时误扣额度），而这类偏差在报告里只表现为数字微妙地对不上。
        """
        # 时效性过滤放在最前：dry-run 也要按它预演，否则预览出来的清单
        # 和真正会下的那批对不上。
        if self.is_ineffective(row):
            out.filtered += 1
            out.ineffective.append({
                "title": row.title, "sxx": row.sxx,
                "gbrq": row.gbrq, "cat": row.flxz,
            })
            return "filtered"

        if dry_run:
            out.fetched += 1
            out.files.append(row.filename(self.fmt))
            return "listed"

        # 先判本地有没有：已存在的**不扣配额**。配额约束的是"对外发了多少下载
        # 请求"；把重复条目也算进去，长跑（每天重跑同一批）会把额度全花在
        # "什么都没下"上。
        if self.existing_path(row) is not None and not self.overwrite:
            out.skipped += 1
            return "skipped"

        if consume_quota and not storage.try_consume_quota(SOURCE):
            st = storage.quota_status(SOURCE)
            out.status = "paused"
            out.reason = (f"来源 {SOURCE} 当日配额已用尽"
                          f"（{st['used']}/{st['limit']}），等次日续跑")
            return "paused"

        out.fetched += 1
        try:
            dst = await self.save_one(page, row)
        except CaptchaRequired as exc:
            if consume_quota:
                storage.release_quota(SOURCE)
            out.status, out.reason = "paused", f"命中验证码，已转人工：{exc}"
            return "paused"
        except Exception as exc:  # noqa: BLE001 - 单条失败不中断整批
            if consume_quota:
                storage.release_quota(SOURCE)
            out.failed += 1
            msg = f"{row.title}：{type(exc).__name__}: {exc}"
            out.errors.append(msg)
            logger.warning("flk 单条下载失败：%s", msg)
            return "failed"

        if dst is None:
            out.skipped += 1
            return "skipped"
        out.saved += 1
        out.files.append(dst.name)
        return "saved"

    # ---------------------------------------------------------------- 主流程
    async def run(
        self,
        *,
        keyword: str = "",
        category: str = "",
        max_pages: int = 1,
        limit: int = 0,
        consume_quota: bool = True,
        dry_run: bool = False,
        page: Any | None = None,
        output_dir: str | Path | None = None,
    ) -> FlkOutcome:
        out = FlkOutcome()
        # 落盘目录：显式给了 output_dir 就用它；否则按检索词找 raw/ 下的同名主题目录；
        # 都没有才回落到 CRAWL_FLK_DIR。退出时恢复，免得影响同进程里的后续任务。
        prev_dir = self._active_dir
        self._active_dir = (
            resolve_output_dir(output_dir) if output_dir else self._topic_dir(keyword)
        ) or prev_dir
        if self._active_dir is not None and self._active_dir != self.output_dir:
            logger.info("检索词「%s」的落盘目录：%s", keyword or "-", self._active_dir)
        # 传入 page = 复用已有会话。配置驱动时一个进程要连跑多个任务，
        # 每次都重开浏览器 + 重新暖场要多花十几秒，纯属浪费。
        own_session = page is None
        if page is None:
            page = await self.open_session()
        try:
            codes: list[int] = []
            if category:
                cats = await self.load_categories(page)
                codes = cats.get(category, [])
                if not codes:
                    raise CrawlError(
                        f"未知分类「{category}」；可用：{'、'.join(sorted(cats))}"
                    )
                out.categories = codes

            for pnum in range(1, max(1, int(max_pages)) + 1):
                await self.fetcher.throttle()
                total, rows = await self.search(
                    page, keyword=keyword, codes=codes, page_num=pnum
                )
                await self.preclassify(rows)          # 先判定领域，落盘时才查得到
                out.total = total
                out.pages += 1
                if not rows:
                    break
                logger.info("flk 第 %d 页：命中 %d 条，共 %d", pnum, len(rows), total)

                for row in rows:
                    if limit and out.fetched >= limit:
                        break
                    if await self._download_row(
                        page, row, out, consume_quota=consume_quota, dry_run=dry_run
                    ) == "paused":
                        return out

                if limit and out.fetched >= limit:
                    break
                if total and pnum * self.page_size >= total:
                    break
        except CaptchaRequired as exc:
            out.status, out.reason = "paused", f"命中验证码，已转人工：{exc}"
        except Exception as exc:  # noqa: BLE001
            out.status, out.reason = "failed", f"{type(exc).__name__}: {exc}"
            logger.exception("flk 采集异常")
        finally:
            self._active_dir = prev_dir          # 恢复落盘目录，别影响后续任务
            if own_session:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass
                await self.close()
        return out

    # ---------------------------------------------------------------- 自动目录
    async def run_catalog(
        self,
        *,
        categories: list[str],
        max_pages: int = 0,
        stop_after_pages_all_existing: int = 1,
        limit: int = 0,
        consume_quota: bool = True,
        dry_run: bool = False,
        page: Any | None = None,
    ) -> FlkOutcome:
        """按**目录**增量采集 —— **不需要手填名单**。

        对每个分类（默认只含国家级，见 ``CRAWL_FLK_AUTO_CATEGORIES``）从新到旧
        逐页扫描，逐条处置：

        - 非现行有效 → 过滤（与按名单一致，不给库塞废止条文）；
        - 本地已存在 → 跳过（文件幂等，``existing_path`` 已覆盖 docx/pdf/doc）；
        - 其余 → 下载。

        **停止条件**：连续 ``stop_after_pages_all_existing`` 页的条目**全部**不是
        新条目（都已存在），即认为更旧的都采过了，停止该分类。

        为什么用"整页已存在"而不是"按发布日期过滤"：站点的日期过滤参数格式随版本
        变动，写死会**静默失效**（表现为每轮重采全站）；而"结果按公布日期倒序 +
        文件幂等"这条路径不依赖任何私有参数。代价是首次要多翻几页。
        """
        out = FlkOutcome()
        own_session = page is None
        if page is None:
            page = await self.open_session()
        try:
            cats = await self.load_categories(page)
            picked: list[tuple[str, list[int]]] = []
            for name in (categories or []):
                codes = cats.get(name)
                if not codes:
                    out.errors.append(
                        f"未知分类「{name}」（用 `python -m backend.crawler --status` "
                        f"或 fetch_flk_statutes.py --list-categories 看可用名）"
                    )
                    continue
                picked.append((name, codes))
            if not picked:
                raise CrawlError(
                    "自动目录模式：没有任何有效分类（检查 CRAWL_FLK_AUTO_CATEGORIES）"
                )
            out.categories = [c for _n, codes in picked for c in codes]
            stop_pages = max(1, int(stop_after_pages_all_existing))
            eff_max = int(max_pages)
            if dry_run and not eff_max:
                # dry-run 不落盘，任何条目都不会变成"已存在"，"整页已存在"这个停止
                # 条件因此**永不为真** —— 不设上限就会一路翻到全站末尾。给个上限。
                eff_max = DRY_RUN_PAGE_CAP
                logger.info("dry-run：每分类最多翻 %d 页（避免扫全站）", eff_max)

            for name, codes in picked:
                page_num = 1
                stale_pages = 0
                while True:
                    if eff_max and page_num > eff_max:
                        break
                    await self.fetcher.throttle()
                    total, rows = await self.search(page, codes=codes, page_num=page_num)
                    await self.preclassify(rows)      # 先判定领域，落盘时才查得到
                    if page_num == 1:
                        out.total += total          # 每分类只在首页累加站点申报总数
                    out.pages += 1
                    if not rows:
                        logger.info("分类「%s」第 %d 页无结果，结束", name, page_num)
                        break

                    page_all_existing = True
                    for row in rows:
                        if limit and out.fetched >= limit:
                            logger.info("自动目录已达本轮上限 %d 条，停止", limit)
                            return out
                        result = await self._download_row(
                            page, row, out, consume_quota=consume_quota, dry_run=dry_run
                        )
                        if result == "paused":
                            return out
                        # 只有"真出了新东西"或"失败"才说明还没追平；skipped/filtered
                        # 都表示这条不需要处理，不能据此判定"还要继续翻"。
                        if result in ("saved", "listed", "failed"):
                            page_all_existing = False

                    if page_all_existing:
                        stale_pages += 1
                        logger.info("分类「%s」第 %d 页全部已存在（连续 %d/%d 页）",
                                    name, page_num, stale_pages, stop_pages)
                        if stale_pages >= stop_pages:
                            logger.info("分类「%s」已追平，停止（共翻 %d 页）", name, page_num)
                            break
                    else:
                        stale_pages = 0
                    if total and page_num * self.page_size >= total:
                        break
                    page_num += 1
        except CaptchaRequired as exc:
            out.status, out.reason = "paused", f"命中验证码，已转人工：{exc}"
        except Exception as exc:  # noqa: BLE001
            out.status, out.reason = "failed", f"{type(exc).__name__}: {exc}"
            logger.exception("flk 目录采集异常")
        finally:
            if own_session:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass
                await self.close()
        return out

    # ---------------------------------------------------------------- 按名单采集
    @staticmethod
    def _norm_name(name: str) -> str:
        """标题归一化：去掉书名号、各类括号与空白。

        站点的标题**不带书名号**（``中华人民共和国劳动合同法``），而人手写名单时
        习惯带（``《中华人民共和国劳动合同法》``）。不归一化就会"明明存在却匹配
        不上"，而报出来的原因只有一句"未找到" —— 排查方向完全错。
        """
        return re.sub(r"[\s《》〈〉（）()\[\]【】〔〕{}]", "", name or "").strip()

    async def _scan(
        self, page: Any, keyword: str, *, search_type: int, scan: int
    ) -> list[FlkRow]:
        """按指定检索类型扫描前 ``scan`` 条结果（必要时跨页）。"""
        rows: list[FlkRow] = []
        size = max(1, min(50, self.page_size))
        pnum = 1
        while len(rows) < scan:
            if pnum > 1:
                # 第 2 页起要自己守间隔：调用方只在整段扫描前节流一次，跨页连打
                # 就变成"一次多请求"，与"每次请求都有随机间隔"的承诺不符。
                # 默认 page_size=20 时 scan 一页就够、走不到这里；把 page_size
                # 调到 scan 以下才会触发。
                await self.fetcher.throttle()
            payload = build_search_body(
                keyword=keyword, page=pnum, size=size, search_type=search_type
            )
            rec = await page.evaluate(SEARCH_JS, payload)
            text = (rec or {}).get("text") or ""
            self._check_blocked(text)
            data = self._loads(text, "search/list")
            got = [FlkRow.from_api(x) for x in (data.get("rows") or []) if x.get("bbbs")]
            if not got:
                break
            rows.extend(got)
            total = int(data.get("total") or 0)
            if pnum * size >= total:
                break
            pnum += 1
        return rows[:scan]

    async def find_by_name(
        self, page: Any, name: str, *, scan: int = 20
    ) -> tuple[list[FlkRow], list[FlkRow]]:
        """按一个名字找法规。返回 ``(命中的版本列表, 供参考的候选)``。

        两段式，缺一不可：

        1. **精确检索**（``searchType=1``）—— 实测"中华人民共和国劳动合同法"
           精确 2 条 / 模糊 8013 条，精确模式只返回名字**包含**该词的结果；
        2. 在结果里做**标题完全匹配**（归一化后相等）—— 因为精确检索是"包含"
           而不是"等于"（搜"劳动合同法"会同时带出"劳动合同法实施条例"）。

        精确检索里没有完全匹配时，回退模糊检索再找一遍，并把最接近的几条作为
        候选带出来：名单里写错一个字时，"未找到 + 候选"比只报"未找到"有用得多。
        """
        target = self._norm_name(name)
        await self.fetcher.throttle()
        rows = await self._scan(page, name, search_type=1, scan=scan)
        hit = [r for r in rows if self._norm_name(r.title) == target]
        if not hit:
            await self.fetcher.throttle()
            rows = await self._scan(page, name, search_type=2, scan=scan)
            hit = [r for r in rows if self._norm_name(r.title) == target]
        hit_ids = {r.bbbs for r in hit}
        others = [r for r in rows if r.bbbs not in hit_ids]
        # 候选按与目标名的**相似度**排序。直接用检索顺序不行：模糊检索的默认
        # 排序是"公布日期倒序"，端出来的前几条跟目标名毫无关系（实测查一个乱写
        # 的名字，候选全是 1978~1988 年的批复决议），等于没有提示作用。
        # 按相似度排之后，"名字写错一个字"时正确的那条会排到第一位。
        others.sort(key=lambda r: -difflib.SequenceMatcher(
            None, target, self._norm_name(r.title)).ratio())
        return hit, others[:5]

    async def run_by_names(
        self,
        names: list[str],
        *,
        all_versions: bool = False,
        limit: int = 0,
        consume_quota: bool = True,
        dry_run: bool = False,
        page: Any | None = None,
    ) -> FlkOutcome:
        """按**法规名单**精确采集（"我要这几部法"）。

        与 ``run``（按关键词/分类扫）的区别：

        - 每个名字单独检索，取标题**完全匹配**的那一条；
        - 同名多版本（站点会返回历史修正版本，实测《劳动法》有 2009 与 2018
          两版）默认只取**公布日期最新**的那份 —— 默认全要会让旧版一起入库，
          检索结果里新旧混排；要全收加 ``all_versions=True``；
        - 没找到的名字连同候选一起报出，**不会被静默跳过**。
        """
        out = FlkOutcome()
        out.requested = list(names)
        own_session = page is None
        if page is None:
            page = await self.open_session()
        try:
            for name in names:
                if limit and out.fetched >= limit:
                    break
                try:
                    hit, candidates = await self.find_by_name(page, name)
                    await self.preclassify(hit)       # 先判定领域，落盘时才查得到
                except CaptchaRequired as exc:
                    out.status, out.reason = "paused", f"命中验证码，已转人工：{exc}"
                    return out
                except Exception as exc:  # noqa: BLE001 - 单个名字失败不中断整批
                    out.failed += 1
                    out.errors.append(f"{name}：检索失败 {type(exc).__name__}: {exc}")
                    continue

                if not hit:
                    out.not_found.append({
                        "name": name,
                        "candidates": [
                            f"{r.title}（{r.gbrq or '无日期'}，{r.flxz}）" for r in candidates
                        ],
                    })
                    continue

                hit.sort(key=lambda r: r.gbrq or "", reverse=True)
                if len(hit) > 1:
                    out.versions[name] = [r.gbrq or "无日期" for r in hit]
                picked = hit if all_versions else hit[:1]
                for row in picked:
                    if limit and out.fetched >= limit:
                        break
                    if await self._download_row(
                        page, row, out, consume_quota=consume_quota, dry_run=dry_run
                    ) == "paused":
                        return out
                out.total += len(picked)
        except Exception as exc:  # noqa: BLE001
            out.status, out.reason = "failed", f"{type(exc).__name__}: {exc}"
            logger.exception("flk 按名单采集异常")
        finally:
            if own_session:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass
                await self.close()
        return out
