"""IntentRouter 的回归测试。

背景（真机踩过）：长段案情描述往往**只命中一个意图**（consult），
但分数低于模糊阈值时仍会进入"改由 LLM 判定"分支 ——
该分支的 notes 曾直接取 ``ranked[1]``，一票候选时 IndexError，
IntentNode 整个失败，流程退回"自动判定"（置信度 0.15、零引用）。
"""

from __future__ import annotations

import pytest

from backend.agents.router import IntentRouter

LONG_LABOR_DISPUTE = (
    "我2019年3月入职，2023年8月被辞退，公司说我不胜任工作，但从来没给我培训。"
    "每周一到周五加班到晚上9点，周六全天加班，月薪从6000涨到8500，"
    "公司现在经营困难，只给2万补偿，请问我该要多少？"
)


@pytest.mark.asyncio
async def test_single_hit_weak_score_does_not_crash():
    """只有一个意图候选 + 弱分数 ⇒ 必须正常返回，绝不能 IndexError。"""
    res = await IntentRouter().classify(LONG_LABOR_DISPUTE)
    assert res.intent in ("consult", "case_analysis", "quick_answer")
    assert res.decided_by in ("rule", "rule-ambiguous", "llm", "default")


@pytest.mark.asyncio
async def test_off_topic_still_direct():
    res = await IntentRouter().classify("你好")
    assert res.intent == "off_topic"
