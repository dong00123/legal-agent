"""访问层的优化点单测：不依赖真浏览器，只验行为契约。

覆盖：
- 浏览器断开 / 不可重试错误的分类（P0-1、P0-6）；
- 资源拦截路由只拦图片/媒体/字体、放行文档（P1-1）；
- 上下文**按量轮换**（P0-3）；
- 下载重试并守节流（P0-5）；
- 启动参数**不含反检测项**（合规红线，防回退）。
"""

from __future__ import annotations

import asyncio

from backend.app.config import settings
from backend.crawler.fetcher import (
    PlaywrightFetcher,
    _block_route,
    _is_browser_gone,
    _is_non_retryable,
)


def _fetcher(tmp_path, **kw) -> PlaywrightFetcher:
    # 两个目录都指到临时目录：默认值会在 data/ 下建目录，单测不该碰真实数据。
    return PlaywrightFetcher(state_dir=tmp_path, artifact_dir=tmp_path, **kw)


# ------------------------------------------------------------------ 错误分类
def test_browser_gone_detection():
    assert _is_browser_gone("Error: Target closed")
    assert _is_browser_gone("Browser has been closed")
    assert _is_browser_gone("Target page, context or browser has been closed")
    assert not _is_browser_gone("Timeout 30000ms exceeded")


def test_non_retryable_detection():
    assert _is_non_retryable("Error: net::ERR_INVALID_URL")
    assert _is_non_retryable("Cannot navigate to invalid URL")
    # 普通超时可重试：不能误判成不可重试，否则网络抖动不再重试。
    assert not _is_non_retryable("Timeout 30000ms exceeded")


# ------------------------------------------------------------------ 资源拦截
class _ReqStub:
    def __init__(self, resource_type: str) -> None:
        self.resource_type = resource_type


class _Route:
    def __init__(self, resource_type: str) -> None:
        self.request = _ReqStub(resource_type)
        self.decision = ""

    async def abort(self) -> None:
        self.decision = "abort"

    async def continue_(self) -> None:
        self.decision = "continue"


def test_block_route_blocks_irrelevant_resources():
    for rtype in ("image", "media", "font"):
        r = _Route(rtype)
        asyncio.run(_block_route(r))
        assert r.decision == "abort", f"{rtype} 应被阻断"


def test_block_route_allows_document_and_xhr():
    # document/script/xhr/fetch 与 stylesheet 必须放行：
    # flk 依赖页面内 fetch；正文抽取依赖渲染（阻断 CSS 会让隐藏文本混入）。
    for rtype in ("document", "script", "xhr", "fetch", "stylesheet"):
        r = _Route(rtype)
        asyncio.run(_block_route(r))
        assert r.decision == "continue", f"{rtype} 应放行"


# ------------------------------------------------------------------ 按量轮换
def test_after_fetch_rotates_by_page_count(tmp_path, monkeypatch):
    f = _fetcher(tmp_path)
    monkeypatch.setattr(settings, "CRAWL_CONTEXT_MAX_PAGES", 2)
    rotated: list[str] = []

    async def _rotate(source: str) -> None:
        rotated.append(source)

    monkeypatch.setattr(f, "_rotate_context", _rotate)

    async def run() -> None:
        await f._after_fetch("s")       # 第 1 页：不轮换
        assert rotated == []
        await f._after_fetch("s")       # 第 2 页：达到阈值 → 轮换

    asyncio.run(run())
    assert rotated == ["s"], rotated


def test_after_fetch_no_rotation_when_unlimited(tmp_path, monkeypatch):
    f = _fetcher(tmp_path)
    monkeypatch.setattr(settings, "CRAWL_CONTEXT_MAX_PAGES", 0)   # 0 = 不限
    monkeypatch.setattr(f, "_rotate_context", lambda source: None)

    async def run() -> None:
        for _ in range(50):
            await f._after_fetch("s")

    asyncio.run(run())      # 不抛异常即通过；阈值 0 不该触发轮换


# ------------------------------------------------------------------ 下载重试
class _Resp:
    status = 200
    headers = {"content-type": "application/octet-stream"}

    async def body(self) -> bytes:
        return b"PK\x03\x04payload"


class _Req:
    def __init__(self) -> None:
        self.n = 0

    async def get(self, url: str, timeout=None):  # noqa: ANN001
        self.n += 1
        if self.n == 1:
            raise RuntimeError("connection reset")
        return _Resp()


class _Ctx:
    def __init__(self) -> None:
        self.request = _Req()


def test_fetch_bytes_retries_and_throttles(tmp_path, monkeypatch):
    f = _fetcher(tmp_path)
    monkeypatch.setattr(settings, "CRAWL_DOWNLOAD_RETRY", 1)
    ctx = _Ctx()
    calls: list[str] = []

    async def _context(source: str):  # noqa: ANN202
        return ctx

    async def _throttle_file() -> None:
        calls.append("throttle_file")

    monkeypatch.setattr(f, "_context", _context)
    monkeypatch.setattr(f, "throttle_file", _throttle_file)

    status, body, _headers = asyncio.run(f.fetch_bytes("https://oss/x.docx", source="s"))

    assert status == 200 and body.startswith(b"PK")
    # 首次失败 → 节流 → 二次成功
    assert calls == ["throttle_file"], calls
    assert ctx.request.n == 2


# ------------------------------------------------------------------ 合规红线
def test_launch_args_has_no_stealth_flags(tmp_path):
    f = _fetcher(tmp_path)
    args = f._launch_args()
    joined = " ".join(args).lower()
    # 性能/稳定性调优可以有：
    assert "--disable-gpu" in args
    # 反检测/伪装身份的参数一律不得出现（违反站点条款）。
    assert "automationcontrolled" not in joined
    assert "user-agent" not in joined
    assert "webdriver" not in joined
