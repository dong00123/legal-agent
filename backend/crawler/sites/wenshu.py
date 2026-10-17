"""中国裁判文书网（wenshu.court.gov.cn）案例采集适配器。

**合规边界（先读这段）**

该站有严格的使用条款，且对自动化访问部署了登录与验证码校验。**必须在已获授权的
前提下使用**（单位内部使用、已向站点申请授权等）。本适配器沿用 flk 的边界：

- **不破解验证码**：命中即 ``CaptchaRequired`` 转人工挂起，不做任何绕过；
- **有随机间隔**（``CRAWL_DELAY_MIN/MAX``）与**单日配额**（``CRAWL_DAILY_QUOTA``）；
- **不伪装身份**：沿用站点自己的 UA，不伪造、不轮换身份。

**为什么走通用四层流水线，而不是自写接口**

``PlaywrightFetcher.fetch()`` 取到的是**渲染后的 DOM**，所以「列表页正则抽详情链接
+ 详情页选择器抽字段」这条路可行，不必逆向站点的私有接口 —— 那类接口带加密参数、
随版本频繁变动，写死即失效。

**首次使用：先有头跑一次完成登录**

    1) 让 ``CRAWL_WENSHU_ENABLED=true``；
    2) 跑 ``python -m backend.crawler.runner --once --cases-only --headed``；
    3) 在弹出的浏览器里人工登录 / 过验证码；登录态会落 ``CRAWL_STATE_DIR/wenshu.json``；
    4) 之后恢复正常（无头）即可自动续跑。

**必须核验的两处（站点改版后）**

下面的 ``DEFAULT_LINK_PATTERN`` 与 ``DEFAULT_SELECTORS`` 是按站点公开页面的常见结构
给出的，**不保证长期有效**。核验方式：有头跑一次，看日志里 ``crawler.pipeline`` 抽到的
链接数，以及入库行的 ``extra.field_sources``——``dom`` 表示选择器命中，``regex``/空
表示已失效（走了兜底）。失效时改 ``CRAWL_WENSHU_LINK_PATTERN`` /
``CRAWL_WENSHU_SELECTORS``，**不必改代码**。
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode, urlparse, urlunparse

from backend.app.config import settings
from backend.crawler.models import CrawlTarget

logger = logging.getLogger("legal_agent.crawler.wenshu")

SOURCE = "wenshu"

# 裁判文书网检索页。列表由 JS 渲染，需等结果渲染出来再抽链接。
DEFAULT_LIST_URL = "https://wenshu.court.gov.cn/website/wenshu/181217BMTKHNT2W0/index.html"

# 详情页链接形如 /website/wenshu/<hash>/index.html?docId=xxxx，需含命名组 ``u``。
DEFAULT_LINK_PATTERN = r"""href=["'](?P<u>/website/wenshu/[^"']*?docId=[^"']+)["']"""

# 详情页字段选择器。**目标站改版后需重新核验**（见模块 docstring）。
DEFAULT_SELECTORS: dict[str, str] = {
    "title": "h1",
    "case_no": ".caseNo",
    "court": ".courtName",
    "judge_date": ".judgeDate",
    "body": "#content",
}


def parse_selectors(raw: str) -> dict[str, str]:
    """``body=#content,title=h1`` → 字典。

    缺 ``=`` 的片段**跳过**而不是报错：选择器只是"更好的抽取方式"，
    缺了还有正文正则兜底，不值得让整次采集失败（与 CLI 同语义）。
    """
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, sel = part.partition("=")
        key, sel = key.strip(), sel.strip()
        if key and sel:
            out[key] = sel
    return out


def effective_selectors() -> dict[str, str]:
    """默认选择器 + ``CRAWL_WENSHU_SELECTORS`` 覆盖。"""
    return {**DEFAULT_SELECTORS, **parse_selectors(settings.CRAWL_WENSHU_SELECTORS)}


def effective_link_pattern() -> str:
    """详情链接正则：配置优先，留空用默认。"""
    return (settings.CRAWL_WENSHU_LINK_PATTERN or "").strip() or DEFAULT_LINK_PATTERN


def _with_keyword(url: str, keyword: str, param: str) -> str:
    """把关键词拼进列表页 URL。

    **站点若不认 URL 参数**（列表由 JS 触发检索），把 ``CRAWL_WENSHU_KEYWORD_PARAM``
    留空，并改用「固定列表页 URL + 关键词留空」的方式（见 docs/crawl-automation.md）。
    不这么做的话，会表现为"翻来翻去都是同一批最新文书"，而没有任何报错。
    """
    param = (param or "").strip()
    if not keyword or not param:
        return url
    p = urlparse(url)
    extra = urlencode({param: keyword})
    query = f"{p.query}&{extra}" if p.query else extra
    return urlunparse(p._replace(query=query))


def build_targets(
    *,
    keywords: list[str] | None = None,
    courts: list[str] | None = None,
    max_pages: int | None = None,
    list_url: str = "",
    page_param: str = "",
) -> list[CrawlTarget]:
    """按 ``CRAWL_WENSHU_*`` 组装案例采集目标：**每个关键词一个 target**。

    一次关键词一个 target（= 一个断点坐标）而不是合成一个：断点、配额、日志
    都按 target 记账，混在一起会让"哪个关键词采到哪了"无从判断，续跑也只能整体重来。
    """
    kws = [k.strip() for k in (
        keywords if keywords is not None else settings.crawl_wenshu_keyword_list
    ) if k.strip()]
    if not kws:
        kws = [""]      # 无关键词 = 只用列表页默认结果（谨慎：量大且未必是想要的）

    base = (list_url or settings.CRAWL_WENSHU_LIST_URL or "").strip() or DEFAULT_LIST_URL
    param = page_param or settings.CRAWL_WENSHU_PAGE_PARAM or "page"
    kw_param = settings.CRAWL_WENSHU_KEYWORD_PARAM
    pages = max(1, int(settings.CRAWL_WENSHU_MAX_PAGES if max_pages is None else max_pages))
    court_list = list(courts if courts is not None else settings.crawl_wenshu_court_list)
    pattern = effective_link_pattern()
    selectors = effective_selectors()

    targets: list[CrawlTarget] = []
    for kw in kws:
        key = f"{SOURCE}:{kw or 'all'}"
        targets.append(CrawlTarget(
            source=SOURCE,
            cursor_key=key,
            list_url=_with_keyword(base, kw, kw_param),
            page_param=param,
            start_page=1,
            max_pages=pages,
            doc_link_pattern=pattern,
            dom_selectors=dict(selectors),
            doc_id_field="case_no",
            extra={
                "keyword": kw,
                # 法院名只记进 extra 供人工核对，不做站点侧过滤：站点的法院过滤参数
                # 随版本变动，硬编码会**静默失效**（筛了跟没筛一样）。
                "courts": court_list,
                "page_size": int(settings.CRAWL_WENSHU_PAGE_SIZE),
            },
        ))
    return targets


__all__ = [
    "SOURCE",
    "DEFAULT_LIST_URL",
    "DEFAULT_LINK_PATTERN",
    "DEFAULT_SELECTORS",
    "build_targets",
    "effective_link_pattern",
    "effective_selectors",
    "parse_selectors",
]
