# Playwright 浏览器自动化优化文档

> 适用范围：`legal-agent/backend/crawler/**`（四层采集）及 `backend/tools/crawl_tools.py`、`backend/app/config.py` 中的 `CRAWL_*` 配置。
> 目标：在**不触碰合规红线**（不破验证码、不做指纹伪装、不绕 robots）的前提下，提升稳定性、降低单页耗时与内存占用。
> 每条均给出「现状 → 问题 → 影响 → 改法（可直接落地）→ 验证」。
>
> **落地状态：P0-1 ~ P3-2 已实施**，改动集中在 `backend/crawler/fetcher.py`、`backend/crawler/sites/flk.py`、`backend/app/config.py`，回归见 `tests/test_crawler_fetcher_opts.py`、`tests/test_crawler_throttle.py`（15 项全绿）。

---

## 一、现状与相关文件

| 环节 | 文件 | 关键位置 |
| --- | --- | --- |
| 浏览器生命周期 / 上下文 | `backend/crawler/fetcher.py` | `start()`、`_context()`、`close()`、`_after_fetch()` |
| 取页 / 取文件 | 同上 | `fetch()`、`_fetch_once()`、`_wait_ready()`、`fetch_bytes()` |
| DOM 抽取 | 同上 | `_extract_dom()`（单次 `evaluate` 批量取） |
| 接口驱动采集（flk） | `backend/crawler/sites/flk.py` | `open_session()`、`save_one()` |
| 节流 | `fetcher.py` | `throttle()` / `throttle_file()` / `_wait_since()` |
| 配置项 | `backend/app/config.py` | `CRAWL_*` |
| 回归测试 | `tests/` | `test_crawler_throttle.py`、`test_crawler_fetcher_opts.py` |

现状链路：`chromium.launch(headless, args)` → 按 source 缓存 context（绑定代理 + storage_state + 资源拦截路由）→ 每次取页 `new_page()` → `goto(domcontentloaded)` + 等就绪 → 抽 `content/text/dom` → 记账并按量轮换 → `close()` 回写登录态。

---

## 二、优化清单总览

| 编号 | 优先级 | 问题 | 收益 | 落地位置 |
| --- | --- | --- | --- | --- |
| P0-1 | 高 | 浏览器崩溃后不自愈 | 稳定性 | `_on_browser_disconnected` / `fetch` |
| P0-2 | 高 | `start()` 无锁，并发启动多个 Chromium | 稳定性 | `start` + `_start_lock` |
| P0-3 | 高 | 上下文无「按量轮换」 | 稳定性 / 内存 | `_after_fetch` |
| P0-4 | 高 | 登录态不自动回写 | 正确性 | `close` + `persist_session` |
| P0-5 | 中 | 下载（`fetch_bytes`）无重试 | 稳定性 | `fetch_bytes` |
| P0-6 | 中 | 不可重试错误被反复重试 | 性能 / 体验 | `fetch` + `_is_non_retryable` |
| P1-1 | 高 | 未拦截图片/字体/媒体 | 单页耗时 | `_block_route` |
| P1-2 | 高 | 依赖 `networkidle` + 固定 sleep | 单页耗时 | `_wait_ready` / `flk.open_session` |
| P1-3 | 中 | `_extract_dom` 逐字段串行往返 | 单页耗时 | `_extract_dom` |
| P1-4 | 中 | 缺少启动参数调优 | 内存 | `_launch_args` |
| P2-1 | 中 | 异常退出可能残留孤儿进程 | 资源 | `_atexit_cleanup` |
| P2-2 | 低 | 验证码 `full_page` 截图过大 | 资源 | `_handle_captcha` |
| P3-1 | 中 | 失败现场无 trace | 可观测性 | `_dump_trace` |
| P3-2 | 低 | 关键指标未聚合 | 可观测性 | `fetch` 的 metrics 日志 |

---

## 三、P0 稳定性

### P0-1 浏览器崩溃后无自愈

**原现状**：`start()` 只在 `self._browser is None` 时启动；崩溃后对象非 None 但已死，后续每次取页都在死对象上重试到耗尽且永不恢复。

**改法**：注册 `browser.on("disconnected", ...)` 回调；回调（同步）只清状态不重建；`fetch()` 捕获到断开类错误时清状态并**立即** `start()` 重建后重试（不等退避）。

```python
def _on_browser_disconnected(self, _browser: Any = None) -> None:
    if self._closing:
        return
    logger.warning("采集浏览器已断开，将在下次取页时重建")
    self._browser = None
    self._contexts.clear()
    for lease in list(self._leases.values()):
        self.proxies.release(lease)
    self._leases.clear()
    self._page_counts.clear()
```

**验证**：`tests/test_crawler_fetcher_opts.py::test_browser_gone_detection`；端到端可手工 `kill` Chromium 后确认下一轮自动恢复。

---

### P0-2 `start()` 并发竞态

**改法**：`asyncio.Lock` 双重检查；`is_connected()` 为真才视为已启动。

```python
if self._browser is not None and self._browser.is_connected():
    return
async with self._start_lock:
    if self._browser is not None and self._browser.is_connected():
        return
    ...
```

---

### P0-3 上下文按量轮换

**原现状**：仅在 `CRAWL_PROXY_MAX_USES`（默认 0=永不）命中时轮换 → 默认配置下上下文永不轮换。

