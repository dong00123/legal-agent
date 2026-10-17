"""
对话正文的**五节结构兜底**测试。

背景：2026-10-13 助手页（``/agents/assistant/stream``）实测，模型无视
``ASSISTANT_REPORT_PROMPT`` 里"必须五节、禁止自拟小节"的描述性约束，
自行编出「行政违法 / 刑事犯罪 / 刑事免责 / 案件移送」等分类小节，
框内仍是量刑档次罗列。提示词压不住，于是由代码在生成后校验并改写一次。

这个文件钉住三类行为：

1. **合规不打扰**。五节齐全、顺序正确的正文原样放行，不再白调一次 LLM。
2. **自拟标题必被拦**。模型编的分类小节一个都匹配不上五节，触发改写；
   改写稿合规时采用改写稿。
3. **改写失败不丢内容**。改写仍不合规 / 异常时保留原稿（至少不丢内容），
   并在 Trace 里留证据。
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

from backend.agents.harness import Harness
from backend.agents.state import new_state

report_mod = importlib.import_module("backend.agents.nodes.report_node")

QUERY = "公司违法解除，月均 8500，能拿多少"
COMPLIANT = (
    "（一）核心结论\n"
    "公司违法解除，你可主张赔偿金。\n\n"
    "（二）具体分析\n"
    "按工作年限与经济补偿标准计算。\n\n"
    "（三）通俗化输出\n"
    "就是公司要按 2N 赔你。\n\n"
    "（四）额外思考\n"
    "还要注意证据保留与时效。\n\n"
    "（五）行动建议\n"
    "先固定证据，再发函主张。\n"
)
SELF_MADE = (
    "你担心被补缴和罚款，先看分类判断。\n\n"
    "行政违法\n"
    "- 未申报的行政处罚风险。\n\n"
    "刑事犯罪\n"
    "- 逃税罪的构成要件。\n\n"
    "刑事免责\n"
    "- 什么情形下不追责。\n\n"
    "案件移送\n"
    "- 什么情况移送公安。\n"
)


def _ready(style: str = "conversational") -> tuple[dict, Harness]:
    state = new_state(QUERY, answer_style=style)
    state["intent"] = "consult"          # type: ignore[typeddict-item]
    state["statutes"] = [
        {"doc_name": "劳动合同法", "article_no": "第1条", "status": "现行有效",
         "text": "第1条的正文。"}
    ]
    state["knowledges"] = []
    harness = Harness.start(state, client="test", username="pytest")
    return state, harness


@pytest.fixture
def fake_chat(monkeypatch: pytest.MonkeyPatch):
    """替换模型调用：记录调用次数与消息，返回 box 里指定的文本。"""
    from backend.llm.client import LLMResult

    box: dict = {"text": "", "calls": 0, "messages": None}

    async def _fake_chat(messages, **kwargs):  # noqa: ANN001, ANN003
        box["calls"] += 1
        box["messages"] = messages
        return LLMResult(text=box["text"], provider="openai", model="fake")

    async def _available() -> bool:
        return True

    monkeypatch.setattr(report_mod.llm_client, "chat", _fake_chat)
    monkeypatch.setattr(report_mod, "_llm_available", _available)
    return box


def _ensure(body: str, box: dict) -> str:
    state, harness = _ready()
    return asyncio.run(report_mod._ensure_conversational_structure(
        state, harness, body))


# --------------------------------------------------------------------------- 判定
def test_compliant_five_sections_pass() -> None:
    assert report_mod._conversational_compliant(COMPLIANT)


def test_self_made_classifier_headings_fail() -> None:
    """模型自拟的「行政违法 / 刑事犯罪…」小节一个都匹配不上五节 ⇒ 不合规。"""
    assert not report_mod._conversational_compliant(SELF_MADE)


def test_missing_section_fails() -> None:
    assert not report_mod._conversational_compliant(
        COMPLIANT.replace("（四）额外思考\n还要注意证据保留与时效。\n\n", ""))


def test_wrong_order_fails() -> None:
    body = COMPLIANT.replace(
        "（三）通俗化输出\n就是公司要按 2N 赔你。\n\n",
        "",
    ).replace(
        "（五）行动建议\n先固定证据，再发函主张。\n",
        "（三）行动建议\n先固定证据，再发函主张。\n\n"
        "（五）通俗化输出\n就是公司要按 2N 赔你。\n",
    )
    assert not report_mod._conversational_compliant(body)


def test_markdown_variants_are_accepted() -> None:
    """标题带 ## / 加粗 / 数字编号也认 —— 别为写格式把合规稿误判成违规。"""
    body = (
        "## 一、核心结论\n\n"
        "**（二）具体分析**\n\n"
        "### 3. 通俗化输出：\n\n"
        "#### （四）额外思考\n\n"
        "五、行动建议\n"
    )
    assert report_mod._conversational_compliant(body)


# --------------------------------------------------------------------------- 兜底
def test_compliant_body_skips_rewrite(fake_chat) -> None:
    """合规正文原样返回，不再白调一次 LLM。"""
    assert _ensure(COMPLIANT, fake_chat) == COMPLIANT
    assert fake_chat["calls"] == 0


def test_self_made_headings_are_rewritten_once(fake_chat) -> None:
    """自拟标题触发改写；改写稿合规 ⇒ 采用改写稿，且上一版输出带进消息。"""
    fake_chat["text"] = COMPLIANT
    assert _ensure(SELF_MADE, fake_chat) == COMPLIANT.strip()
    assert fake_chat["calls"] == 1
    # 改写消息里带着上一版输出，模型才能照着改写而不是重新编
    joined = "\n".join(m.get("content", "") for m in fake_chat["messages"])
    assert "行政违法" in joined and "刑事免责" in joined


def test_rewrite_failure_keeps_original(fake_chat) -> None:
    """改写仍不合规 ⇒ 保留原稿，不丢内容。"""
    fake_chat["text"] = "还是没按五节写，只有一段话。"
    assert _ensure(SELF_MADE, fake_chat) == SELF_MADE
    assert fake_chat["calls"] == 1


def test_rewrite_error_keeps_original() -> None:
    """改写调用抛异常 ⇒ 保留原稿。"""
    async def _boom(messages, **kwargs):  # noqa: ANN001, ANN003
        raise RuntimeError("gateway down")

    orig = report_mod.llm_client.chat
    report_mod.llm_client.chat = _boom  # type: ignore[method-assign]
    try:
        state, harness = _ready()
        out = asyncio.run(report_mod._ensure_conversational_structure(
            state, harness, SELF_MADE))
        assert out == SELF_MADE
    finally:
        report_mod.llm_client.chat = orig  # type: ignore[method-assign]
