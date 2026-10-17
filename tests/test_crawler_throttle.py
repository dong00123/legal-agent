"""采集的分级节流：站点接口 vs 文件下载。

慢是这套采集的常态 —— 防封靠的就是"每次请求都等几秒"。所以"哪里能省"必须
钉死：**文件下载**（站点给的 OSS 直链）走独立时钟 + 更短间隔，**站点接口**
一秒都不能松。

两者共用一个时钟的话，从下载上省下的时间会被下一次接口等待吃掉 —— 优化等于
没做，而且没有任何报错，只是"看起来还是慢"，极难查。
"""

from __future__ import annotations

import asyncio
import json
import time

from backend.app.config import settings
from backend.crawler.fetcher import PlaywrightFetcher
from backend.crawler.sites.flk import FlkCollector, FlkRow


def _fetcher(tmp_path, **kw) -> PlaywrightFetcher:
    # 两个目录都指到临时目录：默认值会在 data/ 下建目录，单测不该碰真实数据。
    return PlaywrightFetcher(state_dir=tmp_path, artifact_dir=tmp_path, **kw)


# ------------------------------------------------------------------ 配置层
def test_download_delay_is_shorter_than_request_delay():
    """默认配置下下载间隔必须**明显短于**接口间隔，否则这次优化是空转。"""
    d_lo, d_hi = settings.crawl_download_delay_range
    r_lo, _r_hi = settings.crawl_delay_range
    assert d_hi < r_lo, f"下载间隔 {d_lo}~{d_hi} 未短于接口间隔下限 {r_lo}"


# ------------------------------------------------------------------ 独立时钟
def test_clocks_are_independent(tmp_path):
    """接口节流走完紧接着下载，不该被接口时钟推后。

    共用时钟时这里会等满一轮**下载间隔**（默认 1~2 秒），断言随即失败。
    """
    f = _fetcher(tmp_path, delay_min=0.0, delay_max=0.0)

    async def run() -> float:
        await f.throttle()                  # 接口时钟首次：立即返回
        t0 = time.monotonic()
        await f.throttle_file()             # 下载时钟首次：也该立即返回
        return time.monotonic() - t0

    gap = asyncio.run(run())
    assert gap < 0.5, f"下载节流被接口时钟拖住了：{gap:.2f}s"


def test_download_clock_enforces_its_own_gap(tmp_path):
    """同一个时钟连调两次要补足间隔（否则"独立"就等于"不限速"）。"""
    f = _fetcher(tmp_path)

    async def run() -> float:
        await f.throttle_file()
        t0 = time.monotonic()
        await f.throttle_file()
        return time.monotonic() - t0

    lo, _hi = settings.crawl_download_delay_range
    gap = asyncio.run(run())
    assert gap >= lo * 0.8, f"下载间隔没生效：只等了 {gap:.2f}s（下限 {lo}）"


def test_wait_since_credits_elapsed_time():
    """已耗掉的时间要抵扣。

    否则一次 3 秒的下载会变成"下载 3 秒 + 再等满一轮"，重试退避叠加同理。
    """
    async def run() -> float:
        stale = time.monotonic() - 0.6          # 假装上次节流是 0.6 秒前
        t0 = time.monotonic()
        await PlaywrightFetcher._wait_since(stale, 1.0, 1.0)
        return time.monotonic() - t0

    gap = asyncio.run(run())
    assert 0.2 <= gap <= 0.8, f"应只补足约 0.4s，实际 {gap:.2f}s"


def test_wait_since_first_call_is_immediate():
    """首次调用不等待 —— 老实现靠 `is None` 分支，抽成公共方法后别丢掉。"""
    async def run() -> float:
        t0 = time.monotonic()
        await PlaywrightFetcher._wait_since(None, 5.0, 5.0)
        return time.monotonic() - t0

    assert asyncio.run(run()) < 0.2


# ------------------------------------------------------------------ 调用点
class _RecordingFetcher:
    """只记录调了哪个节流入口，不发任何请求。"""

    def __init__(self, body: bytes = b"") -> None:
        self.calls: list[str] = []
        self._body = body

    async def throttle(self) -> None:
        self.calls.append("throttle")

    async def throttle_file(self) -> None:
        self.calls.append("throttle_file")

    async def fetch_bytes(self, url: str, *, source: str = ""):
        self.calls.append("fetch_bytes")
        return 200, self._body, {}


def test_save_one_uses_file_throttle_for_download(tmp_path):
    """下载那一步必须走 throttle_file。

    钉在调用点上，是因为"改回 throttle 也一样能跑"—— 只有顺序断言能发现回退。
    """
    fake = _RecordingFetcher(body=b"PK\x03\x04" + b"x" * 200)

    async def _link(page, bbbs, fmt=None):       # 站点接口：直接给直链
        return "https://oss.example/doc.docx"

    collector = FlkCollector(fetcher=fake, output_dir=tmp_path)
    collector.download_link = _link

    row = FlkRow(bbbs="1", title="中华人民共和国测试法", gbrq="2024-01-01")
    written = asyncio.run(collector.save_one(object(), row))

    assert written is not None and written.exists()
    assert fake.calls == ["throttle", "throttle_file", "fetch_bytes"], fake.calls


class _FakePage:
    """按页返回检索结果的假页面。"""

    def __init__(self, total: int, per_page: int) -> None:
        self.total = total
        self.per_page = per_page
        self.pages: list[int] = []

    async def evaluate(self, script: str, payload: dict | None = None):
        pnum = int((payload or {}).get("pageNum") or 1)
        self.pages.append(pnum)
        start = (pnum - 1) * self.per_page
        rows = [{"bbbs": f"b{start + i}", "title": f"法规{start + i}"}
                for i in range(self.per_page) if start + i < self.total]
        return {"status": 200, "text": json.dumps({"total": self.total, "rows": rows})}


def test_scan_throttles_between_pages(tmp_path):
    """跨页扫描时第 2 页起要自己守间隔。

    调用方只在整段扫描前节流一次，跨页连打就成了"一次多请求"，与"每次请求都有
    随机间隔"的承诺不符。默认 page_size=20 时一页就够、走不到这里，所以这条
    分支只能靠小 page_size 触发 —— 也正是它一直没人发现的原因。
    """
    fake = _RecordingFetcher()
    collector = FlkCollector(fetcher=fake, output_dir=tmp_path, page_size=10)
    page = _FakePage(total=100, per_page=10)

    rows = asyncio.run(collector._scan(page, "民法", search_type=2, scan=25))

    assert len(rows) == 25
    assert page.pages == [1, 2, 3], page.pages
    assert fake.calls == ["throttle", "throttle"], fake.calls
