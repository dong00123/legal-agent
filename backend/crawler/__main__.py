"""采集命令行入口：``python -m backend.crawler ...``

用法示例::

    # 查看运行态（配额 / 断点 / playwright 是否可用）
    python -m backend.crawler --status --source wenshu

    # 采集一个列表页的前 3 页
    python -m backend.crawler --source wenshu --url "https://example.com/list" \
        --cursor-key "劳动争议-2024" --max-pages 3 \
        --selectors "body=div#content,title=h1" \
        --link-pattern "href=\"(?P<u>/doc/\\d+)\""

    # 只采单个详情页
    python -m backend.crawler --source wenshu --doc-url "https://example.com/doc/1"

    # 清掉某来源的登录态，强制下次重新登录
    python -m backend.crawler --source wenshu --clear-session
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from backend.app.config import settings
from backend.app.db.session import init_db


def _parse_selectors(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, sel = part.partition("=")
        out[key.strip()] = sel.strip()
    return out


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m backend.crawler",
                                description="Playwright 采集（调度/访问/解析/存储 四层）")
    p.add_argument("--source", default="default", help="来源标识（也是登录态文件名）")
    p.add_argument("--url", default="", help="列表页 URL")
    p.add_argument("--doc-url", default="", help="只采单个详情页")
    p.add_argument("--cursor-key", default="", help="断点坐标；留空则用 URL")
    p.add_argument("--page-param", default="page", help="列表页的页码参数名")
    p.add_argument("--start-page", type=int, default=1)
    p.add_argument("--max-pages", type=int, default=0, help="本轮最多翻多少页；0=不限")
    p.add_argument("--link-pattern", default=r"""href=["'](?P<u>/[^"']+)["']""",
                   help="从列表页抽详情链接的正则，需含命名组 u")
    p.add_argument("--selectors", default="", help="DOM 选择器，形如 body=div#content,title=h1")
    p.add_argument("--priority", type=int, default=0)
    p.add_argument("--status", action="store_true", help="打印运行态后退出")
    p.add_argument("--clear-session", action="store_true", help="清掉该来源的登录态后退出")
    p.add_argument("--headed", action="store_true", help="强制有头模式（便于人工过验证码）")
    p.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    return p


async def _run(args: argparse.Namespace) -> int:
    from backend.crawler import CrawlPipeline, status
    from backend.crawler.fetcher import CaptchaRequired, PlaywrightMissing
    from backend.crawler.models import CrawlTarget
    from backend.crawler.fetcher import PlaywrightFetcher

    if args.status:
        print(json.dumps(status(args.source), ensure_ascii=False, indent=2, default=str))
        return 0

    if args.clear_session:
        from backend.crawler.fetcher import SessionStore
        SessionStore().clear(args.source)
        print(f"已清除来源 {args.source} 的登录态")
        return 0

    if not args.url and not args.doc_url:
        print("需要 --url（列表页）或 --doc-url（详情页）；用 --status 查看运行态",
              file=sys.stderr)
        return 2

    init_db()
    selectors = _parse_selectors(args.selectors)

    if args.doc_url:
        from backend.crawler import collect_url
        result = await collect_url(args.doc_url, source=args.source, dom_selectors=selectors)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if result.get("status") != "failed" else 1

    fetcher = PlaywrightFetcher(headless=False if args.headed else None)
    pipeline = CrawlPipeline(fetcher=fetcher)
    target = CrawlTarget(
        source=args.source,
        cursor_key=args.cursor_key,
        list_url=args.url,
        page_param=args.page_param,
        start_page=args.start_page,
        max_pages=args.max_pages,
        doc_link_pattern=args.link_pattern,
        dom_selectors=selectors,
    )
    pipeline.scheduler.submit(target, priority=args.priority)
    try:
        results = await pipeline.run_until_idle(max_jobs=1)
    except PlaywrightMissing as exc:
        print(f"采集不可用：{exc}", file=sys.stderr)
        return 3
    except CaptchaRequired:
        print("命中验证码，任务已转人工挂起；可用 --headed 重跑以现场处理", file=sys.stderr)
        return 4
    finally:
        await pipeline.close()

    print(json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=2, default=str))
    return 0 if results and results[0].status == "done" else 1


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not settings.CRAWL_ENABLED and not (args.status or args.clear_session):
        print("提示：CRAWL_ENABLED=false，仍将按命令行参数强制运行采集。"
              "生产请把它设为 true。", file=sys.stderr)
    return asyncio.run(_run(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
