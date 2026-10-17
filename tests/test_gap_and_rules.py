"""证据缺口去重 与 风险规则依据 回归测试。

覆盖的缺陷（同样都是静默失效）：

1. **同一件事在缺口清单里出现三遍**。``add_evidence_gap`` 原先按 ``kind|reason``
   去重，而同一个事实由不同节点登记时 kind 不同（``case`` / ``analysis``），
   措辞也各写各的，于是谁也去不掉谁。律师看到三条说的是同一件事，
   会认为系统在凑数 —— 一旦对缺口清单失去信任，真正要紧的那条也就没人看了。

2. **风险规则声明了依据，或压根没声明**。``no_confidentiality`` /
   ``no_service_address`` / ``wage_delay_no_interest`` 三条规则没有 ``cite``，
   命中的风险点 ``basis`` 恒为空，报告里一律写「（需补充法律依据）」。

3. **依据写错比不写更糟**。保密义务在劳动合同下依《劳动合同法》第二十三条、
   在其他合同下依《民法典》第五百零一条 —— 两条同时写上去必有一条是错的，
   而引错法会被对方律师直接拿去打。
"""

from __future__ import annotations

import pytest

from backend.agents.harness.state_manager import _gap_key, add_evidence_gap
from backend.agents.state import GAP_NO_CASES, GAP_NO_FACTS, GAP_NO_STATUTES
from backend.llm.rule_engine import (
    CONTRACT_RISK_RULES,
    required_cites,
    rules_for_contract,
)


# --------------------------------------------------------------------------- 缺口去重
def test_gap_key_ignores_kind_only() -> None:
    """去重键只取理由，不含 kind —— kind 只说明"谁先发现的"。"""
    assert _gap_key("未提取到结构化案件事实") == _gap_key("未提取到结构化案件事实。")


@pytest.mark.parametrize("a,b", [
    ("未提取到结构化案件事实", "未提取到结构化案件事实。"),
    ("法规检索未命中适用条文", "法规检索未命中适用条文；"),
    ("类案检索未获取到可用判例", GAP_NO_CASES),
])
def test_gap_key_normalizes_punctuation(a: str, b: str) -> None:
    assert _gap_key(a) == _gap_key(b)


def test_gap_key_keeps_distinct_facts_apart() -> None:
    assert _gap_key(GAP_NO_CASES) != _gap_key(GAP_NO_STATUTES) != _gap_key(GAP_NO_FACTS)


def test_same_reason_from_two_nodes_merges() -> None:
    """CaseNode 与 AnalysisNode 都登记"类案不可用"时，只应留下一条。"""
    state: dict = {}
    add_evidence_gap(state, "case", GAP_NO_CASES, "缺少裁判倾向参考")
    add_evidence_gap(state, "analysis", GAP_NO_CASES, "结论说服力下降")
    gaps = state["evidence_gap"]
    assert len(gaps) == 1
    # 保留首次登记（谁先发现）的 kind 与更具体的 impact
    assert gaps[0]["kind"] == "case"
    assert gaps[0]["impact"] == "缺少裁判倾向参考"


def test_different_reasons_both_kept() -> None:
    state: dict = {}
    add_evidence_gap(state, "analysis", GAP_NO_FACTS, "x")
    add_evidence_gap(state, "statute", GAP_NO_STATUTES, "y")
    assert len(state["evidence_gap"]) == 2


def test_blank_reason_ignored() -> None:
    """空理由去重键为空，若放行会让所有空理由缺口互相去重、只剩一条。"""
    state: dict = {}
    add_evidence_gap(state, "analysis", "   ", "x")
    add_evidence_gap(state, "analysis", "", "y")
    assert not state.get("evidence_gap")


def test_punctuation_variant_reason_merges() -> None:
    """"未提取到结构化案件事实。" 与不带句号的写法是同一条事实。"""
    state: dict = {}
    add_evidence_gap(state, "analysis", GAP_NO_FACTS, "a")
    add_evidence_gap(state, "intake", GAP_NO_FACTS + "。", "b")
    assert len(state["evidence_gap"]) == 1


# --------------------------------------------------------------------------- 规则依据
def test_every_document_scope_rule_declares_a_cite() -> None:
    """全文级规则没有条款正文可挂依据，更不能连 ``cite`` 都没有。

    ``scope="document"`` 的规则命中的风险点只能落在"全文"上，
    律师最需要的就是"这条凭什么" —— 依据为空等于让它不可用。
    """
    missing = [r.id for r in CONTRACT_RISK_RULES
               if r.scope == "document" and not r.cite and not r.cites_by_contract]
    assert not missing, f"以下规则未声明任何依据：{missing}"


@pytest.mark.parametrize("rule_id", [
    "penalty_too_high", "penalty_percent_high", "deposit_over_20",
    "lease_term_over_20y", "no_termination", "no_force_majeure",
    "no_confidentiality", "no_service_address", "jurisdiction_vague",
    "trial_period_over_6m", "noncompete_no_compensation", "labor_penalty",
])
def test_rule_declares_cite(rule_id: str) -> None:
    rule = next(r for r in CONTRACT_RISK_RULES if r.id == rule_id)
    assert rule.cites_for("劳动合同"), f"{rule_id} 未声明依据"


def test_confidentiality_cite_switches_by_contract() -> None:
    """保密义务的依据随合同类型切换，且只有一条 —— 两条并列时必有一条是错的。"""
    rule = next(r for r in CONTRACT_RISK_RULES if r.id == "no_confidentiality")
    assert rule.cites_for("劳动合同") == [("劳动合同法", "第二十三条")]
    assert rule.cites_for("房屋租赁") == [("民法典", "第五百零一条")]
    assert rule.cites_for("") == [("民法典", "第五百零一条")]


def test_labor_only_rules_not_applied_to_other_contracts() -> None:
    """劳动合同法的条文绝不能塞进房屋租赁合同的检索式。"""
    lease = required_cites("房屋租赁")
    assert all("劳动合同法" not in law for law, _ in lease)
    assert ("劳动合同法", "第二十四条") not in lease


def test_required_cites_covers_multi_cite_rule() -> None:
    """``cites`` 里的补充依据也必须进检索式，否则它永远命不中。"""
    cites = required_cites("劳动")
    assert ("民法典", "第五百八十五条") in cites      # 主依据
    assert ("民法典", "第五百八十四条") in cites      # 补充依据


def test_required_cites_dedupes() -> None:
    cites = required_cites("劳动")
    assert len(cites) == len(set(cites))


def test_rules_for_contract_includes_generic_and_specific() -> None:
    ids = {r.id for r in rules_for_contract("劳动合同")}
    assert "no_termination" in ids            # 通用规则
    assert "trial_period_over_6m" in ids      # 劳动合同专属
    lease_ids = {r.id for r in rules_for_contract("房屋租赁")}
    assert "trial_period_over_6m" not in lease_ids