**改法**：新增独立阈值 `CRAWL_CONTEXT_MAX_PAGES`（默认 200），在 `_after_fetch()` 中与代理上限一并判定，任一命中即 `_rotate_context()`。

```python
self._page_counts[source] = self._page_counts.get(source, 0) + 1
limit = int(settings.CRAWL_CONTEXT_MAX_PAGES)
if limit and self._page_counts[source] >= limit:
    rotate = True
```

**验证**：`test_after_fetch_rotates_by_page_count`、`test_after_fetch_no_rotation_when_unlimited`。

---

### P0-4 登录态自动回写

**改法**：`persist_session()` 记录 `_logged_in`；`close()` 对所有「曾登录 / 已有状态文件」的来源回写 `storage_state`。

**注意**：只回写曾登录的来源，避免把匿名态覆盖成登录态。

**验证**：`persist_session(src)` 后 `close()`，状态文件时间戳被刷新。

---

### P0-5 下载（`fetch_bytes`）无重试

**改法**：`fetch_bytes(..., retries=None)` 内做 `CRAWL_DOWNLOAD_RETRY` 次重试，重试前走 `throttle_file()` 守节流；仍失败抛 `CrawlError(retryable=True)`。

**验证**：`test_fetch_bytes_retries_and_throttles`。

---

### P0-6 不可重试错误被反复重试

**改法**：`_is_non_retryable()` 命中 URL 非法类错误 → 直接返回失败，不进退避。**注意别把普通超时也判成不可重试**，否则网络抖动不再重试。

**验证**：`test_non_retryable_detection`。

---

## 四、P1 性能

### P1-1 拦截无关资源

**改法**：`context.route("**/*", _block_route)`，按资源类型阻断 `image/media/font`，放行 `document/script/xhr/fetch`，**不阻断 `stylesheet`**。

**风险提示**：正文抽取与 **渲染** 相关（`inner_text` 排除 CSS 隐藏节点），去掉样式会让本该隐藏的文本混进正文；纯接口驱动站点（如 flk）可自行再加 `stylesheet`。

**验证**：`test_block_route_blocks_irrelevant_resources`、`test_block_route_allows_document_and_xhr`。

---

### P1-2 替换 `networkidle` + 固定 sleep

**改法**：`_wait_ready()` —— 有选择器等选择器，否则退到 `load`；`flk.open_session()` 去掉 `networkidle(20s)` 与固定 `wait_for_timeout(2000)`，改等 `load`。超时都不致命（抽取层有正则兜底）。

---

### P1-3 `_extract_dom` 批量抽取

**改法**：单次 `page.evaluate` 循环 `document.querySelector` 批量返回，省掉 N 个字段 N 次 CDP 往返，语义与逐个 `inner_text` 一致。

---

### P1-4 启动参数调优

**改法**：`--disable-gpu`、`--disable-dev-shm-usage`、`--disable-background-timer-throttling`、`--disable-renderer-backgrounding`、`--disable-backgrounding-occluded-windows`；容器 root 场景可选 `--no-sandbox`。

**禁止**加入反检测参数（合规）。**验证**：`test_launch_args_has_no_stealth_flags`。

---

## 五、P2 资源与内存

- **P2-1 孤儿进程兜底**：`start()` 后注册 `atexit`，异常退出时尽力 `close()`；`_closing` 标志区分主动关闭与崩溃。
- **P2-2 验证码截图瘦身**：改 `full_page=False` + JPEG(q70)，人工看首屏即可。

---

## 六、P3 可观测性

- **P3-1 失败现场 trace**：`CRAWL_TRACE_ON_ERROR=true` 时上下文开 trace，命中验证码走 `_dump_trace()` 落 `trace-*.zip`；默认关闭（体积大）。
- **P3-2 指标聚合**：`fetch()` 结束打一条结构化 `metrics` 日志（source/ok/attempts/elapsed_ms/proxy），便于统计成功率与调参。

---

## 七、合规红线（明确不做）

- 破解/自动识别验证码，或绕过人机校验；
- 伪造或轮换浏览器指纹、UA、TLS 指纹做反检测；
- 忽略 `robots.txt`、突破站点频率限制或单日配额；
- 优化只允许在**稳定性、资源占用、单页耗时、可维护性**四个维度内进行。

---

## 八、新增配置项汇总

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `CRAWL_BLOCK_RESOURCES` | `True` | 阻断图片/媒体/字体（不阻断 stylesheet） |
| `CRAWL_CONTEXT_MAX_PAGES` | `200` | 单个上下文最多取多少页后轮换（0=不限） |
| `CRAWL_DOWNLOAD_RETRY` | `2` | `fetch_bytes` 下载重试次数 |
| `CRAWL_LAUNCH_NO_SANDBOX` | `False` | 容器 root 场景追加 `--no-sandbox` |
| `CRAWL_TRACE_ON_ERROR` | `False` | 失败时落 trace.zip |

---

## 九、验证与回归

```powershell
# 采集相关（节流语义 + 优化点）
.\.venv\Scripts\python.exe -m pytest tests/test_crawler_throttle.py tests/test_crawler_fetcher_opts.py -q
# 兼容性 / 其它
.\.venv\Scripts\python.exe -m pytest tests/test_py311_compat.py tests/test_draft_versions.py -q
```

端到端对比：同一目标页开/关优化开关，记录 `elapsed_ms` 分布与抽取字段一致性，确认**优化不提通过率、不改内容，只省资源与时间**。
