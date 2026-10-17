"""计算类 MCP Tool：法定金额测算（确定性，不做裁量）。

**为什么要有这个工具**：项目总纲是「凡是能算出来的一律由代码算，不交给模型」
（见 ``template_service._PROMPT`` 第 8 条与 `apply_amount_consistency`）。文书那边
已经栽过一次：请求事项写 `54000元`、正文算式却是 `9000 × 4.5 × 2 = 81000元`，
同一份文书两个数 —— 模型是**先填请求、后写正文**，两处各算各的。报告里的
金额测算表同理：只要数字从模型笔下来，就有两份数字打架的可能。

折算规则与文书侧**逐字同源**（``template_service._PROMPT`` 规则 8）：
每满 1 年 = 1 个月；余下满 6 个月不满 1 年 = 1 个月；余下不满 6 个月 = 0.5 个月。
例：工作 1 年零 15 天 → 1 + 0.5 = **1.5 个月**。两侧必须同源，否则同一个案子
在文书里和报告里会折出不同的月数。

**加班费倍数由代码固定**（工作日 1.5 / 休息日 2 / 法定节假日 3），不接受调用方传 ——
倍数本身就是法律结论，开放成参数等于把最容易错的一项交出去。

**"算不了"是返回值，不是抛异常**：关键事实缺失时返回 ``ok=False`` + ``missing``，
``total`` 为 ``None``。**绝不代 0、绝不拿最低工资猜** —— 一个看起来确定的数字
比一个诚实的缺口危险得多（与 ``registry`` 开篇的降级原则同源）。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from backend.tools.base import ToolResult

logger = logging.getLogger("legal_agent.tools.calc")

# 月计薪天数：劳社部发〔2008〕3 号。日工资 = 月工资 / 21.75；小时工资 = 日工资 / 8。
_MONTHLY_PAY_DAYS = 21.75
_DAILY_HOURS = 8.0

# 加班费倍数：法定，不接受调用方覆盖
_OT_RATES: dict[str, float] = {"weekday": 1.5, "restday": 2.0, "holiday": 3.0}

_OT_LABELS: dict[str, str] = {
    "weekday": "工作日加班",
    "restday": "休息日加班",
    "holiday": "法定节假日加班",
}

# 违法解除赔偿金封顶年限：《劳动合同法》第 47 条（月工资高于当地上年度职工月
# 平均工资三倍的，按三倍支付，且年限最高不超过 12 年）
_CAP_YEARS = 12.0

SCENARIOS: tuple[str, ...] = (
    "illegal_termination",      # 违法解除赔偿金 2N
    "economic_compensation",    # 经济补偿 N
    "notice_pay",               # 代通知金（额外支付一个月工资）
    "overtime",                 # 加班费
    "termination_bundle",       # 解除类三行一起算（2N / N / 代通知金），互为备选
)


# --------------------------------------------------------------------------- 折算
def compensated_months(start: date, end: date) -> tuple[int, int, int, float]:
    """工作年限折算。返回 ``(整年, 余月, 余天, 折算月数)``。

    折算月数 = 整年 + 零头：
    - 零头为 0（正好整年）→ +0；
    - 零头不满 6 个月（含"整月数不足 6"与"有零天"两种）→ +0.5；
    - 零头满 6 个月不满 1 年 → +1。

    **为什么按日历月判"满 6 个月"而不是按天数**：``183 天`` 与 ``6 个自然月``
    在跨月长度不同的年份里会给出不同结论，而法条说的是"六个月"。零头天数
    单独记（``1 年零 15 天 → 1.5``，与文书侧的例子一致）。
    """
    total_months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        total_months -= 1
    years, rem_months = divmod(total_months, 12)
    boundary = _add_months(start, total_months)
    rem_days = (end - boundary).days

    if rem_months == 0 and rem_days == 0:
        extra = 0.0
    elif rem_months < 6:
        extra = 0.5
    else:
        extra = 1.0
    return years, rem_months, rem_days, float(years) + extra


def _add_months(d: date, months: int) -> date:
    """加上整数个月（用于算"零头天数"的边界）。"""
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    # 月末对齐：1 月 31 日 + 1 个月按 2 月 28/29 日算
    day = min(d.day, _days_in_month(y, m))
    return date(y, m, day)


def _days_in_month(y: int, m: int) -> int:
    if m == 12:
        return 31
    return (date(y + (m // 12), m % 12 + 1, 1) - date(y, m, 1)).days


def _parse_date(value: Any) -> date | None:
    """吃 ``YYYY-MM-DD`` / ``YYYY/M/D`` / ``YYYY年M月D日``；解析不了返回 None。"""
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for sep in ("-", "/", "."):
        if sep in text:
            parts = text.split(sep)
            break
    else:
        import re

        m = re.match(r"^(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?$", text)
        if not m:
            return None
        parts = list(m.groups())
    if len(parts) < 3:
        return None
    try:
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- 主入口
async def damages_calc(
    scenario: str,
    monthly_wage: float | None = None,
    years: float | None = None,
    start_date: str = "",
    end_date: str = "",
    cap_monthly: float | None = None,
    overtime: dict[str, Any] | None = None,
) -> ToolResult:
    """法定金额测算。参数名与 ``registry`` 里的 ``param()`` 声明必须逐字一致。"""
    name = "damages_calc"
    scenario = str(scenario or "").strip()
    if scenario not in SCENARIOS:
        return _cannot(name, scenario, ["scenario"], {
            "supported": list(SCENARIOS),
            "hint": "scenario 必须是受支持的取值之一",
        })

    assumptions: list[str] = []
    capped = False
    # 缺什么一次报全。逐个报的后果是律师补一个、重跑一次、又发现还缺一个 ——
    # 而这条链路的每一跑都要付一次检索与生成的代价。
    missing: list[str] = []

    # ---- 工作年限：优先用起止日期，其次用直接给的 years
    ym: dict[str, Any] = {}
    months: float | None = None
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if start and end and end >= start:
        y, rem_m, rem_d, months = compensated_months(start, end)
        ym = {"years": y, "months": rem_m, "days": rem_d,
              "compensated_months": months,
              "text": f"{start.isoformat()} → {end.isoformat()} = "
                      f"{y} 年 {rem_m} 个月 {rem_d} 天"}
    elif years is not None and float(years) > 0:
        months = _round_half(float(years))
        ym = {"years": None, "months": None, "compensated_months": months,
              "text": f"按给定的 {float(years):g} 年折算"}
        assumptions.append("未提供起止日期，年限按调用方给定值，未做零头折算")
    elif scenario != "overtime":
        # 有起止日期但 end < start：不静默按 years 走，如实报矛盾
        if start and end:
            return _cannot(name, scenario, ["起止日期顺序颠倒（离职早于入职）"],
                           {"start_date": start_date, "end_date": end_date})
        missing.append("start_date/end_date 或 years")

    wage = float(monthly_wage) if monthly_wage not in (None, "") else None
    if scenario != "overtime" and (wage is None or wage <= 0):
        missing.append("monthly_wage")

    if missing:
        return _cannot(name, scenario, missing,
                       {"monthly_wage": monthly_wage, "start_date": start_date,
                        "end_date": end_date, "years": years})

    # ---- 三倍封顶（只有给了封顶线才判，没给就明确写进 assumptions）
    if wage and cap_monthly and float(cap_monthly) > 0:
        if wage > float(cap_monthly):
            capped = True
            assumptions.append(
                f"月工资 {wage:g} 元高于三倍封顶线 {float(cap_monthly):g} 元，"
                f"已按封顶线计算（《劳动合同法》第47条）")
            wage = float(cap_monthly)
        if months is not None and months > _CAP_YEARS:
            capped = True
            assumptions.append(
                f"工作年限折算 {months:g} 个月超过 12 年上限，已按 12 年（12 个月）计")
            months = _CAP_YEARS
    elif wage and months is not None and months > _CAP_YEARS:
        assumptions.append("未提供当地上年度职工月平均工资三倍封顶线，未做封顶判断")

    items: list[dict[str, Any]] = []
    total: float | None = None
    total_kind = "single"

    if scenario in ("illegal_termination", "economic_compensation", "notice_pay",
                    "termination_bundle"):
        assert wage is not None and months is not None  # 上面已判过缺失
        n_val = round(wage * months, 2)
        two_n = round(n_val * 2, 2)
        row_n = {
            "label": "经济补偿（N）", "formula": f"{wage:g} × {months:g} 个月",
            "value": n_val, "unit": "元", "scenario": "economic_compensation",
            "basis": "《劳动合同法》第47条",
        }
        row_2n = {
            "label": "违法解除赔偿金（2N）", "formula": f"{wage:g} × {months:g} 个月 × 2倍",
            "value": two_n, "unit": "元", "scenario": "illegal_termination",
            "basis": "《劳动合同法》第87条",
        }
        row_notice = {
            "label": "代通知金", "formula": f"上一个月工资标准 {wage:g}",
            "value": round(wage, 2), "unit": "元", "scenario": "notice_pay",
            "basis": "《劳动合同法实施条例》第20条",
            "note": "仅在用人单位选择额外支付一个月工资解除时适用；"
                    "主张违法解除赔偿金（2N）时不再支持",
        }
        if scenario == "termination_bundle":
            # 三行互为**备选**（不是累加）：命中违法解除走 2N，否则走 N+代通知金。
            # 所以 total 给主请求（2N），并标 total_kind=primary，别让上层误当合计。
            items = [row_2n, row_n, row_notice]
            total = two_n
            total_kind = "primary"
            assumptions.append("2N 与 N／代通知金互为备选，不是相加关系；"
                               "total 取主请求（违法解除赔偿金 2N）")
        else:
            row = {"illegal_termination": row_2n, "economic_compensation": row_n,
                   "notice_pay": row_notice}[scenario]
            # 单项时也要把年限折算露出来，否则无法核对月数是怎么来的
            items = [{"label": "工作年限折算", "formula": ym.get("text") or "",
                      "value": months, "unit": "个月",
                      "basis": "《劳动合同法》第47条", "scenario": "years"}, row]
            total = float(row["value"])

    elif scenario == "overtime":
        ot = overtime or {}
        if not ot:
            return _cannot(name, scenario, ["overtime"],
                           {"overtime": None,
                            "hint": "需要 {hourly_base|monthly_wage, weekday_hours, "
                                    "restday_hours, holiday_hours}"})
        base_hourly = ot.get("hourly_base")
        if base_hourly in (None, "", 0):
            base_wage = ot.get("monthly_wage", monthly_wage)
            if base_wage in (None, ""):
                return _cannot(name, scenario, ["overtime.hourly_base 或 monthly_wage"],
                               {"overtime": ot, "monthly_wage": monthly_wage})
            base_hourly = float(base_wage) / _MONTHLY_PAY_DAYS / _DAILY_HOURS
            assumptions.append(
                f"加班费计算基数按 {float(base_wage):g} 元／月 折算为小时工资"
                f"（{_MONTHLY_PAY_DAYS:g} 天 × {_DAILY_HOURS:g} 小时）")
        base_hourly = float(base_hourly)

        hours_total = 0.0
        for key in ("weekday", "restday", "holiday"):
            hours = float(ot.get(f"{key}_hours") or 0)
            if hours <= 0:
                continue
            # 休息日加班"不能安排补休"才按 2 倍计 —— 这一条只能由律师核实，
            # 代码不替它做判断，写进 assumptions 提示。
            if key == "restday":
                assumptions.append("休息日加班按 2 倍计，前提是未能安排补休（需核实）")
            value = round(base_hourly * _OT_RATES[key] * hours, 2)
            items.append({
                "label": _OT_LABELS[key],
                "formula": f"{base_hourly:.2f} 元/时 × {_OT_RATES[key]:g} 倍 × {hours:g} 小时",
                "value": value, "unit": "元", "scenario": f"overtime_{key}",
                "basis": "《劳动法》第44条",
            })
            hours_total += hours
        if not items:
            return _cannot(name, scenario,
                           ["overtime.weekday_hours / restday_hours / holiday_hours"],
                           {"overtime": ot, "hint": "至少给一个加班小时数"})
        total = round(sum(i["value"] for i in items), 2)
        total_kind = "sum"
        items.insert(0, {"label": "加班费计算基数",
                         "formula": f"小时工资 {base_hourly:.2f} 元",
                         "value": base_hourly, "unit": "元/时",
                         "basis": "《劳动法》第44条", "scenario": "overtime_base"})

    return ToolResult(
        ok=True,
        tool=name,
        data={
            "scenario": scenario,
            "items": items,
            "total": total,
            "total_kind": total_kind,
            "missing": [],
            "assumptions": assumptions,
            "capped": capped,
            "years_months": ym,
        },
    )


# --------------------------------------------------------------------------- 小工具
def _round_half(value: float) -> float:
    """直接把年数折算成月数时的取整口径：向上取到 0.5 的整数倍。

    只在"调用方直接给年限、没给日期"时用 —— 拿不到零头天数，宁可略宽，
    也不要把零头抹掉（法条要求"六个月以上不满一年按一年算"，抹掉就是少算）。
    """
    import math

    return math.ceil(value * 2) / 2


def _cannot(name: str, scenario: str, need: list[str], got: dict[str, Any]) -> ToolResult:
    """关键事实缺失：返回缺口，不代 0、不猜。

    ``ok=False`` 是给上层看的信号 —— ReportNode 据此写一条证据缺口，
    让「证据缺口与不确定性」如实说明"金额未测算，原因是……"。
    调用方若把 ``total=None`` 当 0 渲染，就会在交付物里出现一个假的确数。
    """
    logger.info("damages_calc 缺少关键事实：%s（scenario=%s）", need, scenario)
    return ToolResult(
        ok=False,
        tool=name,
        error=f"缺少关键事实：{'、'.join(need)}",
        degraded=True,
        fallback_note="请补齐后再测算；本工具不会用推测值代入",
        data={"scenario": scenario, "missing": need, "got": got,
              "items": [], "total": None, "total_kind": "none",
              "assumptions": [], "capped": False},
    )


__all__ = ["damages_calc", "compensated_months", "SCENARIOS"]
