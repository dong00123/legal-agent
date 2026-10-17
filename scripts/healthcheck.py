"""服务健康检查（给部署、CI 与运维探针用）。

和 ``scripts/smoke_services.py`` 的区别：那个直接连数据库**在进程内**跑服务层，
用来验证代码逻辑；这个走 **HTTP**，用来验证"部署出来的那个进程是活的"。
排查线上问题时先跑这个 —— 它能在三秒内告诉你该往哪个方向查。

用法：
    python scripts/healthcheck.py                          # 本机默认 8001
    python scripts/healthcheck.py --url http://10.0.0.5:8080
    python scripts/healthcheck.py --wait 180               # 等服务起来（冷启动要加载模型）
    python scripts/healthcheck.py --strict                 # 有降级也算失败
    python scripts/healthcheck.py --json                   # 给流水线消费

退出码：
    0  正常或仅降级（业务可用）
    1  不可用（关键路径断了）
    2  ``--strict`` 且存在降级
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_URL = "http://127.0.0.1:8001"


def _get(url: str, timeout: float) -> tuple[int, dict[str, Any] | None, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        try:
            return resp.status, json.loads(raw), ""
        except json.JSONDecodeError:
            return resp.status, None, raw[:200]
    except urllib.error.HTTPError as exc:
        return exc.code, None, exc.read().decode("utf-8", "replace")[:200]
    except Exception as exc:  # noqa: BLE001
        return -1, None, f"{type(exc).__name__}: {exc}"


def probe(base: str, *, wait: float = 0.0, full: bool = True) -> tuple[dict[str, Any] | None, str]:
    """拉一次健康状态。``wait>0`` 时轮询到可用为止。"""
    url = f"{base.rstrip('/')}/health?full={'true' if full else 'false'}"
    deadline = time.monotonic() + max(0.0, wait)
    last = ""
    while True:
        code, body, err = _get(url, timeout=30 if not wait else 10)
        if body is not None:
            return body, ""
        last = err or f"HTTP {code}"
        if time.monotonic() >= deadline:
            return None, last
        time.sleep(2.0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="服务健康检查（HTTP 探针）")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"服务地址（默认 {DEFAULT_URL}）")
    parser.add_argument("--wait", type=float, default=0.0,
                        help="最多等多少秒到服务就绪（冷启动要加载 embedding 模型，给 120 以上）")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    parser.add_argument("--strict", action="store_true", help="存在降级时以退出码 2 结束")
    args = parser.parse_args(argv)

    # 轮询阶段用 full=false：完整检查会加载 embedding 模型，
    # 冷启动那十几秒里它连不上又慢，会被误判成"服务没起来"。
    body, err = probe(args.url, wait=args.wait, full=False)
    if body is None:
        print(f"服务不可用：{err}", file=sys.stderr)
        if args.json:
            print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
        return 1

    status = str(body.get("status") or "unknown")
    degraded = list(body.get("degraded") or [])
    problems = list(body.get("problems") or [])
    db = body.get("database") or {}

    # 就绪之后再拉一次完整的，取子系统与队列状态
    if not args.wait or status != "ok":
        full_body, _ = probe(args.url, wait=0.0, full=True)
        if full_body:
            body = full_body
    subsystems = body.get("subsystems") or {}
    queue = subsystems.get("task_queue") or {}

    payload = {
        "ok": status != "error",
        "status": status,
        "database_backend": db.get("backend"),
        "degraded": degraded,
        "problems": problems,
        "queue": {k: queue.get(k) for k in
                  ("pending", "queued", "running", "stale_running_count") if k in queue},
        "uptime_seconds": body.get("uptime_seconds"),
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"服务状态：{status}   运行 {body.get('uptime_seconds')} 秒")
        print(f"关系库后端：{db.get('backend')}")
        # 这条是**部署期**最容易踩的坑：容器里连不上 MySQL 时应用会静默
        # 回落到 SQLite，一切看起来正常，直到容器重启数据全没了。
        if db.get("backend") == "sqlite":
            print("  **当前跑在 SQLite 上。** 容器部署下这意味着数据在容器里，"
                  "重启即丢 —— 检查 DB_URL 里的主机名是否写成了 localhost（应为服务名 mysql）。")
        if problems:
            print("\n【不可用项】")
            for p in problems:
                print(f"  · {p}")
        if degraded:
            print("\n【降级项】业务仍可用，但少了一些能力：")
            for d in degraded:
                print(f"  · {d}")
        if payload["queue"]:
            print("\n【任务队列】")
            for k, v in payload["queue"].items():
                print(f"  {k}: {v}")
            if queue.get("warning"):
                print(f"  **{queue['warning']}**")
            if queue.get("warning_stale"):
                print(f"  **{queue['warning_stale']}**")

    if status == "error":
        return 1
    if args.strict and degraded:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
