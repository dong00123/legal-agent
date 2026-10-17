"""访问层：Playwright 取页，把「像人一样访问」的规则集中在一处。

五件事，彼此独立又都要有：

- **登录态持久化**：``storage_state`` 落到 ``CRAWL_STATE_DIR/{source}.json``。
  每次抓取都重新登录既慢又更容易触发风控；不落盘则进程重启就要重登。
- **验证码转人工**：命中即截图存档 + 把任务挂起（有头模式下可现场等人过），
  **绝不尝试自动破解** —— 破解违反站点条款，且失败率高到会把账号拖进黑名单。
- **随机延迟 3~8s**：固定间隔本身就是可识别的机器特征。
- **指数退避重试**：网络抖动要重试，但退避必须封顶，否则 2^n 会涨到几十分钟。
- **代理预留**：一次请求独占一个代理条目，用完归还；超过复用上限即从池中退役。

playwright 是**惰性导入**的：没装的机器上 import 本模块不会失败，只有在真正
发起抓取时才抛 ``PlaywrightMissing`` —— 与项目「可选依赖 + 降级」的约定一致。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol
from urllib.parse import urlparse

from backend.app.config import settings
from backend.crawler.models import FetchResult

logger = logging.getLogger("legal_agent.crawler.fetcher")


# --------------------------------------------------------------------------- 异常
class CrawlError(RuntimeError):
    """采集访问层异常基类。

    ``retryable`` 决定它进不进重试循环：网络抖动可重试，"代理池已满"也可重试
    （等别人还），而"验证码"与"没装 playwright"重试多少次都一样。
    """

    def __init__(self, msg: str, *, retryable: bool = False) -> None:
        super().__init__(msg)
        self.retryable = retryable


class PlaywrightMissing(CrawlError):
    def __init__(self, msg: str = "未安装 playwright，采集不可用") -> None:
        super().__init__(msg, retryable=False)


class ProxyPoolBusy(CrawlError):
    def __init__(self, msg: str = "代理池已满，暂无可预留的代理") -> None:
        super().__init__(msg, retryable=True)


class CaptchaRequired(CrawlError):
    """命中验证码/人机校验 → **转人工**，不重试、不尝试破解。"""

    def __init__(self, msg: str, *, url: str = "", screenshot: str = "") -> None:
        super().__init__(msg, retryable=False)
        self.url = url
        self.screenshot = screenshot


# --------------------------------------------------------------------------- 代理池
@dataclass
class ProxyLease:
    proxy: str = ""
    uses: int = 0


class ProxyPool:
    """代理预留池。

    「预留」而不是「轮询取一个」：同一时刻一个代理只归一个请求用，
    否则两个请求共用一个出口 IP，风控看到的请求密度翻倍 —— 比不用代理更糟。
    池子空了就**显式报忙**（可重试），而不是悄悄回落到直连：
    直连会把本机真实 IP 暴露给站点，是这层最不该发生的失手。
    """

    def __init__(self, proxies: list[str] | None = None, max_uses: int = 0) -> None:
        self.max_uses = max(0, int(max_uses))
        self._entries: list[dict[str, Any]] = [
            {"proxy": p, "in_use": False, "uses": 0} for p in (proxies or [])
        ]
        self._rr = 0

    @property
    def configured(self) -> bool:
        return bool(self._entries)

    @property
    def available(self) -> int:
        return sum(1 for e in self._entries if not e["in_use"])

    def acquire(self) -> ProxyLease:
        if not self._entries:
            return ProxyLease("")          # 未配置代理 → 直连（有意为之）
        n = len(self._entries)
        for i in range(n):
            e = self._entries[(self._rr + i) % n]
            if not e["in_use"]:
                e["in_use"] = True
                self._rr = (self._rr + i + 1) % n
                return ProxyLease(proxy=e["proxy"], uses=int(e["uses"]))
        raise ProxyPoolBusy()

    def touch(self, lease: ProxyLease) -> bool:
        """记一次使用。返回 True 表示已达复用上限、需要轮换上下文。"""
        if not lease.proxy:
            return False
        for e in self._entries:
            if e["proxy"] == lease.proxy:
                e["uses"] = int(e["uses"]) + 1
                lease.uses = e["uses"]
                return bool(self.max_uses) and e["uses"] >= self.max_uses
        return False

    def release(self, lease: ProxyLease, *, dead: bool = False) -> None:
        if not lease.proxy:
            return
        for e in list(self._entries):
            if e["proxy"] == lease.proxy:
                e["in_use"] = False
                if dead or (self.max_uses and e["uses"] >= self.max_uses):
                    self._entries.remove(e)
                return

    def stats(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "total": len(self._entries),
            "available": self.available,
            "max_uses": self.max_uses,
        }


# --------------------------------------------------------------------------- 登录态
class SessionStore:
    """登录态持久化：``storage_state`` JSON 一源一份。

    按源分文件而不是共用一个：不同站点的 cookie 域不同，混在一起既没用又更容易
    在某个站点失效时把另一站点的登录态一起清掉。
    """

    def __init__(self, state_dir: str | Path | None = None) -> None:
        self.dir = Path(state_dir or settings.CRAWL_STATE_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, source: str) -> Path:
        safe = "".join(c for c in (source or "default") if c.isalnum() or c in "-_") or "default"
        return self.dir / f"{safe}.json"

    def exists(self, source: str) -> bool:
        p = self.path(source)
        return p.exists() and p.stat().st_size > 0

    def as_state_arg(self, source: str) -> str | None:
        """给 Playwright 的 ``storage_state`` 参数。不存在时返回 None（首次登录）。"""
        return str(self.path(source)) if self.exists(source) else None

    def clear(self, source: str) -> None:
        try:
            self.path(source).unlink(missing_ok=True)
        except OSError as exc:  # noqa: BLE001
            logger.warning("清理登录态失败 %s：%s", source, exc)


# --------------------------------------------------------------------------- 协议
class BaseFetcher(Protocol):
    """访问层对外契约。流水线只依赖它，故测试可注入 ``StaticFetcher``。"""

    async def fetch(
        self, url: str, *, source: str = "", dom_selectors: dict[str, str] | None = None
    ) -> FetchResult: ...


# --------------------------------------------------------------------------- 工具函数
def playwright_available() -> bool:
    """playwright 包是否可导入（浏览器内核是否已下载只能在启动时才知道）。"""
    return importlib.util.find_spec("playwright") is not None


def _proxy_settings(proxy_url: str) -> dict[str, str] | None:
    if not proxy_url:
        return None
    p = urlparse(proxy_url)
    if not p.hostname:
        logger.warning("代理地址无法解析，已忽略：%s", proxy_url)
        return None
    server = f"{p.scheme or 'http'}://{p.hostname}"
    if p.port:
        server += f":{p.port}"
    out = {"server": server}
    if p.username:
        out["username"] = p.username
    if p.password:
        out["password"] = p.password
    return out


def _looks_like_captcha(text: str, title: str = "") -> bool:
    keywords = settings.crawl_captcha_keyword_list
    if not keywords:
        return False
    haystack = f"{title}\n{text}"
    return any(k in haystack for k in keywords)


# 资源类型黑名单：这些对**文本采集**毫无价值，却会显著拉长加载时间与内存占用。
# 只按资源类型判、不按域名判：站点自有的 CSS/JS 必须放行（flk 依赖页面内 fetch）。
_BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font"})

# 重试没有意义的错误：URL 本身不合法。退避再久也是白等，直接判失败。
_NON_RETRYABLE_MARKERS = (
    "Malformed URL", "Invalid URL", "net::ERR_INVALID_URL",
    "Cannot navigate to invalid URL", "unknown url scheme",
)

# 浏览器/上下文已关闭：这是"环境坏了"，正确反应是**重建浏览器后立即重试**，
# 而不是当普通失败去等指数退避（等完还是失败）。
_BROWSER_GONE_MARKERS = (
    "target closed", "browser closed", "browser has been closed",
    "target page, context or browser has been closed", "disconnected",
)


async def _block_route(route: Any) -> None:
    """路由回调：阻断无关资源，其余放行。"""
    try:
        if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
            await route.abort()
        else:
            await route.continue_()
    except Exception:  # noqa: BLE001 - 页面关闭瞬间路由可能抛错，兜底放行
        try:
            await route.continue_()
        except Exception:  # noqa: BLE001
            pass


def _is_browser_gone(msg: str) -> bool:
    low = (msg or "").lower()
    return any(k in low for k in _BROWSER_GONE_MARKERS)


def _is_non_retryable(msg: str) -> bool:
    return any(k in (msg or "") for k in _NON_RETRYABLE_MARKERS)


# --------------------------------------------------------------------------- 访问层实现
class PlaywrightFetcher:
    """Playwright 取页器。

    生命周期用 ``async with``：浏览器与上下文要显式关闭，否则进程退出时
    Chromium 子进程会变成孤儿进程（Windows 上尤其明显，任务管理器里
    一堆 ``chrome.exe`` 指着已退出的父进程）。
    """

    def __init__(
        self,
        *,
        headless: bool | None = None,
        timeout_ms: int | None = None,
        delay_min: float | None = None,
        delay_max: float | None = None,
        max_retry: int | None = None,
        backoff: float | None = None,
        backoff_max: float | None = None,
        proxies: list[str] | None = None,
        state_dir: str | Path | None = None,
        artifact_dir: str | Path | None = None,
        on_captcha: Callable[[dict[str, Any]], Awaitable[str] | str] | None = None,
    ) -> None:
        self.headless = settings.CRAWL_HEADLESS if headless is None else bool(headless)
        self.timeout_ms = int(timeout_ms or settings.CRAWL_TIMEOUT_MS)
        lo, hi = settings.crawl_delay_range
        self.delay_min = lo if delay_min is None else float(delay_min)
        self.delay_max = hi if delay_max is None else float(delay_max)
        self.max_retry = int(settings.CRAWL_MAX_RETRY if max_retry is None else max_retry)
        self.backoff = float(settings.CRAWL_RETRY_BACKOFF if backoff is None else backoff)
        self.backoff_max = float(
            settings.CRAWL_RETRY_BACKOFF_MAX if backoff_max is None else backoff_max
        )
        self.sessions = SessionStore(state_dir or settings.crawl_state_dir)
        self.artifact_dir = Path(artifact_dir or settings.crawl_artifact_dir)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.proxies = ProxyPool(
            settings.crawl_proxy_list if proxies is None else proxies,
            settings.CRAWL_PROXY_MAX_USES,
        )
        self.on_captcha = on_captcha

        self._pw: Any = None
        self._browser: Any = None
        self._contexts: dict[str, Any] = {}
        self._leases: dict[str, ProxyLease] = {}
        # 每个来源已取页数：用于**按量轮换上下文**，独立于代理复用上限。
        self._page_counts: dict[str, int] = {}
        # 曾显式保存过登录态的来源：只有这些才在关闭时回写，避免把匿名态
        # 覆盖成"登录态"。
        self._logged_in: set[str] = set()
        # start() 的并发闸：两个协程同时启动会起两个 Chromium，泄漏其一。
        self._start_lock = asyncio.Lock()
        # 关闭中标志：断开回调据此区分"我们主动关的"与"它自己崩了"。
        self._closing = False
        self._atexit_registered = False
        self._last_request_at: float | None = None
        # 文件下载的时钟，**独立于站点请求**。共用会互相污染：下载把时钟推新后，
        # 下一次站点请求被迫多等，从 OSS 上省下的时间又还回去一半。
        self._last_file_at: float | None = None

    # ---------------------------------------------------------------- 生命周期
    async def __aenter__(self) -> "PlaywrightFetcher":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    @staticmethod
    def _launch_args() -> list[str]:
        """Chromium 启动参数。

        **刻意不含任何反检测参数**（如 ``--disable-blink-features=AutomationControlled``、
        指纹/UA 轮换）：本项目承诺"不伪装身份"，绕过人机校验属于违规。这里只做
        与性能、稳定性相关、且不改变身份的调优。
        """
        args = [
            "--disable-gpu",
            "--disable-dev-shm-usage",                  # 容器 / 低内存环境防崩溃
            "--disable-background-timer-throttling",    # 后台页定时器不被降频
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
        ]
        if settings.CRAWL_LAUNCH_NO_SANDBOX:
            args.append("--no-sandbox")                 # 仅容器内 root 运行时需要
        return args

    def _on_browser_disconnected(self, _browser: Any = None) -> None:
        """Chromium 断开（崩溃 / 被 kill）回调。

        回调是同步的，只清状态；真正的重建交给下一次 ``start()``。
        不清理的话 ``_browser`` 非 None 但已死，后续每次取页都会在死对象上
        重试到耗尽，且永不恢复。
        """
        if self._closing:
            return
        logger.warning("采集浏览器已断开，将在下次取页时重建")
        self._browser = None
        self._contexts.clear()
        for lease in list(self._leases.values()):
            self.proxies.release(lease)
        self._leases.clear()
        self._page_counts.clear()

    def _atexit_cleanup(self) -> None:
        """进程异常退出兜底：尽力关掉浏览器，减少 Windows 上的孤儿 chrome.exe。"""
        if self._browser is None:
            return
        self._closing = True
        try:
            asyncio.run(self.close())
        except Exception:  # noqa: BLE001 - 事件循环仍在跑时无法 asyncio.run
            pass

    async def start(self) -> None:
        # 已连接才算"已启动"：崩溃后 is_connected() 为 False，会走重建。
        if self._browser is not None and self._browser.is_connected():
            return
        async with self._start_lock:                    # 并发启动会泄漏一个 Chromium
            if self._browser is not None and self._browser.is_connected():
                return
            self._closing = False
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:  # pragma: no cover - 取决于环境
                raise PlaywrightMissing(
                    "未安装 playwright；请执行 `pip install playwright && playwright install chromium`"
                ) from exc
            self._pw = await async_playwright().start()
            try:
                self._browser = await self._pw.chromium.launch(
                    headless=self.headless, args=self._launch_args(),
                )
            except Exception as exc:  # noqa: BLE001 - 内核缺失等
                await self._pw.stop()
                self._pw = None
                raise PlaywrightMissing(f"启动 Chromium 失败（可能未下载内核）：{exc}") from exc
            try:
                self._browser.on("disconnected", self._on_browser_disconnected)
            except Exception:  # noqa: BLE001 - 老版本 API 差异，忽略即可
                pass
            if not self._atexit_registered:
                import atexit

                atexit.register(self._atexit_cleanup)
                self._atexit_registered = True
            logger.info("采集浏览器已启动（headless=%s，代理 %d 条）",
                        self.headless, self.proxies.stats()["total"])

    async def close(self) -> None:
        self._closing = True
        for src, ctx in list(self._contexts.items()):
            # 关闭前回写登录态：会话内站点常刷新 cookie，不落盘就白登录了。
            # 只回写"曾经登录过"的来源，避免把匿名态覆盖成登录态。
            if src in self._logged_in or self.sessions.exists(src):
                try:
                    await ctx.storage_state(path=str(self.sessions.path(src)))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("关闭前保存来源 %s 登录态失败：%s", src, exc)
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001
                pass
            lease = self._leases.get(src)
            if lease is not None:
                self.proxies.release(lease)
        self._contexts.clear()
        self._leases.clear()
        self._page_counts.clear()
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # noqa: BLE001
                pass
            self._browser = None
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None

    # ---------------------------------------------------------------- 上下文
    async def _context(self, source: str) -> Any:
        """取（或建）某来源的浏览器上下文。

        代理在**上下文**粒度绑定（Playwright 的限制），所以"代理预留"的语义是
        "一个上下文独占一个代理条目"；请求数超过复用上限时轮换上下文，
        从而把旧代理归还、换上一个新预留的。

        另有**按量轮换**（``CRAWL_CONTEXT_MAX_PAGES``）：与代理无关，纯粹防长跑
        内存膨胀与会话 cookie 过老 —— 默认代理复用上限是 0（永不轮换），
        没有这条兜底上下文就会一直用下去。
        """
        ctx = self._contexts.get(source)
        if ctx is not None:
            return ctx

        lease = self.proxies.acquire()
        kwargs: dict[str, Any] = {
            "user_agent": settings.CRAWL_USER_AGENT,
            "viewport": {"width": 1440, "height": 900},
        }
        state = self.sessions.as_state_arg(source)
        if state:
            kwargs["storage_state"] = state
        settings_proxy = _proxy_settings(lease.proxy)
        if settings_proxy:
            kwargs["proxy"] = settings_proxy

        ctx = await self._browser.new_context(**kwargs)
        ctx.set_default_timeout(self.timeout_ms)
        if settings.CRAWL_BLOCK_RESOURCES:
            # 阻断图片/媒体/字体：对文本采集无价值，却显著拖慢加载。
            # **不阻断 stylesheet**：正文抽取依赖渲染（inner_text 会排除隐藏节点）。
            try:
                await ctx.route("**/*", _block_route)
            except Exception as exc:  # noqa: BLE001 - 拦截失败只是慢，不影响正确性
                logger.warning("注册资源拦截路由失败（不影响采集）：%s", exc)
        if settings.CRAWL_TRACE_ON_ERROR:
            try:
                await ctx.tracing.start(screenshots=True, snapshots=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("启动 trace 失败（不影响采集）：%s", exc)
        self._page_counts[source] = 0
        self._contexts[source] = ctx
        self._leases[source] = lease
        logger.info("为来源 %s 新建上下文（代理=%s，登录态=%s）",
                    source or "-", lease.proxy or "直连", "已载入" if state else "首次")
        return ctx

    async def persist_session(self, source: str) -> None:
        """把当前登录态落盘。登录成功后调用一次即可。

        调用过本方法的来源会被记为"已登录"，从而在 ``close()`` 时**自动回写**
        —— 站点在会话中刷新 cookie 是常态，不自动回写就等于白登录。
        """
        ctx = self._contexts.get(source)
        if ctx is None:
            return
        try:
            await ctx.storage_state(path=str(self.sessions.path(source)))
            self._logged_in.add(source)
            logger.info("已保存来源 %s 的登录态", source)
        except Exception as exc:  # noqa: BLE001
            logger.warning("保存登录态失败 %s：%s", source, exc)

    async def _rotate_context(self, source: str) -> None:
        ctx = self._contexts.pop(source, None)
        lease = self._leases.pop(source, None)
        self._page_counts.pop(source, None)
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001
                pass
        if lease is not None:
            self.proxies.release(lease)

    async def _after_fetch(self, source: str) -> None:
        """一次取页成功后的记账：代理使用次数 + 上下文**按量轮换**。

        轮换有两路触发，任一命中即重建上下文：

        1. 代理复用上限（``CRAWL_PROXY_MAX_USES``，默认 0 = 永不）；
        2. 页面数上限（``CRAWL_CONTEXT_MAX_PAGES``）——与代理无关，防长跑
           内存膨胀与会话 cookie 过老。默认代理上限是 0，没有第 2 条兜底
           上下文就会一直用下去。
        """
        lease = self._leases.get(source)
        rotate = lease is not None and self.proxies.touch(lease)
        if rotate:
            logger.info("来源 %s 代理已用满 %d 次，轮换上下文",
                        source or "-", self.proxies.max_uses)
        self._page_counts[source] = self._page_counts.get(source, 0) + 1
        limit = int(settings.CRAWL_CONTEXT_MAX_PAGES)
        if limit and self._page_counts[source] >= limit:
            logger.info("来源 %s 已达 %d 页，按量轮换上下文以防内存膨胀",
                        source or "-", limit)
            rotate = True
        if rotate:
            await self._rotate_context(source)

    # ------------------------------------------------------- 会话内取页 / 取文件
    async def new_page(self, source: str = "") -> Any:
        """取一个页面（按需建上下文）。**调用方负责 close()**。"""
        ctx = await self._context(source)
        return await ctx.new_page()

    async def fetch_bytes(
        self, url: str, *, source: str = "", retries: int | None = None
    ) -> tuple[int, bytes, dict[str, str]]:
        """以**会话内身份**取二进制（文件下载）。返回 (状态码, 字节, 响应头)。

        用 ``context.request`` 而不是另起 httpx：它天然带上该上下文的
        cookies / UA / 代理，与页面里发起的下载是同一个身份。换成裸 httpx
        会被站点判成不同来源 —— 实测同一批接口，httpx 调一律报"系统异常"，
        而会话内调返回正常数据。

        带重试：站点给的 OSS 直链偶发抖动（连接重置 / 超时）不该让一条已经
        检查过的法规整条作废；重试前仍走 ``throttle_file()`` 守节流承诺。
        """
        ctx = await self._context(source)
        n = max(0, int(settings.CRAWL_DOWNLOAD_RETRY if retries is None else retries))
        last: Exception | None = None
        for i in range(n + 1):
            try:
                resp = await ctx.request.get(url, timeout=self.timeout_ms * 2)
                return int(resp.status), await resp.body(), dict(resp.headers)
            except Exception as exc:  # noqa: BLE001
                last = exc
                if i < n:
                    logger.info("下载失败（第 %d/%d 次），节流后重试：%s", i + 1, n + 1, url)
                    await self.throttle_file()
        raise CrawlError(f"下载失败（重试 {n} 次后仍失败）：{last}", retryable=True)

    # ---------------------------------------------------------------- 节流
    async def throttle(self) -> None:
        """站点**接口**的节流入口。

        走 JSON 接口取数（``page.evaluate`` 发 fetch）时不经过 ``fetch()``，
        随机延迟就不会被触发 —— 那条路径必须自己显式调用它，
        否则"接口比页面快"会让请求节奏变成机器节奏。
        """
        await self._throttle()

    async def throttle_file(self) -> None:
        """文件下载（站点给的 OSS 直链）的节流入口，走**独立时钟**。

        独立是必需的，不是风格选择：两者共用一个时钟会互相污染 —— 下载把时钟
        推新后，下一次站点请求被迫多等，从 OSS 上省下的时间又还回去一半。

        间隔也比接口短（``CRAWL_DOWNLOAD_DELAY_*``）：OSS 是对象存储，不是被
        robots 约束的检索接口，站点自己的下载按钮也没秒级间隔。
        """
        lo, hi = settings.crawl_download_delay_range
        self._last_file_at = await self._wait_since(self._last_file_at, lo, hi)

    async def _throttle(self) -> None:
        """相邻**站点请求**的随机间隔。"""
        self._last_request_at = await self._wait_since(
            self._last_request_at, self.delay_min, self.delay_max
        )

    @staticmethod
    async def _wait_since(last: float | None, lo: float, hi: float) -> float:
        """距 ``last`` 补足到一个随机目标间隔，返回新的时间戳。

        按"距上次已过多久"补足，而不是每次无脑 sleep：请求/下载本身的耗时就该
        抵扣下一次等待（否则一次 3 秒的下载会变成"下载 3s + 再等 5.5s"），
        重试退避已经等过的时间同理，不该再叠一遍。
        """
        now = time.monotonic()
        if last is None:
            return now
        target = random.uniform(min(lo, hi), max(lo, hi))
        wait = target - (now - last)
        if wait > 0:
            await asyncio.sleep(wait)
        return time.monotonic()

    # ---------------------------------------------------------------- 取页
    async def fetch(
        self, url: str, *, source: str = "", dom_selectors: dict[str, str] | None = None
    ) -> FetchResult:
        if self._browser is None or not self._browser.is_connected():
            await self.start()

        attempted = 0
        last_error = ""
        started = time.perf_counter()

        for attempt in range(self.max_retry + 1):
            attempted = attempt + 1
            try:
                await self._throttle()
                res = await self._fetch_once(url, source=source, dom_selectors=dom_selectors,
                                             attempts=attempted, started=started,
                                             retry_offset=attempt)
                logger.info("metrics fetch source=%s ok=%s attempts=%d elapsed_ms=%d proxy=%s",
                            source or "-", res.ok, res.attempts, res.elapsed_ms, res.proxy or "-")
                return res
            except CaptchaRequired:
                raise                                  # 转人工：重试无意义
            except ProxyPoolBusy:
                raise                                  # 交回调度层挂起，等代理归还
            except PlaywrightMissing:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
                if _is_browser_gone(last_error):
                    # 浏览器崩了：清状态并**立即重建**再试，不必等退避 ——
                    # 在已死的对象上退避等待，等完还是同样的失败。
                    logger.warning("采集浏览器已断开，重建后重试：%s", url)
                    self._on_browser_disconnected()
                    try:
                        await self.start()
                    except PlaywrightMissing:
                        raise
                    except Exception as exc2:  # noqa: BLE001
                        last_error = f"浏览器重建失败：{exc2}"
                        break
                    continue
                if _is_non_retryable(last_error):
                    logger.warning("取页失败（不可重试）%s：%s", url, last_error)
                    return FetchResult(
                        ok=False, url=url, error=last_error, attempts=attempted,
                        elapsed_ms=int((time.perf_counter() - started) * 1000),
                    )
                logger.warning("取页失败（第 %d/%d 次）%s：%s",
                               attempted, self.max_retry + 1, url, last_error)

            if attempt < self.max_retry:
                backoff = min(self.backoff * (2 ** attempt), self.backoff_max)
                backoff += random.uniform(0, backoff * 0.2)      # 抖动，避免重试同拍
                logger.info("退避 %.1fs 后重试 %s", backoff, url)
                await asyncio.sleep(backoff)

        logger.info("metrics fetch source=%s ok=False attempts=%d elapsed_ms=%d error=%s",
                    source or "-", attempted,
                    int((time.perf_counter() - started) * 1000), last_error)
        return FetchResult(
            ok=False, url=url, error=last_error, attempts=attempted,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    async def _wait_ready(self, page: Any, dom_selectors: dict[str, str] | None) -> None:
        """等待页面可用。

        **不再用 ``networkidle``**：它是 Playwright 官方标注不推荐的方式 ——
        站点若挂着长轮询 / 埋点长连接，"网络空闲"永远不到达，只能吃满超时；
        固定 ``wait_for_timeout`` 更纯粹是浪费。

        有明确选择器时等选择器（最贴合"我们要的字段渲染出来了"）；否则退到
        ``load``。两者超时都**不致命**：抽取层对缺失字段有正则兜底。
        """
        sels = [s for s in (dom_selectors or {}).values() if s]
        if sels:
            try:
                await page.wait_for_selector(",".join(sels),
                                             timeout=min(self.timeout_ms, 10_000))
                return
            except Exception:  # noqa: BLE001 - 等不到就继续，交给抽取层兜底
                pass
        try:
            await page.wait_for_load_state("load", timeout=min(self.timeout_ms, 10_000))
        except Exception:  # noqa: BLE001
            pass

    async def _fetch_once(
        self, url: str, *, source: str, dom_selectors: dict[str, str] | None,
        attempts: int, started: float, retry_offset: int = 0,
    ) -> FetchResult:
        ctx = await self._context(source)
        page = await ctx.new_page()
        try:
            resp = await page.goto(url, timeout=self.timeout_ms, wait_until="domcontentloaded")
            await self._wait_ready(page, dom_selectors)

            content = await page.content()
            title = ""
            try:
                title = await page.title()
            except Exception:  # noqa: BLE001
                pass
            # 用 evaluate 直接取渲染文本：与 inner_text("body") 等价，但省掉
            # "查 body 元素 + 取 innerText" 的两次往返，边缘（无 body）页面也不抛。
            text = ""
            try:
                text = await page.evaluate(
                    "() => (document.body ? document.body.innerText : '')"
                )
            except Exception:  # noqa: BLE001
                pass

            if _looks_like_captcha(text, title):
                shot = await self._handle_captcha(page, source, url)
                raise CaptchaRequired("命中验证码/人机校验，已转人工", url=url, screenshot=shot)

            dom = await _extract_dom(page, dom_selectors or {})

            # 先记下本条用的代理：_after_fetch 可能触发轮换，轮换后 lease 会被归还。
            lease = self._leases.get(source)

            # 记账 + 按量轮换（代理复用上限 / 页面数上限，任一命中即重建上下文）。
            await self._after_fetch(source)

            return FetchResult(
                ok=True, url=url,
                status=int(getattr(resp, "status", 0) or 0),
                html=content, text=text, dom=dom, title=title,
                attempts=attempts, proxy=(lease.proxy if lease else ""),
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
        finally:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------------- 验证码转人工
    async def _dump_trace(self, source: str) -> str:
        """把当前上下文的 trace 落盘（仅在 ``CRAWL_TRACE_ON_ERROR`` 下启用）。

        含页面快照与网络记录，是复盘"为什么走到验证码/为什么抽不到字段"最直接
        的证据；体积较大，所以默认关闭、只在失败路径 dump。
        """
        ctx = self._contexts.get(source)
        if ctx is None:
            return ""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self.artifact_dir / f"trace-{source or 'default'}-{stamp}.zip"
        try:
            await ctx.tracing.stop(path=str(path))
            logger.info("已保存 trace：%s", path)
            return str(path)
        except Exception as exc:  # noqa: BLE001 - trace 失败不影响主流程
            logger.warning("保存 trace 失败：%s", exc)
            return ""

    async def _handle_captcha(self, page: Any, source: str, url: str) -> str:
        """截图存档并把控制权交给人工。返回截图路径。"""
        if settings.CRAWL_TRACE_ON_ERROR:
            await self._dump_trace(source)          # 失败现场：trace 比日志可读得多
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        # 只截视口、输出 JPEG：``full_page`` + PNG 在超长页面上可能几十 MB，既慢
        # 又占盘，而人工判断"是不是验证码"看首屏就够了。
        shot = self.artifact_dir / f"captcha-{source or 'default'}-{stamp}.jpg"
        try:
            await page.screenshot(path=str(shot), full_page=False, type="jpeg", quality=70)
        except Exception as exc:  # noqa: BLE001 - 截图失败不能盖过"要转人工"这个事实
            logger.warning("验证码截图失败：%s", exc)
            shot = None  # type: ignore[assignment]

        logger.warning("采集命中验证码，已转人工：source=%s url=%s 截图=%s", source, url, shot)

        if self.on_captcha is not None:
            decision = self.on_captcha({"source": source, "url": url,
                                        "screenshot": str(shot) if shot else ""})
            if asyncio.iscoroutine(decision):
                decision = await decision
            if str(decision).lower() in ("resolved", "ok", "continue"):
                return str(shot) if shot else ""

        # 无回调时：有头模式现场等人（轮询关键词消失），无头模式只能挂起等重跑。
        if not self.headless and settings.CRAWL_CAPTCHA_WAIT_SECONDS > 0:
            logger.warning("有头模式下等待人工处理验证码，最长 %.0fs ……",
                           settings.CRAWL_CAPTCHA_WAIT_SECONDS)
            deadline = time.monotonic() + settings.CRAWL_CAPTCHA_WAIT_SECONDS
            while time.monotonic() < deadline:
                await asyncio.sleep(2.0)
                try:
                    if not _looks_like_captcha(await page.inner_text("body"),
                                               await page.title()):
                        logger.info("人工已完成验证，继续采集")
                        return str(shot) if shot else ""
                except Exception:  # noqa: BLE001 - 页面跳转中，继续等
                    continue
        return str(shot) if shot else ""


async def _extract_dom(page: Any, selectors: dict[str, str]) -> dict[str, str]:
    """按选择器抽 DOM 字段。

    **单次 ``evaluate`` 批量取**：逐个 ``locator().inner_text()`` 是 N 个字段 N 次
    CDP 往返，字段一多就不划算。语义一致（都用渲染后的 ``innerText``），
    单个选择器写错 / 元素消失都只丢该字段，不牵连整页。
    """
    sels = {k: v for k, v in (selectors or {}).items() if v}
    if not sels:
        return {}
    try:
        raw = await page.evaluate(
            """(sels) => {
                const out = {};
                for (const key in sels) {
                    const el = document.querySelector(sels[key]);
                    const t = el && el.innerText ? el.innerText.trim() : '';
                    if (t) out[key] = t;
                }
                return out;
            }""",
            sels,
        )
    except Exception as exc:  # noqa: BLE001 - 批量失败退回空字典，不拖垮整页
        logger.debug("DOM 批量抽取失败：%s", exc)
        return {}
    return {k: str(v) for k, v in (raw or {}).items() if v}


# --------------------------------------------------------------------------- 静态取页器
@dataclass
class StaticFetcher:
    """离线取页器：从预置字典或回调取页，供单测与 dry-run。

    存在的理由是让**解析层与存储层可以被独立验证** —— 没有它，任何一条断言
    都要先起一个真浏览器，测试从秒级变成分钟级，最后就没人跑了。
    """

    pages: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0
    calls: list[str] = field(default_factory=list)
    fail_urls: set[str] = field(default_factory=set)
    captcha_urls: set[str] = field(default_factory=set)

    async def fetch(
        self, url: str, *, source: str = "", dom_selectors: dict[str, str] | None = None
    ) -> FetchResult:
        self.calls.append(url)
        if self.delay:
            await asyncio.sleep(self.delay)
        if url in self.captcha_urls:
            raise CaptchaRequired("模拟验证码", url=url, screenshot="fake.png")
        if url in self.fail_urls:
            return FetchResult(ok=False, url=url, error="模拟失败")
        html = self.pages.get(url, "")
        if not html:
            return FetchResult(ok=False, url=url, error="页面不存在")
        from backend.crawler.parser import html_to_text
        return FetchResult(ok=True, url=url, status=200, html=html, text=html_to_text(html),
                           dom=_pseudo_dom(html, dom_selectors or {}))


def _pseudo_dom(html: str, selectors: dict[str, str]) -> dict[str, str]:
    """离线取页器的**伪 DOM 抽取**（仅支持 ``tag#id`` / ``#id`` / ``tag``）。

    只为让解析层的"DOM 优先"这条分支在没有真浏览器时也能被测到 ——
    否则所有测试都只能走正则兜底，DOM 路径永远没人验证，改坏了也发现不了。
    它不追求 CSS 选择器的完整性。
    """
    import re as _re

    from backend.crawler.parser import html_to_text

    out: dict[str, str] = {}
    for key, sel in (selectors or {}).items():
        sel = (sel or "").strip()
        if not sel:
            continue
        tag, _, elem_id = sel.partition("#")
        tag_re = _re.escape(tag) if tag else r"[a-zA-Z0-9]+"
        if elem_id:
            pat = rf"<{tag_re}[^>]*id=[\"']{_re.escape(elem_id)}[\"'][^>]*>(?P<v>.*?)</{tag_re}>"
        else:
            pat = rf"<{tag_re}[^>]*>(?P<v>.*?)</{tag_re}>"
        m = _re.search(pat, html or "", _re.S | _re.I)
        if not m:
            continue
        value = html_to_text(m.group("v")).strip()
        if value:
            out[key] = value
    return out
