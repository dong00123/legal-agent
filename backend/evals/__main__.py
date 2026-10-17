"""命令行入口：``python -m backend.evals``。

写成 ``__main__.py`` 而不是 ``cli.py``，是为了让``python -m backend.evals`` 直接可用 ——
与 worker 的 ``python -m backend.worker.app`` 是同一套约定。

**退出码有意义**（给 CI / 定时任务用）：

- ``0`` —— 报告正常生成；
- ``1`` —— 取数失败（数据库不可用）；
- ``2`` —— 语料体检发现空条文法规（数据问题，需要人工处理）。

第 2 条刻意让任务"红"：那些法规检索永远查不到，
而律师看到的是"没有相关规定"。让它保持绿色等于默认这件事可以忽略。
"""

from __future__ import annotations

import argparse
import json
import sys

from backend.app.logging_conf import setup_logging

from . import runner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.evals",
        description="法律智能体离线评测：从跑过的真实任务里算质量指标",
    )
    parser.add_argument("--days", type=int, default=7, help="回溯天数（默认 7）")
    parser.add_argument("--limit", type=int, default=500, help="最多取多少条任务（默认 500）")
    parser.add_argument("--out", default="data/logs/evals.json", help="JSON 输出路径")
    parser.add_argument("--no-out", action="store_true", help="不落盘，只打印")
    parser.add_argument("--json-only", action="store_true", help="只输出 JSON（给流水线）")
    parser.add_argument("--include-nonstandard", action="store_true",
                        help="把非标准编号的任务（测试/演示）也算进来，默认排除")
    parser.add_argument("--strict", action="store_true",
                        help="空条文法规存在时以退出码 2 结束（默认行为，显式声明用）")
    args = parser.parse_args(argv)

    if not args.json_only:
        setup_logging()

    report, text = runner.run(
        days=args.days,
        limit=args.limit,
        out=None if args.no_out else args.out,
        include_nonstandard=args.include_nonstandard,
    )

    if args.json_only:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(text)

    if report.get("corpus", {}).get("error"):
        return 1
    if (report.get("corpus", {}).get("empty_docs_count") or 0) > 0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
