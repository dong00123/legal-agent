"""采集自动执行器：把「法条 + 案例」封装成**一轮幂等采集**。

给「Windows 任务计划程序 / cron」的入口是 ``--once``：跑一轮就退出，不需要常驻进程。
需要常驻时用 ``--serve``（按 ``CRAWL_INTERVAL_MINUTES`` 循环）。

一轮做什么：

1. 启动**一个**浏览器，法条与案例共用 —— 省掉重复启动与暖场（暖场要加载上 MB 的 SPA）；
2. **法条**：按 ``CRAWL_FLK_*`` 组装任务 → :class:`FlkCollector` 逐条采；
   可选自动建索引（调 ``backend.corpus.ingest --statutes``）；
3. **案例**：按 ``CRAWL_WENSHU_*`` 组装 :class:`CrawlTarget` → :class:`CrawlPipeline`
   采集并写入 ``la_crawl_doc``（双去重、断点、配额复用同一套）；
4. 打印结构化汇总（``--json`` 给机器读），并以**退出码**表达结果。

退出码的语义（任务计划程序据此决定要不要报警）：

- ``0`` 正常 / 未启用（未启用不是失败，不该报警）；
- ``3`` 有任务**挂起**（验证码转人工、配额触顶）—— 没失败，但要有人处理；
- ``1`` 真失败。

**为什么退出码要区分挂起**：把挂起标成失败会让人反复重跑（每次都撞同一道墙）；
标成成功又会让人以为采完了。这两件事对"要不要人工介入"的含义完全不同，必须分开。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
import time
from datetime import datetime
from typing import Any

from backend.app.config import PROJECT_ROOT, settings
from backend.crawler.fetcher import PlaywrightFetcher, playwright_available
from backend.crawler.models import CrawlTarget

logger = logging.getLogger("legal_agent.crawler.runner")


# --------------------------------------------------------------------------- 法条
def _task_label(task: dict[str, Any]) -> str:
    if task.get("kind") == "names":
        return f"按名单（{len(task.get('names') or [])} 条）"
    kw = task.get("keyword") or "（不限关键词）"
    cat = task.get("category") or "（不限级别）"
    return f"关键词「{kw}」× 级别「{cat}」"


async def run_statutes(
    fetcher: Any, *, dry_run: bool = False, limit: int = 0, max_pages: int = 1,
) -> dict[str, Any]:
    """跑完所有法条任务，复用同一个页面会话。返回 :meth:`FlkOutcome.to_dict`。"""
    from backend.crawler.sites.flk import (
        FlkCollector,
        FlkOutcome,
        tasks_from_config,
        tasks_from_raw,
    )

    tasks, notes = tasks_from_config()
    for n in notes:
        logger.warning("法条清单：%s", n)
    if settings.CRAWL_FLK_FROM_RAW:
        raw_tasks = tasks_from_raw()
        if raw_tasks:
            words = "、".join(str(t.get("keyword") or "") for t in raw_tasks)
            logger.info("法条：从 data/raw/ 推导出 %d 个检索词：%s%s",
                        len(raw_tasks), words[:120], "…" if len(words) > 120 else "")
        else:
            logger.warning("CRAWL_FLK_FROM_RAW=true，但 data/raw/ 没推导出任何检索词")
        tasks = tasks + raw_tasks
    auto = bool(settings.CRAWL_FLK_AUTO)
    if not tasks and not auto:
        return {"skipped": True,
                "reason": ("既未配置 CRAWL_FLK_* 清单，也未开启自动目录/raw 推导模式"
                           "（CRAWL_FLK_AUTO / CRAWL_FLK_FROM_RAW）")}

    # 注入 fetcher：collector 不接管浏览器生命周期（由 runner 统一开关）。
    collector = FlkCollector(fetcher=fetcher)
    merged = FlkOutcome()
    page = await collector.open_session()
    try:
        if auto:
            cats = settings.crawl_flk_auto_category_list
            logger.info("法条自动目录模式：分类 %s", "、".join(cats) or "（空）")
            cat_out = await collector.run_catalog(
                categories=cats,
                max_pages=settings.CRAWL_FLK_AUTO_MAX_PAGES,
                stop_after_pages_all_existing=settings.CRAWL_FLK_AUTO_STOP_PAGES,
                limit=limit, dry_run=dry_run, page=page,
            )
            merged.merge(cat_out)
            logger.info("  自动目录 → 待采 %d，新增 %d，跳过 %d，失败 %d，翻页 %d",
                        cat_out.fetched, cat_out.saved, cat_out.skipped,
                        cat_out.failed, cat_out.pages)
            if merged.status in ("paused", "failed"):
                return merged.to_dict()

        for idx, task in enumerate(tasks, 1):
            # ``--limit`` 是**全局**额度：按已用量扣减，否则配 3 个任务会各下满一次。
            remain = 0
            if limit:
                remain = max(0, int(limit) - merged.fetched)
                if remain <= 0:
                    logger.info("法条已达本轮上限 %d 条，剩余任务跳过", limit)
                    break
            logger.info("法条任务 %d/%d：%s", idx, len(tasks), _task_label(task))
            if task["kind"] == "names":
                out = await collector.run_by_names(
                    task["names"], all_versions=settings.CRAWL_FLK_ALL_VERSIONS,
                    limit=remain, dry_run=dry_run, page=page,
                )
            else:
                out = await collector.run(
                    keyword=task.get("keyword", ""), category=task.get("category", ""),
                    max_pages=max_pages, limit=remain, dry_run=dry_run, page=page,
                )
            merged.merge(out)
            logger.info("  法条 → 待采 %d，新增 %d，跳过 %d，失败 %d%s",
                        out.fetched, out.saved, out.skipped, out.failed,
                        f"，未找到 {len(out.not_found)}" if out.not_found else "")
            if out.status in ("paused", "failed"):
                logger.warning("法条任务中止：%s —— %s", out.status, out.reason)
                break
    finally:
        try:
            await page.close()
        except Exception:  # noqa: BLE001
            pass
    return merged.to_dict()


def ingest_statutes() -> dict[str, Any]:
    """法条采完后建索引（子进程调 ``backend.corpus.ingest --statutes``）。

    用子进程而不是直接 import：ingest 会加载向量/分词依赖，且它有自己的一套
    "扫描 → 解析 → 入库" 状态；放在同一进程里跑会与浏览器的 asyncio 循环耦合。
    """
    cmd = [sys.executable, "-m", "backend.corpus.ingest", "--statutes"]
    logger.info("法条建索引：%s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    except Exception as exc:  # noqa: BLE001
        logger.warning("建索引子进程启动失败：%s", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": proc.returncode == 0, "returncode": proc.returncode}


# --------------------------------------------------------------------------- 案例
def case_targets() -> list[CrawlTarget]:
    """按 ``CRAWL_WENSHU_*`` 组装案例采集目标；未启用返回空。"""
    if not settings.CRAWL_WENSHU_ENABLED:
        return []
    from backend.crawler.sites.wenshu import build_targets

    return build_targets()


async def run_cases(
    fetcher: Any, *, dry_run: bool = False, max_jobs: int = 0,
) -> dict[str, Any]:
    """跑完所有案例目标，写入 ``la_crawl_doc``。"""
    from backend.crawler.pipeline import CrawlPipeline

    targets = case_targets()
    if not targets:
        return {"skipped": True,
                "reason": "未启用或未配置案例目标（CRAWL_WENSHU_ENABLED / CRAWL_WENSHU_*）"}
    if dry_run:
        return {"dry_run": True, "targets": [t.cursor_name for t in targets],
                "list_urls": [t.list_url for t in targets]}

    # 注入 fetcher：pipeline 不接管浏览器生命周期（由 runner 统一开关）。
    pipeline = CrawlPipeline(fetcher=fetcher)
    for t in targets:
        pipeline.scheduler.submit(t)
    outcomes = await pipeline.run_until_idle(max_jobs=max_jobs or len(targets))
    return {
        "targets": len(targets),
        "saved": sum(o.saved for o in outcomes),
        "updated": sum(o.updated for o in outcomes),
        "duplicate": sum(o.duplicate for o in outcomes),
        "failed": sum(o.failed for o in outcomes),
        "outcomes": [o.to_dict() for o in outcomes],
    }


# --------------------------------------------------------------------------- 一轮
def _in_time_window() -> tuple[bool, str]:
    win = settings.crawl_time_window
    if win is None:
        return True, ""
    from backend.crawler.scheduler import TimeWindow

    start, end = win
    hour = datetime.now().hour
    if TimeWindow.is_inside(hour, start, end):
        return True, ""
    return False, f"当前 {hour} 时不在采集时间窗 {start}:00-{end}:00 内"


async def run_once(
    *,
    fetcher: Any = None,
    statutes: bool = True,
    cases: bool = True,
    dry_run: bool = False,
    limit: int = 0,
    max_pages: int = 1,
    ingest: bool = True,
) -> dict[str, Any]:
    """跑一轮采集。``fetcher`` 可注入（单测用 ``StaticFetcher``）。"""
    started = time.perf_counter()
    if not settings.CRAWL_ENABLED:
        return {"ok": False, "skipped": True,
                "reason": "CRAWL_ENABLED=false（采集未启用）"}
    if fetcher is None and not playwright_available():
        return {"ok": False, "skipped": True,
                "reason": "未安装 playwright（pip install playwright && playwright install chromium）"}
    ok_win, why = _in_time_window()
    if not ok_win:
        return {"ok": True, "skipped": True, "reason": why}

    own = fetcher is None
    fetcher = fetcher or PlaywrightFetcher()
    summary: dict[str, Any] = {"ok": True, "dry_run": dry_run, "statutes": None, "cases": None}
    try:
        # 注入的 fetcher 可能没有 start/close（如单测的 StaticFetcher），逐个探测。
        starter = getattr(fetcher, "start", None)
        if starter is not None:
            await starter()
        if statutes and settings.CRAWL_RUN_STATUTES:
            summary["statutes"] = await run_statutes(
                fetcher, dry_run=dry_run, limit=limit, max_pages=max_pages)
        if cases and settings.CRAWL_RUN_CASES:
            summary["cases"] = await run_cases(fetcher, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 - 一轮异常不该让任务计划报"没有输出"
        logger.exception("采集执行异常")
        summary["ok"] = False
        summary["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if own:
            closer = getattr(fetcher, "close", None)
            if closer is not None:
                try:
                    await closer()
                except Exception:  # noqa: BLE001
                    pass

    summary["elapsed_ms"] = int((time.perf_counter() - started) * 1000)

    st = summary.get("statutes") or {}
    if ingest and not dry_run and isinstance(st, dict) and st.get("saved") \
            and settings.CRAWL_INGEST_STATUTES:
        summary["ingest"] = ingest_statutes()
    return summary


# --------------------------------------------------------------------------- 常驻
async def serve(*, interval_minutes: float | None = None, **kwargs: Any) -> None:
    """常驻循环：每轮跑 :func:`run_once`，睡眠到下一轮。Ctrl+C 退出。"""
    minutes = float(settings.CRAWL_INTERVAL_MINUTES if interval_minutes is None
                    else interval_minutes)
    logger.info("采集常驻启动：每 %.1f 分钟一轮（Ctrl+C 退出）", minutes)
    while True:
        summary = await run_once(**kwargs)
        logger.info("一轮采集结束：%s",
                    json.dumps(_slim(summary), ensure_ascii=False, default=str))
        await asyncio.sleep(max(1.0, minutes * 60))


# --------------------------------------------------------------------------- 输出
def _slim(summary: dict[str, Any]) -> dict[str, Any]:
    """日志用精简版（不把每个 outcome 的整块 dict 打进去）。"""
    out: dict[str, Any] = {"ok": summary.get("ok"), "skipped": summary.get("skipped"),
                           "reason": summary.get("reason"),
                           "elapsed_ms": summary.get("elapsed_ms")}
    st = summary.get("statutes")
    if isinstance(st, dict):
        out["statutes"] = {"status": st.get("status"), "saved": st.get("saved"),
                           "skipped": st.get("skipped"), "failed": st.get("failed"),
                           "reason": st.get("reason")}
    cs = summary.get("cases")
    if isinstance(cs, dict):
        out["cases"] = {k: cs.get(k) for k in ("saved", "duplicate", "failed", "reason")
                        if k in cs}
    if summary.get("error"):
        out["error"] = summary["error"]
    if summary.get("ingest"):
        out["ingest"] = summary["ingest"]
    return out


def exit_code(summary: dict[str, Any]) -> int:
    if summary.get("skipped"):
        return 0                      # 未启用 / 不在时间窗：不是失败，不该报警
    if not summary.get("ok"):
        return 1
    statuses: set[str] = set()
    st = summary.get("statutes")
    if isinstance(st, dict) and st.get("status"):
        statuses.add(str(st["status"]))
    for o in (summary.get("cases") or {}).get("outcomes", []) or []:
        if o.get("status"):
            statuses.add(str(o["status"]))
    if "failed" in statuses:
        return 1
    if "paused" in statuses:
        return 3
    return 0


def _print_report(summary: dict[str, Any]) -> None:
    if summary.get("skipped"):
        print(f"本轮跳过：{summary.get('reason') or '未启用'}")
        return
    st = summary.get("statutes")
    if isinstance(st, dict):
        if st.get("skipped"):
            print(f"法条：跳过（{st.get('reason')}）")
        else:
            # 带 "待采"：dry-run 下 saved 恒为 0，只报"新增 0"会让人以为没扫到东西。
            print(f"法条：待采 {st.get('fetched', 0)}，新增 {st.get('saved', 0)}，"
                  f"跳过 {st.get('skipped', 0)}，失败 {st.get('failed', 0)}，"
                  f"状态 {st.get('status')}"
                  + (f"（{st.get('reason')}）" if st.get("reason") else ""))
    cs = summary.get("cases")
    if isinstance(cs, dict):
        if cs.get("skipped"):
            print(f"案例：跳过（{cs.get('reason')}）")
        elif cs.get("dry_run"):
            print(f"案例（dry-run）：{len(cs.get('targets') or [])} 个目标")
            for u in cs.get("list_urls") or []:
                print(f"  · {u}")
        else:
            print(f"案例：新增 {cs.get('saved', 0)}，修订 {cs.get('updated', 0)}，"
                  f"去重 {cs.get('duplicate', 0)}，失败 {cs.get('failed', 0)}")
    if summary.get("ingest") is not None:
        ing = summary["ingest"]
        print(f"建索引：{'成功' if ing.get('ok') else '失败 ' + str(ing)}")
    if summary.get("error"):
        print(f"错误：{summary['error']}")
    print(f"耗时 {summary.get('elapsed_ms', 0)} ms")


# --------------------------------------------------------------------------- 入口
def main(argv: list[str] | None = None) -> int:
    from backend.app.logging_conf import setup_logging

    parser = argparse.ArgumentParser(
        prog="python -m backend.crawler.runner",
        description="采集自动执行器：法条 + 案例，一轮（--once）或常驻（--serve）",
    )
    parser.add_argument("--once", action="store_true",
                        help="跑一轮就退出（Windows 任务计划程序 / cron 用；默认即此）")
    parser.add_argument("--serve", action="store_true",
                        help="常驻，按 CRAWL_INTERVAL_MINUTES 循环")
    parser.add_argument("--interval", type=float, default=None, help="常驻间隔（分钟）")
    parser.add_argument("--statutes-only", action="store_true", help="只跑法条")
    parser.add_argument("--cases-only", action="store_true", help="只跑案例")
    parser.add_argument("--dry-run", action="store_true", help="只预演：法条列清单、案例打印目标 URL")
    parser.add_argument("--limit", type=int, default=0, help="本轮法条最多下载几篇（0=不限，全局额度）")
    parser.add_argument("--max-pages", type=int, default=1, help="法条关键词任务最多翻几页")
    parser.add_argument("--no-ingest", action="store_true", help="法条采完不建索引")
    parser.add_argument("--headed", action="store_true",
                        help="有头运行（首次登录 / 现场过验证码用；登录态会落盘）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出汇总")
    parser.add_argument("--no-init-db", action="store_true", help="跳过启动时的建表/修复外键")
    args = parser.parse_args(argv)

    setup_logging()

    if not args.no_init_db:
        try:
            from backend.app.db import session as db_session

            db_session.init_db()
        except Exception as exc:  # noqa: BLE001 - 建表失败不该阻止采集（库可能稍后可用）
            logger.warning("初始化数据库失败（继续尝试）：%s: %s", type(exc).__name__, exc)

    if args.headed:
        # 仅本次进程有效：登录态会落盘，之后可恢复无头。
        settings.CRAWL_HEADLESS = False

    statutes = not args.cases_only
    cases = not args.statutes_only
    ingest = settings.CRAWL_INGEST_STATUTES and not args.no_ingest

    if args.serve:
        try:
            asyncio.run(serve(
                interval_minutes=args.interval, statutes=statutes, cases=cases,
                dry_run=args.dry_run, limit=args.limit, max_pages=args.max_pages,
                ingest=ingest,
            ))
        except KeyboardInterrupt:
            logger.info("已中断（KeyboardInterrupt）")
            return 130
        return 0

    summary = asyncio.run(run_once(
        statutes=statutes, cases=cases, dry_run=args.dry_run,
        limit=args.limit, max_pages=args.max_pages, ingest=ingest,
    ))
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    else:
        _print_report(summary)
    return exit_code(summary)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
