"""采集类工具：把 Playwright 四层采集能力登记为 MCP 工具。

为什么登记成工具而不是让 Agent 节点直接 import ``backend.crawler``：与
``knowledge_search`` 同一个理由 —— 直接调用会绕过 Harness 的权限校验、审计留痕、
Trace 记账与次数配额四条约束。

**未启用/未装依赖时返回降级结果，而不是静默返回空**：``CRAWL_ENABLED=false``
或未装 playwright 时返回 ``degraded=True`` + 明确的 ``fallback_note``，
由上层写进证据缺口。否则"采不到"与"本来就没有这类内容"在报告里长得一模一样，
而这两件事对律师的意义完全不同。
"""

from __future__ import annotations

import logging

from backend.app.config import settings
from backend.tools.base import ToolResult

logger = logging.getLogger("legal_agent.tools.crawl")

# 建议的降级话术：告诉上层"该换哪条通道"，而不是只说一句"失败了"。
_FALLBACK_HINT = (
    "浏览器采集不可用，本次未获取外部文书；"
    "可改用 case_search 检索本地类案库，或让运维开启 CRAWL_ENABLED"
)


def _parse_selectors(raw: str) -> dict[str, str]:
    """把 ``body=div#content,title=h1`` 解析成字典。

    解析失败（缺 ``=``）的片段**跳过**而不是报错：选择器只是"更好的抽取方式"，
    缺了它还有正文正则兜底，不值得让整次采集失败。
    """
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, sel = part.partition("=")
        out[key.strip()] = sel.strip()
    return out


async def case_crawl(url: str, source: str = "default", selectors: str = "") -> ToolResult:
    """抓取一个文书详情页并入库。

    - ``url``：详情页地址（列表页请用 ``python -m backend.crawler --url``）。
    - ``source``：来源标识，同时决定登录态文件名与单日配额归属。
    - ``selectors``：DOM 选择器，形如 ``body=div#content,title=h1``；留空则完全靠
      正文正则兜底（案号/法院/日期）。

    幂等：存储层 ``doc_id`` + 正文哈希双去重，重复抓取不会产生重复数据，
    所以失败重试是安全的（``idempotent=True``）。
    """
    if not settings.CRAWL_ENABLED:
        return ToolResult(ok=False, tool="case_crawl", degraded=True,
                          error="采集未启用（CRAWL_ENABLED=false）",
                          fallback_note=_FALLBACK_HINT)

    from backend.crawler import collect_url
    from backend.crawler.fetcher import PlaywrightMissing, playwright_available
    from backend.crawler.models import JOB_FAILED, JOB_PAUSED

    if not playwright_available():
        return ToolResult(ok=False, tool="case_crawl", degraded=True,
                          error="未安装 playwright",
                          fallback_note="未安装 playwright（pip install playwright "
                                        "&& playwright install chromium）；" + _FALLBACK_HINT)

    sel = _parse_selectors(selectors)
    try:
        result = await collect_url(url, source=source, dom_selectors=sel)
    except PlaywrightMissing as exc:
        return ToolResult(ok=False, tool="case_crawl", degraded=True, error=str(exc),
                          fallback_note=_FALLBACK_HINT)
    except Exception as exc:  # noqa: BLE001 - 采集异常一律降级，不冒泡打断整条链路
        logger.warning("采集工具异常：%s", exc)
        return ToolResult(ok=False, tool="case_crawl", degraded=True,
                          error=f"{type(exc).__name__}: {exc}",
                          fallback_note=_FALLBACK_HINT)

    status = result.get("status")
    if status == JOB_PAUSED:
        # 转人工不是失败：重试多少次都一样，要的是有头模式 + 人来过验证码。
        # 混进"失败"里会让上层反复重试同一个必然失败的页面。
        return ToolResult(ok=False, tool="case_crawl", degraded=True,
                          error=result.get("error") or "命中验证码，已转人工",
                          fallback_note="该来源需要人工过验证码后才能继续采集；"
                                        "已有结果不受影响，可先用本地类案库")
    if status == JOB_FAILED:
        return ToolResult(ok=False, tool="case_crawl", degraded=True,
                          error=result.get("error") or "采集失败",
                          fallback_note=_FALLBACK_HINT)

    return ToolResult(ok=True, tool="case_crawl", data=result)
