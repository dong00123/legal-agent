"""采集国家法律法规数据库（flk.npc.gov.cn）的法规原文，落到 data/raw/。

**合规提醒（必读）**

该站的 ``robots.txt`` 是**全站** ``Disallow: /``，首行还写明"禁止使用任何自动化
工具、脚本、爬虫程序采集或复制网站数据"；检索侧部署了验证码。运行本脚本前请
确认你有权这样做（单位内部使用、已向站点申请授权等）。脚本自身守住的边界：

- **不破解验证码**：命中即挂起转人工，不做任何绕过；
- 随机间隔（``CRAWL_DELAY_MIN/MAX``）与单日配额（``CRAWL_DAILY_QUOTA``）；
- 只采法规文本本身（《著作权法》第五条：法律、法规不属于著作权保护对象）。

**两种用法：命令行 / 配置**

命令行（临时、单次）：

    python scripts/fetch_flk_statutes.py --names 中华人民共和国民法典 中华人民共和国刑法
    python scripts/fetch_flk_statutes.py --file 我的法规清单.txt
    python scripts/fetch_flk_statutes.py --keyword 婚姻 --category 法律 --max-pages 2
    python scripts/fetch_flk_statutes.py --list-categories

    python scripts/fetch_flk_statutes.py --dry-run     # 按配置核对，不下载
    python scripts/fetch_flk_statutes.py               # 按配置正式采

**规则：命令行为准。** 只要命令行给了 ``--names/--file/--keyword/--category``
中任意一个，就完全不看配置 —— 避免"改了配置没生效"和"配置与命令行打架"两种困惑。

采到的文件放在 ``data/raw/国家法律法规库/``，命名 ``{法规名}_{公布日期}.docx``
（与该站自身命名一致），直接复用 ``backend.corpus.ingest``，不需要另建入库通道。

**名单文件格式**：每行一个法规名，``#`` 开头为注释，空行忽略；带不带书名号都可以
（``中华人民共和国民法典`` 与 ``《中华人民共和国民法典》`` 等价）。

**注意**：站点会返回同一法规的**历史版本**（实测《劳动法》有 2009 与 2018 两版）。
按名单采集默认只取公布日期最新的那一份，需要全收就加 ``--all-versions``
（或配 ``CRAWL_FLK_ALL_VERSIONS=true``）。
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.config import settings  # noqa: E402
from backend.crawler.sites.flk import (  # noqa: E402
    SOURCE,
    FlkCollector,
    FlkOutcome,
    dedup_names as _dedup,
    read_names_file as _read_names,
    resolve_output_dir,
    tasks_from_config as _tasks_from_config,
)


def _statute_counts() -> tuple[int, int]:
    """回查库里法规部数与条文数，用于导入前后对拍。"""
    try:
        from sqlalchemy import func, select

        from backend.app.db.models.knowledge import StatuteDoc
        from backend.app.db.session import session_scope

        with session_scope() as db:
            docs = int(db.execute(select(func.count()).select_from(StatuteDoc)).scalar_one() or 0)
            arts = int(db.execute(select(func.sum(StatuteDoc.article_count))).scalar_one() or 0)
        return docs, arts
    except Exception as exc:  # noqa: BLE001
        print(f"  （回查库失败：{type(exc).__name__}: {exc}）")
        return -1, -1


# --------------------------------------------------------------------------- 任务组装
def _tasks_from_cli(args: argparse.Namespace) -> list[dict]:
    names = list(args.names or [])
    if args.name_file:
        names.extend(_read_names(args.name_file))
    names = _dedup(names)
    if names:
        return [{"kind": "names", "names": names}]
    if args.keyword or args.category:
        return [{"kind": "search", "keyword": args.keyword, "category": args.category}]
    return []


def _task_label(task: dict) -> str:
    if task["kind"] == "names":
        return f"按名单（{len(task['names'])} 条）"
    kw = task.get("keyword") or "（不限关键词）"
    cat = task.get("category") or "（不限级别）"
    return f"关键词「{kw}」× 级别「{cat}」"


# --------------------------------------------------------------------------- 执行
async def _list_categories() -> int:
    collector = FlkCollector()
    page = await collector.open_session()
    try:
        cats = await collector.load_categories(page)
    finally:
        try:
            await page.close()
        finally:
            await collector.close()
    print("可用分类（--category / CRAWL_FLK_CATEGORIES 用这里的名字）：")
    for name, ids in sorted(cats.items()):
        print(f"  {name:<16} 编码 {len(ids)} 个")
    return 0


async def _run_tasks(args: argparse.Namespace, tasks: list[dict]) -> FlkOutcome:
    """顺序跑完所有任务，复用同一个浏览器会话。

    会话复用不是优化洁癖：每个任务独立开关浏览器的话，除了启动开销，
    还要重新暖场（加载 1.3MB 的 SPA 首屏），任务一多这几分钟就白花了。
    """
    all_versions = bool(args.all_versions or settings.CRAWL_FLK_ALL_VERSIONS)
    collector = FlkCollector(
        output_dir=args.out or None, fmt=args.format, overwrite=args.overwrite,
        # None = 用设置里的 CRAWL_FLK_ONLY_EFFECTIVE；命令行开关可强制关掉过滤
        only_effective=(False if args.include_ineffective else None),
    )
    merged = FlkOutcome()
    page = await collector.open_session()
    try:
        for idx, task in enumerate(tasks, 1):
            # --limit 是**全局**额度：按已用量扣减，否则配了 3 个任务
            # 每个都能下 100 条，实际下了 300 条。
            remain = 0
            if args.limit:
                remain = max(0, int(args.limit) - merged.fetched)
                if remain <= 0:
                    print(f"（已达 --limit {args.limit}，剩余任务不再执行）")
                    break
            print(f"[{idx}/{len(tasks)}] {_task_label(task)}"
                  + (f"  （本次额度 {remain}）" if args.limit else ""))
            if task["kind"] == "names":
                out = await collector.run_by_names(
                    task["names"], all_versions=all_versions, limit=remain,
                    dry_run=args.dry_run, page=page,
                )
            else:
                out = await collector.run(
                    keyword=task.get("keyword", ""), category=task.get("category", ""),
                    max_pages=args.max_pages, limit=remain,
                    dry_run=args.dry_run, page=page,
                )
            merged.merge(out)
            print(f"  → 新增 {out.saved}，跳过 {out.skipped}，失败 {out.failed}"
                  + (f"，未找到 {len(out.not_found)}" if out.not_found else ""))
            if out.status in ("paused", "failed"):
                print(f"  任务状态 {out.status}，停止后续任务：{out.reason}")
                break
    finally:
        try:
            await page.close()
        finally:
            await collector.close()
    return merged


# --------------------------------------------------------------------------- 报告
def _report(out: FlkOutcome, out_dir: Path) -> None:
    if out.requested:
        print(f"名单 {len(out.requested)} 条，选中 {out.total} 条待处理")
    else:
        print(f"\n站点命中 {out.total} 条；翻页 {out.pages} 页")
    print(f"尝试下载 {out.fetched} 条；新增 {out.saved}，"
          f"跳过（本地已存在）{out.skipped}，失败 {out.failed}")

    if out.versions:
        print("\n同名多版本（默认只取公布日期最新的；--all-versions 可全要）：")
        for name, dates in list(out.versions.items())[:10]:
            print(f"  · {name}：{'、'.join(dates)}")

    if out.filtered:
        # 同样必须报出来：静默丢弃会让"名单里那部法怎么没采到"无从查起。
        print(f"\n已跳过 {out.filtered} 条**非现行有效**"
              f"（旧版本/已废止；--include-ineffective 可一并采）：")
        for item in out.ineffective[:5]:
            print(f"  · {item.get('title')}（sxx={item.get('sxx')}，"
                  f"{item.get('gbrq') or '无日期'}）")
        if len(out.ineffective) > 5:
            print(f"  … 还有 {len(out.ineffective) - 5} 条")

    if out.not_found:
        # 必须报出来：名单写错一个字时，静默跳过会让"少采了"完全看不出来
        print(f"\n名单中未找到 {len(out.not_found)} 条（附站内最接近的候选）：")
        for item in out.not_found:
            print(f"  · {item['name']}")
            for c in item.get("candidates") or []:
                print(f"      候选：{c}")
            if not item.get("candidates"):
                print("      （精确与模糊检索都没有返回结果，可能是名字确实不存在）")

    if out.status != "done":
        print(f"[{out.status}] {out.reason}")
    if out.errors:
        print(f"\n失败明细（前 10 条 / 共 {len(out.errors)}）：")
        for e in out.errors[:10]:
            print(f"  · {e}")
    if out.saved:
        print(f"\n输出目录：{out_dir}")
        # 格式分布要报出来：站点对部分地方性法规只提供老式 .doc，
        # 脚本会自动改用公报 PDF 版；剩下的 .doc 则是项目解析不了的，
        # 必须让用户看见（否则只会在入库时报"抽不出文本"，方向全错）。
        low = [f.lower() for f in out.files]
        pdfs = [f for f in low if f.endswith(".pdf")]
        plain_docs = [f for f in out.files
                      if f.lower().endswith(".doc") and not f.lower().endswith(".docx")]
        if pdfs:
            print(f"  其中 {len(pdfs)} 篇以 PDF 保存"
                  f"（站点对这几条只提供老式 .doc，已自动改用公报 PDF 版）")
        if plain_docs:
            print(f"  ⚠ 其中 {len(plain_docs)} 篇是**老式 .doc**，python-docx 解析不了 ——"
                  f"入库前需先用 Word 另存为 .docx")
        for f in out.files[:10]:
            print(f"  · {f}")
        if len(out.files) > 10:
            print(f"  … 还有 {len(out.files) - 10} 个")


def _ingest(force: bool) -> int:
    before = _statute_counts()
    cmd = [sys.executable, "-m", "backend.corpus.ingest", "--statutes"]
    if force:
        cmd.append("--force")
    print("\n建索引：" + " ".join(cmd))
    proc = subprocess.run(cmd, cwd=str(Path(__file__).resolve().parents[1]))
    if proc.returncode != 0:
        print(f"\n入库进程退出码 {proc.returncode} —— 看上面它的输出。")
        return proc.returncode
    after = _statute_counts()
    if before[0] >= 0 and after[0] >= 0:
        print(f"\n法规部数 {before[0]} → {after[0]}；条文数 {before[1]} → {after[1]}")
        if after[0] == before[0] and after[1] == before[1]:
            print("  **条数没有变化。** 常见原因：同名文件被判为已入库而跳过"
                  "（加 --force 重试），或文件抽不出文本。")
    return 0


def _finish(out: FlkOutcome, args: argparse.Namespace, out_dir: Path) -> int:
    """报告 + 决定要不要建索引。两条入口（命令行 / 配置）共用。"""
    _report(out, out_dir)
    if args.dry_run or args.no_ingest:
        print("\n已跳过建索引。")
        return 0
    if out.status == "paused":
        print("\n任务已挂起（断点与配额都保留）；处理完后重跑本命令即可续采。")
        return 3
    if out.saved == 0:
        print("\n没有新文件需要入库。")
        return 0
    return _ingest(args.force)


# --------------------------------------------------------------------------- 入口
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="采集国家法律法规数据库的法规原文（请先阅读脚本开头的合规提醒）"
    )
    parser.add_argument("--names", nargs="+", default=[],
                        help="要采的法规名单（标题精确匹配），带不带《》都可以")
    parser.add_argument("--file", dest="name_file", default="",
                        help="法规名单文件（每行一个，# 开头为注释）")
    parser.add_argument("--all-versions", action="store_true",
                        help="同名多版本时全部下载（默认只取公布日期最新的）")
    parser.add_argument("--include-ineffective", action="store_true",
                        help="连已废止/已修改的旧版本一起采（默认只采现行有效）")
    parser.add_argument("--keyword", default="", help="检索关键词（模糊，配合 --max-pages 用）")
    parser.add_argument("--category", default="", help="法律法规分类，如「法律」「行政法规」")
    parser.add_argument("--max-pages", type=int, default=1, help="每个任务最多翻几页（默认 1）")
    parser.add_argument("--limit", type=int, default=0,
                        help="本次最多下载几篇（0 = 不限）。多任务时是全局额度")
    parser.add_argument("--format", default="docx", choices=["docx", "pdf"],
                        help="docx = WPS 版本（默认）；pdf = 公报原版")
    parser.add_argument("--out", default="",
                        help="输出目录（默认取设置里的 data/raw/国家法律法规库；"
                             "相对路径一律按项目根解析，与 cwd 无关）")
    parser.add_argument("--overwrite", action="store_true", help="覆盖本地已存在的同名文件")
    parser.add_argument("--dry-run", action="store_true", help="只检索并列出，不下载")
    parser.add_argument("--list-categories", action="store_true", help="列出可用分类后退出")
    parser.add_argument("--no-ingest", action="store_true", help="只下载，不建索引")
    parser.add_argument("--force", action="store_true", help="传给 ingest：强制重建索引")
    args = parser.parse_args(argv)

    if not settings.CRAWL_ENABLED:
        print("提示：CRAWL_ENABLED=false。本脚本仍会运行，但它会真实访问外站。")

    if args.list_categories:
        return asyncio.run(_list_categories())

    cli_given = bool(args.names or args.name_file or args.keyword or args.category)
    try:
        if cli_given:
            tasks = _tasks_from_cli(args)
        else:
            tasks, notes = _tasks_from_config()
            for n in notes:
                print(f"[提示] {n}")
            if tasks:
                print(f"未给命令行参数，改用 .env 里的 CRAWL_FLK_* 配置"
                      f"（共 {len(tasks)} 个任务）")
                for i, t in enumerate(tasks, 1):
                    print(f"  {i}. {_task_label(t)}")
    except Exception as exc:  # noqa: BLE001 - 名单文件读不了要明确报错
        print(f"组装任务失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if not tasks:
        print("没有可执行的任务：命令行没给参数，.env 里的 CRAWL_FLK_* 也都是空的。\n"
              "  · 看分类名：--list-categories\n"
              "  · 配置示例：.env.example 的 CRAWL_FLK_* 段", file=sys.stderr)
        return 2

    out_dir = resolve_output_dir(args.out or None)
    out = asyncio.run(_run_tasks(args, tasks))
    return _finish(out, args, out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
