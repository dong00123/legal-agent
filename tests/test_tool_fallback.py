"""工具降级通道的**返回形状**回归。

缺陷（静默、且只在"主通道超时"时才现形）：``ToolExecutor.run`` 的降级分支写了

    return ToolResult(ok=True, tool=spec.name, data=fb, degraded=True, …)

但 ``_fb_statute_search`` 这些降级通道**自己就是返回 ToolResult 的**。再包一层以后
`res.data` 成了一整个 ToolResult，调用方 `(res.data or {}).get("statutes")`
（``statute_node.py:65``）直接抛

    AttributeError: 'ToolResult' object has no attribute 'get'

StatuteNode 判失败（continue），证据池里一条法条都没有 —— 交付物看起来就像
"这次没检索到法条"，而真实原因是降级路径把数据装错了层。同一条错误在
``case_node.py:84`` 原样重演。

真机出现记录：2026-09-29 一条、2026-10-07 五条（StatuteNode ×3、CaseNode ×2），
**全部集中在主通道 30s 超时走降级的那几次**。平时降级通道不触发，所以长期没被发现。

成功路径本来就有这个判断（``isinstance(result, ToolResult)``），降级路径漏了。
本文件钉住"两条路形状一致"。
"""

from __future__ import annotations

import asyncio

from backend.tools.base import ToolExecutor, ToolResult, ToolSpec


def _run(spec: ToolSpec) -> ToolResult:
    return asyncio.run(ToolExecutor().run(spec, task_id="t-fallback"))


def _failing(name: str = "statute_search") -> ToolSpec:
    async def _boom(**kwargs):  # noqa: ANN003, ANN202
        raise RuntimeError("主通道炸了")

    return ToolSpec(name=name, category="statute", description="d", handler=_boom,
                    timeout=1, max_retry=0)


def test_fallback_returning_toolresult_is_not_nested() -> None:
    """降级通道返回 ToolResult ⇒ ``data`` 必须是**它自己的 data**，不是它本身。"""
    spec = _failing()
    spec.fallback = lambda **kw: ToolResult(
        ok=True, tool="statute_search",
        data={"count": 1, "statutes": [{"doc_name": "劳动合同法", "article_no": "第四十七条"}]},
        degraded=True, fallback_note="向量检索不可用，法条结果由纯 BM25 通道返回，召回率可能下降",
    )
    res = _run(spec)
    assert res.ok and res.degraded
    # 这就是调用方那行代码（statute_node.py:65），它过去会 AttributeError
    assert (res.data or {}).get("statutes"), f"data 被装成了 {type(res.data).__name__}"
    assert not isinstance(res.data, ToolResult), "降级结果被包进了 data 里"
    # 降级通道自己写的说明比笼统的"已走降级路径"更准，不能被覆盖掉
    assert "纯 BM25" in res.fallback_note
    assert res.elapsed_ms >= 0 and res.attempts == 1


def test_fallback_returning_toolresult_keeps_its_failure_honest() -> None:
    """降级通道**自己也失败**（ok=False）时不许被改写成 ok=True。"""
    spec = _failing("case_search")
    spec.fallback = lambda **kw: ToolResult(
        ok=False, tool="case_search", error="BM25 降级通道亦失败：索引不存在",
        degraded=True, fallback_note="本次类案检索不可用，分析意见仅基于法规检索结果",
    )
    res = _run(spec)
    assert res.ok is False, "降级通道都失败了，还被报成成功"
    assert "BM25" in res.error or "失败" in res.error


def test_plain_dict_fallback_still_wrapped() -> None:
    """降级通道返回**普通字典**（老写法）时，仍然照旧包成 data。"""
    spec = _failing("format_normalize")
    spec.fallback = lambda **kw: {"doc_type": "complaint", "normalized": kw.get("text", "")}
    res = _run(spec)
    assert res.ok and res.degraded
    assert isinstance(res.data, dict) and res.data["doc_type"] == "complaint"
    assert "已走降级路径" in res.fallback_note
