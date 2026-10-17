"""``damages_calc`` 的回归测试：**折算规则必须与文书侧同源、"算不了"不许代 0**。

两件事在这个文件里被钉死：

1. **年限折算**。同一条规则在 ``template_service._PROMPT`` 规则 8 里给文书填数用，
   在这里给报告表格用。**两份实现各算各的 → 同一个案子在文书里和报告里
   折出不同的月数**，而两处都"看起来正常"。所以基准用例直接用文书侧
   原文给的那个例子（1 年零 15 天 → 1.5 个月）。
2. **缺事实时的返回值**。关键事实缺失一律 ``ok=False`` + ``missing`` + ``total=None``。
   一个看起来确定的假数字（代 0、或拿最低工资猜）比一个诚实的缺口危险得多：
   它会被律师直接抄进仲裁请求。

真机上栽过的那次（请求事项写 54000 元、正文算式却是 9000 × 4.5 × 2 = 81000 元）
就是"数字由模型各写各的"的直接后果 —— 这个工具存在的理由就是那件事。
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from backend.tools.calc_tools import compensated_months, damages_calc


def _calc(**kw):
    return asyncio.run(damages_calc(**kw))


# --------------------------------------------------------------------------- 折算
@pytest.mark.parametrize("start, end, want, note", [
    (date(2019, 3, 1), date(2020, 3, 1), 1.0, "正好 1 年"),
    (date(2019, 3, 1), date(2020, 3, 16), 1.5, "1 年零 15 天（文书侧例子）"),
    (date(2019, 3, 1), date(2020, 8, 1), 1.5, "1 年 5 个月：余下不满 6 个月 → 0.5"),
    (date(2019, 3, 1), date(2020, 9, 1), 2.0, "1 年 6 个月：余下满 6 个月 → 1"),
    (date(2019, 3, 1), date(2023, 8, 1), 4.5, "4 年 5 个月 → 4.5（报告截图里的那组）"),
    (date(2019, 3, 1), date(2023, 3, 1), 4.0, "正好 4 年：零头为 0，不加 0.5"),
])
def test_compensated_months_matches_the_letter_side_rule(start, end, want, note) -> None:
    assert compensated_months(start, end)[3] == want, note


def test_half_day_boundary_is_not_floored() -> None:
    """零头哪怕只有 1 天也算 0.5 个月 —— 抹掉零头是**少算**，方向恰好对律师不利。"""
    assert compensated_months(date(2019, 3, 1), date(2020, 3, 2))[3] == 1.5


def test_rule_text_in_template_service_still_says_the_same_thing() -> None:
    """同源校验：文书侧的提示词里，那条规则的关键数字必须还在。

    这条不查代码查**措辞**，是故意的 —— 折算规则改名的代价是两份实现悄悄分叉，
    而分叉不会报错：文书里 4.5 个月、报告里 5 个月，两份东西都交得出去。
    """
    from backend.app.services import template_service

    rule = template_service._PROMPT
    assert "每满 1 年 = 1 个月" in rule
    assert "0.5 个月" in rule and "1.5 个月" in rule


# --------------------------------------------------------------------------- 算不了
def test_missing_wage_returns_a_gap_not_a_zero() -> None:
    """缺月工资 ⇒ ``ok=False`` + ``total is None``。**绝不能是 0**。"""
    res = _calc(scenario="illegal_termination",
                start_date="2019-03-01", end_date="2023-08-01")
    assert res.ok is False
    assert res.data["total"] is None, "缺事实时给了数（哪怕是 0）会被直接抄进仲裁请求"
    assert res.data["missing"] == ["monthly_wage"]


def test_all_missing_facts_are_reported_at_once() -> None:
    """缺什么一次报全 —— 逐个报会逼调用方补一个重跑一次。"""
    res = _calc(scenario="illegal_termination")
    assert res.ok is False and res.data["total"] is None
    assert set(res.data["missing"]) == {"monthly_wage", "start_date/end_date 或 years"}


def test_reversed_span_is_reported_as_a_contradiction() -> None:
    """离职日期早于入职日期：报矛盾，**不要**静默当成别的口径算出一个数。"""
    res = _calc(scenario="economic_compensation", monthly_wage=8500,
                start_date="2023-08-01", end_date="2019-03-01")
    assert res.ok is False and res.data["total"] is None
    assert "颠倒" in "".join(res.data["missing"])


def test_missing_dates_and_years_returns_a_gap() -> None:
    res = _calc(scenario="economic_compensation", monthly_wage=8500)
    assert res.ok is False and res.data["total"] is None
    assert "start_date/end_date 或 years" in res.data["missing"]


def test_unknown_scenario_is_refused() -> None:
    res = _calc(scenario="whatever", monthly_wage=8500, years=3)
    assert res.ok is False and res.data["total"] is None


def test_zero_or_negative_wage_is_refused() -> None:
    for wage in (0, -100):
        res = _calc(scenario="economic_compensation", monthly_wage=wage, years=3)
        assert res.ok is False and res.data["total"] is None, wage


# --------------------------------------------------------------------------- 场景
def test_two_n_is_exactly_twice_n() -> None:
    n = _calc(scenario="economic_compensation", monthly_wage=8500, years=4.5)
    two = _calc(scenario="illegal_termination", monthly_wage=8500, years=4.5)
    assert n.data["total"] == 38250.0
    assert two.data["total"] == 76500.0 == n.data["total"] * 2


def test_bundle_rows_are_alternatives_not_a_sum() -> None:
    """三行互为备选：``total`` 取主请求（2N），**不是三者相加**。

    标成 ``sum`` 的后果是报告里出现一个把 2N、N、代通知金加起来的数 ——
    那是三倍的钱，且法条依据看起来一样合法。
    """
    res = _calc(scenario="termination_bundle", monthly_wage=8500, years=4.5)
    assert res.data["total_kind"] == "primary"
    assert res.data["total"] == 76500.0
    labels = [i["label"] for i in res.data["items"]]
    assert any("2N" in x for x in labels) and any("代通知金" in x for x in labels)
    assert res.data["total"] != sum(i["value"] for i in res.data["items"])


def test_cap_is_applied_only_when_the_cap_line_is_given() -> None:
    """三倍封顶：给了线才判；没给线要在 ``assumptions`` 里说清"没判"。"""
    capped = _calc(scenario="economic_compensation", monthly_wage=30000,
                   years=15, cap_monthly=20000)
    assert capped.data["capped"] is True
    assert capped.data["total"] == 20000.0 * 12, "超线后应同时按 12 年上限计"
    assert any("封顶" in a for a in capped.data["assumptions"])

    uncapped = _calc(scenario="economic_compensation", monthly_wage=30000, years=15)
    assert uncapped.data["capped"] is False
    assert any("未提供" in a for a in uncapped.data["assumptions"])


def test_overtime_rates_are_fixed_by_code() -> None:
    """倍数由代码固定：1.5 / 2 / 3。调用方传不进来（也不该传进来）。"""
    res = _calc(scenario="overtime", overtime={"hourly_base": 100,
                                               "weekday_hours": 10,
                                               "restday_hours": 10,
                                               "holiday_hours": 10})
    assert res.ok and res.data["total"] == 100 * (1.5 + 2 + 3) * 10
    assert res.data["total_kind"] == "sum"


def test_overtime_without_hours_is_a_gap() -> None:
    res = _calc(scenario="overtime", monthly_wage=8500, overtime={"weekday_hours": 0})
    assert res.ok is False and res.data["total"] is None


def test_overtime_derives_hourly_from_monthly_and_says_so() -> None:
    """只给月工资时按 21.75 天 × 8 小时折算，**并把口径写出来**（律师要能复核）。"""
    res = _calc(scenario="overtime", monthly_wage=8700, overtime={"weekday_hours": 8})
    assert abs(res.data["items"][1]["value"] - (8700 / 21.75 / 8 * 1.5 * 8)) < 0.01
    assert any("21.75" in a for a in res.data["assumptions"])


# --------------------------------------------------------------------------- 注册
def test_tool_is_registered_and_granted_to_exactly_two_nodes() -> None:
    """**三张表必须同步**：注册表、``NODE_TOOLS``、``NODE_SPECS``。

    缺一处就有一处显示的是假的能力：工具在注册表里但没授给节点 ⇒
    Harness 的越权检查直接拦下（``route_denied``），而节点代码照旧写在那儿，
    读代码的人以为它能算。反过来（授了但没注册）更糟：直接 KeyError。
    """
    from backend.agents.harness.planner import NODE_TOOLS
    from backend.agents.harness.router import NODE_SPECS, can_call, tools_for
    from backend.tools import registry as R

    assert R.has_tool("damages_calc")
    assert R.get_tool("damages_calc").fallback is None, "确定性计算没有第二条路，不该配 fallback"
    assert "calc" in R.CATEGORIES

    by_node = {s.node: name for name, s in NODE_SPECS.items()}
    for node in ("AnalysisNode", "ReportNode"):
        assert "damages_calc" in NODE_TOOLS[node], node
        assert "damages_calc" in tools_for(by_node[node]), node
    # 只授给这两个：进了只读通配符的话，ReviewNode/DraftNode 也能自己算金额
    for name in ("review", "draft", "validator", "case", "statute", "knowledge"):
        assert can_call(name, "damages_calc") is False, name
