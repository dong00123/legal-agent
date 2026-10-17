"""双模型交叉复核（合同审查）：初审广撒网 → 第二模型复审严核查。

覆盖四条纪律：
1. 复审把初审风险点逐条判成 confirmed / downgraded / removed，补漏报；
2. 复审模型没配 / 调用失败时**原样返回初审清单**，绝不丢结论；
3. 分歧持久化 + 回灌提示词（自我改进）；
4. 人工抽查抽样标记。
"""

from __future__ import annotations

import asyncio
import importlib
import json
from typing import Any

import pytest

from backend.agents.harness import Harness
from backend.app.config import settings

# ⚠ 必须按**模块**取，不能 ``from backend.agents.nodes import review_node``：
# 那个名字在包 ``__init__`` 里被同名节点函数遮住了，拿到的是函数不是模块。
review_node = importlib.import_module("backend.agents.nodes.review_node")


def _clauses(n: int = 3) -> list[dict[str, Any]]:
    return [{"clause_no": f"第{i}条", "clause_title": "标题",
             "clause_text": f"第{i}条原文", "clause_index": i} for i in range(1, n + 1)]


def _state() -> dict[str, Any]:
    return {
        "task_id": "task_second_pass", "query": "帮我审一下这份合同",
        "intent": "contract_review", "doc_type": "contract",
        "material_text": "合同正文", "clauses": _clauses(),
        "statutes": [], "cases": [], "knowledges": [],
    }


def _harness(state: dict[str, Any]) -> Harness:
    return Harness.start(state, client="test", username="pytest")


class _FakeProvider:
    """复审模型桩：``chat`` 返回一段 JSON 文本，``_second_pass`` 会用 extract_json 解析。"""

    model = "fake-review-model"

    def __init__(self, json_text: str) -> None:
        self._text = json_text
        self.calls = 0

    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1

        class _Res:
            text = self._text

        return _Res()


def _patch_provider(monkeypatch: pytest.MonkeyPatch, json_text: str) -> _FakeProvider:
    from backend.llm import client as llm_client_mod

    provider = _FakeProvider(json_text)
    monkeypatch.setattr(llm_client_mod, "review_provider", lambda: provider)
    return provider


# --------------------------------------------------------------------------- 纯函数
def test_downgrade_walks_one_step_and_stops_at_low() -> None:
    assert review_node._downgrade("high") == "medium"
    assert review_node._downgrade("medium") == "low"
    assert review_node._downgrade("low") == "low"
    assert review_node._downgrade("garbage") == "low"   # 未知等级按 medium 降一档


def test_review_provider_returns_none_when_disabled(monkeypatch) -> None:
    from backend.llm import client as llm_client_mod

    monkeypatch.setattr(settings, "LLM_REVIEW_ENABLED", False)
    assert llm_client_mod.review_provider() is None

    monkeypatch.setattr(settings, "LLM_REVIEW_ENABLED", True)
    monkeypatch.setattr(settings, "LLM_REVIEW_BASE_URL", "")
    assert llm_client_mod.review_provider() is None


# --------------------------------------------------------------------------- 复审行为
def test_second_pass_applies_decisions(monkeypatch) -> None:
    state = _state()
    harness = _harness(state)
    risk_points = [
        {"clause_no": "第1条", "risk": "违约金过高", "level": "high", "basis": []},
        {"clause_no": "第2条", "risk": "管辖约定不明", "level": "high", "basis": []},
        {"clause_no": "第3条", "risk": "误报：其实不是风险", "level": "medium", "basis": []},
    ]
    _patch_provider(monkeypatch, json.dumps({
        "decisions": [
            {"no": 1, "action": "confirmed"},
            {"no": 2, "action": "downgraded", "to_level": "low", "reason": "管辖不必然无效"},
            {"no": 3, "action": "removed", "reason": "并非风险"},
        ],
        "additions": [{"clause_no": "第4条", "clause_title": "违约责任",
                       "risk": "缺争议解决方式", "level": "medium", "suggestion": "补", "basis": []}],
    }, ensure_ascii=False))
    monkeypatch.setattr(settings, "REVIEW_SECOND_PASS_STATE", "")

    try:
        out = asyncio.run(review_node._second_pass(
            state, harness, "合同正文", _clauses(), "劳动合同", "乙方", risk_points))
    finally:
        harness.finish()

    # 第3条被删、补漏1条 → 2 条初审存活 + 1 条新增 = 3 条
    assert len(out) == 3
    assert all("第3条" not in (r.get("clause_no") or "") for r in out), "被删的误报不该还在"
    by_no = {(r.get("clause_no") or ""): r for r in out}
    assert by_no["第1条"]["review"] == "confirmed"
    assert by_no["第2条"]["level"] == "low"
    assert by_no["第2条"]["review"] == "downgraded"
    assert by_no["第2条"]["review_note"] == "管辖不必然无效"
    assert by_no["第4条"]["review"] == "added"


def test_second_pass_returns_initial_when_provider_missing(monkeypatch) -> None:
    """复审模型没配：原样返回初审清单，并如实登记缺口 —— 绝不丢结论。"""
    from backend.llm import client as llm_client_mod

    monkeypatch.setattr(llm_client_mod, "review_provider", lambda: None)
    state = _state()
    harness = _harness(state)
    risk_points = [{"clause_no": "第1条", "risk": "r", "level": "high", "basis": []}]

    try:
        out = asyncio.run(review_node._second_pass(
            state, harness, "合同正文", _clauses(), "劳动合同", "乙方", risk_points))
    finally:
        harness.finish()

    assert out == risk_points
    assert any("复审模型未配置" in str(g) for g in (state.get("evidence_gap") or []))


# --------------------------------------------------------------------------- 自我改进
def test_second_pass_feedback_roundtrip(tmp_path, monkeypatch) -> None:
    """分歧落盘后，复审提示词能从文件里读回统计与样本（回灌）。"""
    monkeypatch.setattr(settings, "REVIEW_SECOND_PASS_STATE", str(tmp_path / "sp.json"))

    assert review_node._second_pass_feedback() == ""   # 还没有历史

    review_node._record_second_pass(
        {"confirmed": 5, "downgraded": 2, "removed": 1, "added": 1},
        [{"risk": "误报风险", "level": "medium", "action": "removed", "reason": "并非风险"}],
    )

    fb = review_node._second_pass_feedback()
    assert "累计复审 1 轮" in fb
    assert "removed 1" in fb
    assert "误报风险" in fb


# --------------------------------------------------------------------------- 人工抽查
def test_maybe_spot_check_respects_rate(monkeypatch) -> None:
    state = _state()
    harness = _harness(state)
    try:
        monkeypatch.setattr(settings, "REVIEW_SPOT_CHECK_RATE", 0.0)
        review_node._maybe_spot_check(state, harness)
        assert not state.get("spot_check")

        monkeypatch.setattr(settings, "REVIEW_SPOT_CHECK_RATE", 1.0)
        review_node._maybe_spot_check(state, harness)
        assert state["spot_check"]["needed"] is True
    finally:
        harness.finish()
