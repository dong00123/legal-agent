"""调度层：条件队列 + 断点 + 单日配额。

三个概念各解决一类问题，**不是同义词**：

- **条件队列**：任务不是"先到先跑"，而是"条件满足才跑"。条件不满足的任务
  **留在队列里**（挂起），后面满足条件的任务可以先跑 —— 用普通 FIFO 时，
  队首一个"等次日配额"的任务会把后面所有任务一起堵死。
- **断点**：一个采集目标跑到第 N 页被中断（验证码 / 配额 / 崩溃），
  续跑要能接着第 N+1 页，而不是从头再来（从头会立刻再撞同一道墙）。
- **单日配额**：对来源的礼貌约束。计数落库（见 storage），
  进程重启、多副本都不会绕过它。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Protocol

from backend.app.config import settings
from backend.crawler import storage
from backend.crawler.models import JOB_DONE, JOB_PAUSED, CrawlTarget

logger = logging.getLogger("legal_agent.crawler.scheduler")


# --------------------------------------------------------------------------- 条件
@dataclass
class ConditionResult:
    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:  # 让条件可以直接 when 判断
        return self.ok


class Condition(Protocol):
    name: str

    def check(self) -> ConditionResult: ...


class AlwaysOk:
    name = "always"

    def check(self) -> ConditionResult:
        return ConditionResult(True)


@dataclass
class CallableCondition:
    """任意谓词。``fn`` 返回 bool 或 ``ConditionResult``。"""

    name: str
    fn: Callable[[], Any]

    def check(self) -> ConditionResult:
        try:
            r = self.fn()
        except Exception as exc:  # noqa: BLE001 - 条件自身异常视作不满足（保守）
            return ConditionResult(False, f"条件 {self.name} 求值异常：{exc}")
        if isinstance(r, ConditionResult):
            return r
        return ConditionResult(bool(r), "" if r else f"条件 {self.name} 未满足")


@dataclass
class AllOf:
    """复合条件：全部满足才放行。任一不满足时把**它的原因**带出来，
    否则"为什么没跑"会退化成一句无用的"条件不满足"。"""

    conditions: list[Any] = field(default_factory=list)
    name: str = "all_of"

    def check(self) -> ConditionResult:
        for c in self.conditions:
            r = c.check()
            if not r.ok:
                return ConditionResult(False, r.reason)
        return ConditionResult(True)


@dataclass
class QuotaCondition:
    """单日配额是否还有余额。余额为 0 时挂起（不是失败）。"""

    source: str
    limit: int | None = None
    name: str = "quota"

    def check(self) -> ConditionResult:
        st = storage.quota_status(self.source, limit=self.limit)
        if st["unlimited"]:
            return ConditionResult(True)
        if st["remaining"] and st["remaining"] > 0:
            return ConditionResult(True)
        return ConditionResult(
            False, f"来源 {self.source} 当日配额已用尽（{st['used']}/{st['limit']}），等次日"
        )


@dataclass
class CursorNotDone:
    """断点已完成的任务不再调度。

    这是"条件"而不是"取任务时特判"：把"做完了"表达成一个条件，
    队列、日志、重跑入口读到的口径就是同一个，不会出现
    "调度说它没跑、断点说它早做完了"这种互相打架的状态。
    """

    source: str
    cursor_key: str
    name: str = "cursor_not_done"

    def check(self) -> ConditionResult:
        cur = storage.load_cursor(self.source, self.cursor_key)
        if cur.get("status") == JOB_DONE:
            return ConditionResult(False, "断点标记为已完成，无需重跑")
        return ConditionResult(True)


@dataclass
class TimeWindow:
    """只在某时间窗内跑（本地时区）。支持跨零点，如 22:00~06:00。

    跨零点必须显式支持：写成 ``start <= h < end`` 时，22→6 这种窗口恒为空，
    表现为"配了夜间跑却永远不跑"，而且没有任何报错。
    """

    start_hour: int
    end_hour: int
    name: str = "time_window"

    @staticmethod
    def is_inside(hour: int, start_hour: int, end_hour: int) -> bool:
        """判据单独拎出来，是为了**能确定性测试** —— 依赖 ``datetime.now()`` 的
        逻辑只能靠"跑的时候刚好是几点"碰运气，跨零点的分支基本永远测不到。
        """
        s, e = int(start_hour) % 24, int(end_hour) % 24
        h = int(hour) % 24
        if s == e:
            return True
        return (s <= h < e) if s < e else (h >= s or h < e)

    def check(self) -> ConditionResult:
        h = datetime.now().hour
        if self.is_inside(h, self.start_hour, self.end_hour):
            return ConditionResult(True)
        s, e = int(self.start_hour) % 24, int(self.end_hour) % 24
        return ConditionResult(False, f"当前 {h} 时不在采集时间窗 {s}:00-{e}:00 内")


# --------------------------------------------------------------------------- 任务与队列
@dataclass
class CrawlJob:
    """一个采集任务。"""

    target: CrawlTarget
    job_id: str = ""
    priority: int = 0                     # 越大越先跑
    conditions: list[Any] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    # 运行时统计
    pages_done: int = 0
    docs_saved: int = 0

    @property
    def source(self) -> str:
        return self.target.source

    @property
    def cursor_key(self) -> str:
        return self.target.cursor_name

    def check(self) -> ConditionResult:
        return AllOf(list(self.conditions)).check()


class ConditionalQueue:
    """条件队列。

    ``pop_ready`` 按优先级扫描，取第一个**条件全部满足**的任务；
    不满足的留在队列里，并把原因一并返回 —— 否则"队列里有货却一个都没跑"
    在日志上完全看不出来。
    """

    def __init__(self) -> None:
        self._jobs: list[CrawlJob] = []

    def __len__(self) -> int:
        return len(self._jobs)

    def jobs(self) -> list[CrawlJob]:
        return list(self._jobs)

    def put(self, job: CrawlJob) -> CrawlJob:
        self._jobs.append(job)
        return job

    def remove(self, job_id: str) -> bool:
        before = len(self._jobs)
        self._jobs = [j for j in self._jobs if j.job_id != job_id]
        return len(self._jobs) != before

    def pop_ready(self) -> tuple[CrawlJob | None, list[dict[str, Any]]]:
        blockers: list[dict[str, Any]] = []
        order = sorted(range(len(self._jobs)), key=lambda i: -self._jobs[i].priority)
        for i in order:
            job = self._jobs[i]
            r = job.check()
            if r.ok:
                self._jobs.remove(job)
                return job, blockers
            blockers.append({"job_id": job.job_id, "source": job.source,
                             "cursor_key": job.cursor_key, "reason": r.reason})
        return None, blockers

    def blocked_report(self) -> list[dict[str, Any]]:
        return [
            {"job_id": j.job_id, "source": j.source, "cursor_key": j.cursor_key,
             "reason": j.check().reason}
            for j in self._jobs
            if not j.check().ok
        ]


# --------------------------------------------------------------------------- 调度器
class Scheduler:
    """调度器：队列 + 断点 + 配额的统一入口。"""

    def __init__(self, queue: ConditionalQueue | None = None) -> None:
        self.queue = queue or ConditionalQueue()

    # ---------------------------------------------------------------- 入队
    def submit(
        self,
        target: CrawlTarget,
        *,
        priority: int = 0,
        conditions: list[Any] | None = None,
        params: dict[str, Any] | None = None,
        with_defaults: bool = True,
    ) -> CrawlJob:
        """入队。默认挂上「配额」与「断点未完成」两个条件。"""
        conds: list[Any] = []
        if with_defaults:
            conds.append(QuotaCondition(target.source))
            conds.append(CursorNotDone(target.source, target.cursor_name))
        if conditions:
            conds.extend(conditions)
        job = CrawlJob(
            target=target,
            job_id=f"{target.source}:{target.cursor_name}",
            priority=priority,
            conditions=conds,
            params=dict(params or {}),
        )
        self.queue.put(job)
        return job

    def next_job(self) -> tuple[CrawlJob | None, list[dict[str, Any]]]:
        return self.queue.pop_ready()

    # ---------------------------------------------------------------- 配额
    def quota(self, source: str, limit: int | None = None) -> dict[str, Any]:
        return storage.quota_status(source, limit=limit)

    def consume_quota(self, source: str, *, limit: int | None = None, n: int = 1) -> bool:
        return storage.try_consume_quota(source, limit=limit, n=n)

    def release_quota(self, source: str, *, n: int = 1) -> None:
        storage.release_quota(source, n=n)

    # ---------------------------------------------------------------- 断点
    def checkpoint(
        self,
        source: str,
        cursor_key: str,
        *,
        status: str | None = None,
        payload: dict[str, Any] | None = None,
        note: str | None = None,
        finished: bool = False,
    ) -> dict[str, Any]:
        return storage.save_cursor(source, cursor_key, status=status, payload=payload,
                                   note=note, finished=finished)

    def resume(self, source: str, cursor_key: str) -> dict[str, Any]:
        """读断点，并给出"从哪继续"的现成结论，调用方不需要自己解析 payload。"""
        cur = storage.load_cursor(source, cursor_key)
        payload = dict(cur.get("payload") or {})
        page = payload.get("page")
        next_page = payload.get("next_page")
        if isinstance(page, int):
            next_page = page + 1
        return {
            **cur,
            "start_page": next_page or (payload.get("start_page") or None),
            "last_doc_id": payload.get("last_doc_id", ""),
            "finished_pages": list(payload.get("finished_pages") or []),
            "is_fresh": not payload,
        }

    @staticmethod
    def pause_target(
        source: str, cursor_key: str, reason: str, *, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """把任务挂起：保留断点、记下原因。**不是失败** —— 失败会丢断点。"""
        logger.info("采集任务挂起：%s/%s —— %s", source, cursor_key, reason)
        return storage.save_cursor(source, cursor_key, status=JOB_PAUSED,
                                   payload=payload, note=reason)

    @staticmethod
    def finish_target(
        source: str, cursor_key: str, *, payload: dict[str, Any] | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        return storage.save_cursor(source, cursor_key, status=JOB_DONE, payload=payload,
                                   note=note, finished=True)

    # ---------------------------------------------------------------- 观测
    def status(self, source: str = "") -> dict[str, Any]:
        return {
            "queued": len(self.queue),
            "blocked": self.queue.blocked_report(),
            "cursors": storage.list_cursors(source),
            "quota": storage.quota_status(source) if source else None,
            "delay_range": list(settings.crawl_delay_range),
        }
