"""Playwright 浏览器自动化采集 —— 四层结构。

::

    调度层  scheduler.py   条件队列 / 断点游标 / 单日配额
    访问层  fetcher.py     登录态持久化 / 验证码转人工 / 随机延迟 3~8s /
                           指数退避重试 / 代理预留
    解析层  parser.py      DOM 提取 + 正文正则兜底（案号 / 法院 / 日期）
    存储层  storage.py     MySQL 双去重（doc_id 唯一键 + 正文哈希）

对外只有三个入口：``CrawlPipeline``（编程式）、``collect_urls`` / ``run_targets``
（便捷函数）、``python -m backend.crawler``（命令行）。

**自动化的入口是** ``python -m backend.crawler.runner --once``（法条 + 案例一轮，
交给 Windows 任务计划程序 / cron 周期调用），见 ``docs/crawl-automation.md``。

**整体默认关闭**（``CRAWL_ENABLED=false``）：它依赖 playwright + 浏览器内核，
且面向外部站点，属于"要用才开"的能力。关闭时本包不会被编排链路 import，
服务启动不受影响。
"""

from __future__ import annotations

import asyncio
from typing import Any

from backend.app.config import settings
from backend.crawler import storage
from backend.crawler.fetcher import (
    BaseFetcher,
    CaptchaRequired,
    CrawlError,
    PlaywrightFetcher,
    PlaywrightMissing,
    ProxyLease,
    ProxyPool,
    ProxyPoolBusy,
    SessionStore,
    StaticFetcher,
    playwright_available,
)
from backend.crawler.models import (
    JOB_DONE,
    JOB_FAILED,
    JOB_PAUSED,
    JOB_SKIPPED,
    SAVE_CREATED,
    SAVE_DUPLICATE,
    SAVE_HASH_DUPLICATE,
    SAVE_UPDATED,
    CrawlTarget,
    FetchResult,
    ParsedDoc,
    SaveOutcome,
)
from backend.crawler.parser import (
    CASE_NO_STRICT_RE,
    COURT_RE,
    extract_case_no,
    extract_court,
    extract_date,
    html_to_text,
    parse_document,
)
from backend.crawler.pipeline import CrawlOutcome, CrawlPipeline
from backend.crawler.scheduler import (
    AllOf,
    CallableCondition,
    ConditionalQueue,
    ConditionResult,
    CrawlJob,
    CursorNotDone,
    QuotaCondition,
    Scheduler,
    TimeWindow,
)

__all__ = [
    # 调度层
    "Scheduler", "CrawlJob", "ConditionalQueue", "ConditionResult", "AllOf",
    "QuotaCondition", "CursorNotDone", "TimeWindow", "CallableCondition",
    # 访问层
    "BaseFetcher", "PlaywrightFetcher", "StaticFetcher", "SessionStore",
    "ProxyPool", "ProxyLease", "CrawlError", "CaptchaRequired", "PlaywrightMissing",
    "ProxyPoolBusy", "playwright_available",
    # 解析层
    "parse_document", "html_to_text", "extract_case_no", "extract_court",
    "extract_date", "CASE_NO_STRICT_RE", "COURT_RE",
    # 存储层
    "storage",
    # 串联
    "CrawlPipeline", "CrawlOutcome",
    # 数据
    "CrawlTarget", "FetchResult", "ParsedDoc", "SaveOutcome",
    "JOB_DONE", "JOB_PAUSED", "JOB_FAILED", "JOB_SKIPPED",
    "SAVE_CREATED", "SAVE_UPDATED", "SAVE_DUPLICATE", "SAVE_HASH_DUPLICATE",
    # 便捷函数
    "collect_urls", "run_targets", "collect_url",
]


async def collect_url(
    url: str, *, source: str, dom_selectors: dict[str, str] | None = None
) -> dict[str, Any]:
    """采集单个详情页并入库（异步）。"""
    pipeline = CrawlPipeline()
    try:
        outcome = await pipeline.crawl_document(
            url, source=source, dom_selectors=dom_selectors
        )
        return {"url": url, "status": outcome.status, "doc_id": outcome.doc_id,
                "detail": outcome.detail}
    except CaptchaRequired as exc:
        # 转人工要与"失败"分开报：失败可以重试，转人工重试多少次都一样，
        # 需要的是有头模式 + 人来过验证码。混在一起会让上层不断重试。
        return {"url": url, "status": JOB_PAUSED, "error": f"命中验证码：{exc}",
                "human": True, "screenshot": exc.screenshot}
    except Exception as exc:  # noqa: BLE001 - 采集失败要如实回传，不静默
        return {"url": url, "status": JOB_FAILED, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        await pipeline.close()


async def collect_urls(
    urls: list[str], *, source: str, dom_selectors: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """批量采集详情页。逐条独立，一条失败不影响其它。"""
    out: list[dict[str, Any]] = []
    for u in urls:
        out.append(await collect_url(u, source=source, dom_selectors=dom_selectors))
    return out


def run_targets(
    targets: list[CrawlTarget],
    *,
    fetcher: BaseFetcher | None = None,
    max_jobs: int = 50,
    priority: int = 0,
) -> list[dict[str, Any]]:
    """同步跑一批采集目标（命令行 / 脚本 / 定时任务入口）。"""
    async def _main() -> list[CrawlOutcome]:
        pipeline = CrawlPipeline(fetcher=fetcher)
        try:
            for t in targets:
                pipeline.scheduler.submit(t, priority=priority)
            return await pipeline.run_until_idle(max_jobs=max_jobs)
        finally:
            await pipeline.close()

    return [o.to_dict() for o in asyncio.run(_main())]


def status(source: str = "") -> dict[str, Any]:
    """采集运行态快照：队列、断点、配额、playwright 可用性。"""
    return {
        "enabled": settings.CRAWL_ENABLED,
        "playwright": playwright_available(),
        "headless": settings.CRAWL_HEADLESS,
        "delay_range": list(settings.crawl_delay_range),
        "daily_quota": settings.CRAWL_DAILY_QUOTA,
        "docs_total": storage.count_docs(source),
        "quota": storage.quota_status(source) if source else None,
        "cursors": storage.list_cursors(source),
    }
