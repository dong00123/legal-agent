"""报告拼装的回归测试：**交付物里不许套第二份报告**。

2026-10-07 真机现象（律师截图）：一份「法律辅助分析报告（法律咨询）」的
「二、分析说明」里，整段套着另一份「法律辅助分析意见（consult）」——
标题、结论摘要、引用清单各来一遍。

根因在降级路径上：模型的这次调用掉回规则引擎时，``rule_engine._h_report``
返回的是**一整篇报告**，而 `_build_report` 只要收到非空正文就
``_assemble(state, body=text)``，把它当成"分析说明"这一节的内容拼进去。
全库 2087 条有结果的任务里有 76 条是这种形状，其中 66 条挂在真账号下 ——
因为云端 403 是**间歇**的，不是"配错了"，所以它会在任何时候再冒出来。

这条测试盯的是**交付物的形状**，不是某一行代码：断言"标题只出现一次"，
所以无论以后是 `_llm_report` 还是 `_assemble` 兜住的，只要不再套娃就绿。
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

from backend.agents.harness import Harness
from backend.agents.state import new_state

# ⚠ 必须按**模块**取，不能 ``from backend.agents.nodes import report_node``：
# 那个名字在包 ``__init__`` 里被同名函数遮住了（``"ReportNode": report_node``），
# 拿到的是节点函数，``report_node._assemble`` 会 AttributeError。
report_mod = importlib.import_module("backend.agents.nodes.report_node")

# 规则引擎降级时交出来的那篇（``rule_engine._h_report`` 的形状，截取开头）
RULE_REPORT = """# 法律辅助分析意见（consult）

## 一、结论摘要
- 共检索到 11 条相关法条，其中标注为现行有效的 11 条。
- 共检索到 6 个类案，可作裁判倾向参照。

## 二、引用清单
### 法条
- 《中华人民共和国劳动合同法》第八十七条（现行有效）
"""


def _ready(state_intent: str = "consult") -> tuple[dict, Harness]:
    state = new_state("我2019年3月入职，2023年8月被辞退，公司说我是主动辞职")
    state["intent"] = state_intent          # type: ignore[typeddict-item]
    state["statutes"] = [{"doc_name": "中华人民共和国劳动合同法", "article_no": "第八十七条",
                          "status": "现行有效", "content": "违法解除应当支付赔偿金。"}]
    state["cases"] = [{"case_no": "（2020）京01民终1号", "case_name": "某公司与张某劳动争议",
                       "gist": "违法解除应支付赔偿金。"}]
    harness = Harness.start(state, client="test", username="pytest")
    return state, harness


@pytest.fixture
def fake_chat(monkeypatch: pytest.MonkeyPatch):
    """替换 ``llm_client.chat``；同时让 ``_llm_available()`` 恒为真。

    两者要一起换：只换 `chat` 的话，`_llm_available()` 在没探测过 provider 的
    测试进程里可能给 False，``_llm_report`` 根本不会被调用 ——
    用例会变成一条"什么都没测到"的绿灯。
    """
    from backend.llm.client import LLMResult

    box: dict = {"result": LLMResult(text="", provider="openai", model="fake")}

    async def _fake_chat(messages, **kwargs):  # noqa: ANN001, ANN003
        return box["result"]

    async def _available() -> bool:
        return True

    monkeypatch.setattr(report_mod.llm_client, "chat", _fake_chat)
    monkeypatch.setattr(report_mod, "_llm_available", _available)
    return box


def _build(state: dict, harness: Harness) -> str:
    return asyncio.run(report_mod._build_report(state, harness))


# --------------------------------------------------------------------------- 降级
def test_degraded_report_is_not_nested(fake_chat) -> None:
    """降级时**只出一份报告**：规则引擎那篇不再被当成"分析说明"。"""
    from backend.llm.client import LLMResult

    state, harness = _ready()
    fake_chat["result"] = LLMResult(text=RULE_REPORT, provider="rule",
                                    model="rule-based-legal-engine", degraded=True)
    try:
        report = _build(state, harness)
    finally:
        harness.finish()

    assert report.count("# 法律辅助分析报告") == 1, report[:400]
    assert "法律辅助分析意见" not in report, "规则引擎那篇整份报告被套进来了"
    assert "## 二、分析说明" not in report, "降级正文不该占「分析说明」这一节"
    # 降级**不等于什么都不给**：代码拼的那几节照旧在
    assert "一、结论摘要" in report and "引用清单" in report


def test_degraded_run_tells_the_lawyer_in_the_trace(fake_chat) -> None:
    """降级要留痕：报告是规则引擎汇总的，这件事不能只体现在正文变短上。"""
    from backend.llm.client import LLMResult

    state, harness = _ready()
    fake_chat["result"] = LLMResult(text=RULE_REPORT, provider="rule",
                                    model="rule-based-legal-engine", degraded=True)
    try:
        _build(state, harness)
    finally:
        harness.finish()
    notes = " ".join(str(e.get("detail") or "") for e in harness.tracer.to_list())
    assert "规则引擎" in notes, notes[:300]


# --------------------------------------------------------------------------- 正常
def test_normal_body_still_lands_in_the_analysis_section(fake_chat) -> None:
    """正常模型正文**照旧**进「分析说明」——别把上面那条修成"永远不拼正文"。"""
    from backend.llm.client import LLMResult

    state, harness = _ready()
    fake_chat["result"] = LLMResult(text="围绕违法解除的赔偿金，可分三步主张……",
                                    provider="openai", model="fake")
    try:
        report = _build(state, harness)
    finally:
        harness.finish()
    assert "## 二、分析说明" in report, report[:500]
    assert "围绕违法解除的赔偿金" in report
    assert report.count("# 法律辅助分析报告") == 1


def test_whole_report_body_is_never_wrapped() -> None:
    """兜底那一层：正文本身是一整篇报告（模型也可能整篇回吐）时，不套第二层。"""
    state, _ = _ready()
    out = report_mod._assemble(state, body=RULE_REPORT)
    assert out.count("# 法律辅助分析报告") == 1
    assert "法律辅助分析意见" not in out


# --------------------------------------------------------------------------- 证据不足（反幻觉）
def test_report_empty_evidence_skips_llm_and_records_gap(fake_chat) -> None:
    """报告形态：证据池为空时**不调用 LLM**，复用降级分支并如实登记证据缺口。

    与对话形态同一道闸（``_llm_report`` / ``_llm_conversational`` 各自入口判定）。
    """
    state, harness = _ready()
    state["statutes"] = []
    state["cases"] = []
    state["knowledges"] = []
    try:
        report = _build(state, harness)
    finally:
        harness.finish()
    assert "messages" not in fake_chat, "证据池为空时不该调用模型"
    assert "## 二、分析说明" not in report, report[:400]
    assert "证据缺口与不确定性" in report, report[:400]
    gaps = state.get("evidence_gap") or []
    assert any(g.get("kind") == "analysis" and "未检索到相关法规" in (g.get("reason") or "")
               for g in gaps), gaps
