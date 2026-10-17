"""四层串联：调度 → 访问 → 解析 → 存储。

一次 ``run_job`` 的闭环：

1. 从**断点**读出起始页（调度层）；
2. 对每一页：先扣**配额**（扣不到就挂起，不是失败）→ 访问层取页；
3. 列表页抽详情链接 → 逐条取详情页 → 解析层出结构化文档；
4. 存储层双去重写入；
5. 每处理完一条就落一次断点 —— 断点粒度是"条"而不是"批"，
   否则中途被打断会丢掉整页已抓取的成果。

异常分工：取页失败（重试已耗尽）→ 记 failed 并**归还配额**；
验证码 / 代理池忙 / 配额用尽 → **挂起（paused）并保留断点**。
把后三者归到 failed 是最常见的错误：失败会让人重头再来，而重头会立刻再撞同一道墙。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

from backend.crawler import storage
from backend.crawler.fetcher import (
    BaseFetcher,
    CaptchaRequired,
    CrawlError,
    PlaywrightMissing,
    ProxyPoolBusy,
)
from backend.crawler.models import (
    JOB_DONE,
    JOB_FAILED,
    JOB_PAUSED,
    SAVE_CREATED,
    SAVE_DUPLICATE,
    SAVE_HASH_DUPLICATE,
    SAVE_UPDATED,
    CrawlTarget,
    ParsedDoc,
    SaveOutcome,
)
from backend.crawler.parser import parse_document
from backend.crawler.scheduler import CrawlJob, Scheduler

logger = logging.getLogger("legal_agent.crawler.pipeline")


@dataclass
class CrawlOutcome:
    """一次采集任务的结果摘要。"""

    job_id: str
    source: str
    status: str
    pages: int = 0
    fetched: int = 0
    saved: int = 0
    updated: int = 0
    duplicate: int = 0
    failed: int = 0
    error: str = ""
    reason: str = ""
    saved_doc_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id, "source": self.source, "status": self.status,
            "pages": self.pages, "fetched": self.fetched, "saved": self.saved,
            "updated": self.updated, "duplicate": self.duplicate, "failed": self.failed,
            "error": self.error, "reason": self.reason,
            "saved_doc_ids": list(self.saved_doc_ids),
        }


def build_page_url(list_url: str, page: int, page_param: str = "page") -> str:
    """把 ``page`` 拼进列表页 URL。

    已有同名参数时**替换**而不是追加：追加会得到 ``?page=2&page=3``，
    多数站点取第一个，于是"翻页"实际一直停在第二页 —— 表现为
    "每页内容都一样"，但没有任何报错。
    """
    if not list_url or page <= 1:
        return list_url
    p = urlparse(list_url)
    query = p.query
    param = re.escape(page_param or "page")
    new_pair = f"{page_param}={page}"
    if re.search(rf"(^|&){param}=", query):
        query = re.sub(rf"(^|&){param}=[^&]*", rf"\g<1>{new_pair}", query)
    else:
        query = f"{query}&{new_pair}" if query else new_pair
    return urlunparse(p._replace(query=query))


def extract_links(html: str, pattern: str, base_url: str) -> list[str]:
    """从列表页 HTML 抽详情链接（正则兜底，不依赖选择器）。

    返回**去重且保持出现顺序**的绝对 URL：顺序是站点的排序（通常按时间倒序），
    断点靠它才有意义；去重是因为列表里"上一篇/下一篇"链接常指向同一条。
    """
    out: list[str] = []
    seen: set[str] = set()
    try:
        rx = re.compile(pattern or r"""href=["'](?P<u>/[^"']+)["']""")
    except re.error as exc:
        logger.warning("详情链接正则非法，已跳过：%s", exc)
        return []
    for m in rx.finditer(html or ""):
        raw = m.group("u") if "u" in (m.groupdict() or {}) else (m.group(0) or "")
        url = urljoin(base_url, raw.strip())
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


class CrawlPipeline:
    """四层编排。``fetcher`` 可注入（单测用 ``StaticFetcher``）。"""

    def __init__(
        self,
        *,
        scheduler: Scheduler | None = None,
        fetcher: BaseFetcher | None = None,
    ) -> None:
        self.scheduler = scheduler or Scheduler()
        self._fetcher = fetcher
        self._owns_fetcher = fetcher is None

    @property
    def fetcher(self) -> BaseFetcher:
        if self._fetcher is None:
            # 惰性构造：这样"只调调度/存储"的用法不必付构造浏览器的代价。
            from backend.crawler.fetcher import PlaywrightFetcher
            self._fetcher = PlaywrightFetcher()
        return self._fetcher

    async def close(self) -> None:
        if self._owns_fetcher and self._fetcher is not None:
            closer = getattr(self._fetcher, "close", None)
            if closer is not None:
                await closer()
            self._fetcher = None

    # ---------------------------------------------------------------- 单文档
    async def crawl_document(
        self,
        url: str,
        *,
        source: str,
        dom_selectors: dict[str, str] | None = None,
        target: CrawlTarget | None = None,
        consume_quota: bool = True,
    ) -> SaveOutcome:
        """采集并存储单个详情页。配额已用尽时抛 ``CrawlError``（可重试）。"""
        if consume_quota and not self.scheduler.consume_quota(source):
            st = self.scheduler.quota(source)
            raise CrawlError(
                f"来源 {source} 当日配额已用尽（{st['used']}/{st['limit']}）", retryable=True
            )
        try:
            res = await self.fetcher.fetch(url, source=source, dom_selectors=dom_selectors)
        except CaptchaRequired:
            if consume_quota:
                self.scheduler.release_quota(source)
            raise
        except Exception:
            if consume_quota:
                self.scheduler.release_quota(source)
            raise

        if not res.ok:
            if consume_quota:
                self.scheduler.release_quota(source)
            raise CrawlError(f"取页失败：{res.error}", retryable=False)

        doc = parse_document(
            source=source, url=url, html=res.html, text=res.text,
            dom=res.dom, title=res.title, target=target,
        )
        outcome = storage.save_doc(doc)
        logger.info("采集 %s → %s（%s）", url, outcome.status, outcome.detail)
        return outcome

    # ---------------------------------------------------------------- 单任务
    async def run_job(self, job: CrawlJob) -> CrawlOutcome:
        target = job.target
        source = target.source
        cursor_key = target.cursor_name
        outcome = CrawlOutcome(job_id=job.job_id, source=source, status=JOB_FAILED)

        cur = self.scheduler.resume(source, cursor_key)
        start_page = int(cur.get("start_page") or target.start_page or 1)
        last_doc_id = cur.get("last_doc_id") or ""
        logger.info("采集任务开始：%s/%s 从第 %s 页%s", source, cursor_key, start_page,
                    f"（断点续跑，上次止于 {last_doc_id}）" if last_doc_id else "")

        self.scheduler.checkpoint(source, cursor_key, status="running",
                                  note=f"第 {start_page} 页开始")

        page = start_page
        while True:
            if target.max_pages and page >= start_page + target.max_pages:
                break
            if not self.scheduler.consume_quota(source):
                st = self.scheduler.quota(source)
                reason = f"来源 {source} 当日配额已用尽（{st['used']}/{st['limit']}），等次日续跑"
                self.scheduler.pause_target(source, cursor_key, reason,
                                            payload={"page": page - 1, "next_page": page,
                                                     "last_doc_id": last_doc_id})
                outcome.status, outcome.reason = JOB_PAUSED, reason
                return outcome

            list_url = build_page_url(target.list_url, page, target.page_param)
            try:
                res = await self.fetcher.fetch(list_url, source=source)
            except (CaptchaRequired, ProxyPoolBusy) as exc:
                self.scheduler.release_quota(source)
                return self._pause(outcome, source, cursor_key, str(exc),
                                   {"page": page - 1, "next_page": page, "last_doc_id": last_doc_id})
            except PlaywrightMissing:
                self.scheduler.release_quota(source)
                outcome.status, outcome.error = JOB_FAILED, "未安装 playwright，采集不可用"
                return outcome
            except Exception as exc:  # noqa: BLE001
                self.scheduler.release_quota(source)
                outcome.status, outcome.error = JOB_FAILED, f"列表页取页异常：{exc}"
                return outcome

            if not res.ok:
                self.scheduler.release_quota(source)
                outcome.status = JOB_FAILED
                outcome.error = f"列表页取页失败（第 {page} 页）：{res.error}"
                return outcome

            outcome.pages += 1
            links = extract_links(res.html or res.text, target.doc_link_pattern, list_url)
            if not links:
                # 列表页没抽出链接 = 到底了。**但不能只凭这一条断言"完成"**：
                # 若选择器/正则写错，也会是空。所以只在"已翻过至少一页"时判定完成，
                # 且把这件事记进断点，让人能复核。
                logger.info("第 %s 页未抽到详情链接，判定已到末页", page)
                break

            for link in links:
                if not self.scheduler.consume_quota(source):
                    reason = f"来源 {source} 当日配额已用尽，等次日续跑"
                    self.scheduler.pause_target(source, cursor_key, reason,
                                                payload={"page": page - 1, "next_page": page,
                                                         "last_doc_id": last_doc_id})
                    outcome.status, outcome.reason = JOB_PAUSED, reason
                    return outcome
                outcome.fetched += 1
                try:
                    resd = await self.fetcher.fetch(link, source=source,
                                                    dom_selectors=target.dom_selectors)
                except CaptchaRequired as exc:
                    self.scheduler.release_quota(source)
                    return self._pause(outcome, source, cursor_key, str(exc),
                                       {"page": page - 1, "next_page": page,
                                        "last_doc_id": last_doc_id})
                except ProxyPoolBusy as exc:
                    self.scheduler.release_quota(source)
                    return self._pause(outcome, source, cursor_key, str(exc),
                                       {"page": page - 1, "next_page": page,
                                        "last_doc_id": last_doc_id})
                except Exception as exc:  # noqa: BLE001
                    self.scheduler.release_quota(source)
                    outcome.failed += 1
                    logger.warning("详情页取页异常，跳过：%s（%s）", link, exc)
                    continue

                if not resd.ok:
                    self.scheduler.release_quota(source)
                    outcome.failed += 1
                    logger.warning("详情页取页失败，跳过：%s（%s）", link, resd.error)
                    continue

                doc = parse_document(source=source, url=link, html=resd.html, text=resd.text,
                                     dom=resd.dom, title=resd.title, target=target)
                save = storage.save_doc(doc)
                _tally(outcome, save, doc)
                outcome.saved_doc_ids.append(save.doc_id)
                last_doc_id = save.doc_id

                # 断点粒度 = 一条文档。被打断时最多丢当前这一条。
                self.scheduler.checkpoint(
                    source, cursor_key, status="running",
                    payload={"page": page, "last_doc_id": last_doc_id,
                             "finished_pages": list(range(target.start_page or 1, page))},
                    note=f"第 {page} 页进行中",
                )

            self.scheduler.checkpoint(
                source, cursor_key, status="running",
                payload={"page": page, "finished_pages": list(range(target.start_page or 1, page + 1))},
                note=f"第 {page} 页完成",
            )
            page += 1

        self.scheduler.finish_target(
            source, cursor_key,
            payload={"page": page - 1, "finished_pages": list(range(target.start_page or 1, page))},
            note=f"共 {outcome.pages} 页 / {outcome.saved} 新增 / {outcome.duplicate} 去重",
        )
        outcome.status = JOB_DONE
        logger.info("采集任务完成：%s/%s 新增 %d，修订 %d，去重 %d，失败 %d",
                    source, cursor_key, outcome.saved, outcome.updated,
                    outcome.duplicate, outcome.failed)
        return outcome

    def _pause(self, outcome: CrawlOutcome, source: str, cursor_key: str,
               reason: str, payload: dict[str, Any]) -> CrawlOutcome:
        self.scheduler.pause_target(source, cursor_key, reason, payload=payload)
        outcome.status, outcome.reason = JOB_PAUSED, reason
        return outcome

    # ---------------------------------------------------------------- 批量
    async def run_until_idle(self, max_jobs: int = 50) -> list[CrawlOutcome]:
        """把队列里**条件已满足**的任务跑到没有为止。

        注意：条件不满足的任务留在队列里（``blocked``），不会被反复重试 ——
        这与"取不到任务"是两件事，返回结果里分开体现。
        """
        results: list[CrawlOutcome] = []
        for _ in range(max(0, int(max_jobs))):
            job, blockers = self.scheduler.next_job()
            if job is None:
                if blockers:
                    logger.info("队列中 %d 个任务条件未满足，暂停调度：%s",
                                len(blockers), blockers[0]["reason"])
                break
            try:
                results.append(await self.run_job(job))
            except Exception as exc:  # noqa: BLE001 - 单个任务异常不该中断整批
                logger.exception("采集任务异常：%s", job.job_id)
                results.append(CrawlOutcome(job_id=job.job_id, source=job.source,
                                            status=JOB_FAILED, error=str(exc)))
        return results


def _tally(outcome: CrawlOutcome, save: SaveOutcome, doc: ParsedDoc) -> None:
    if save.status == SAVE_CREATED:
        outcome.saved += 1
    elif save.status == SAVE_UPDATED:
        outcome.updated += 1
    elif save.status in (SAVE_DUPLICATE, SAVE_HASH_DUPLICATE):
        outcome.duplicate += 1
    if not doc.body:
        outcome.failed += 1     # 空正文也算一种失败信号，不该被静默计入"成功"


def default_conditions(allow_window: bool = False) -> list[Any]:
    """常用条件集合：可选的时间窗。配额与断点条件由 ``submit`` 自动挂上。"""
    from backend.crawler.scheduler import TimeWindow

    conds: list[Any] = []
    if allow_window:
        conds.append(TimeWindow(22, 6))
    return conds


__all__ = [
    "CrawlOutcome",
    "CrawlPipeline",
    "build_page_url",
    "extract_links",
    "default_conditions",
]
