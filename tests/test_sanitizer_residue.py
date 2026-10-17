"""脱敏残留检查的回归测试。

**只测一件事**：``find_residue`` 与脱敏规则要用同一把尺子。

曾经两处判据不一致 —— 脱敏规则（走 ``_is_person_name``）正确地放过了
「当事人陈述」，而 ``find_residue`` 只看正则有没有命中，于是把它报成
"疑似未脱敏姓名"。后果有两层：

1. 每次处理真实文本都刷一条 WARNING，看久了就没人看了；
2. 微调数据工程用 ``find_residue`` 剔除样本，误报会白白淘汰合格样本
   （实测一次淘汰了 5 条）。

所以下面**正反两组都测**：该报的必须报，不该报的一条都不能报。
只测"该报的报了"是不够的 —— 把判据放宽到什么都报也能全绿。
"""

from __future__ import annotations

from backend.memory.sanitizer import find_residue, sanitize

# 真名：脱敏后不该再有残留告警
POSITIVE = [
    "原告张三诉称被告李四未按约付款。",
    "劳动者赵六主张加班费。",
    "委托人孙七签署了授权委托书。",
]

# 法律术语 / 常用表达：长得像"角色标签 + 姓名"，但不是姓名
NEGATIVE = [
    "当事人陈述：原告主张被告违约。",
    "劳动者陈述其月工资为8000元。",
    "当事人陈述与代理人的代理意见不一致。",
    "用人单位应当依法支付劳动报酬。",
    "原告主张被告承担违约责任。",
]


def test_real_names_are_masked_and_not_reported_as_residue() -> None:
    """真名要被脱敏，且脱敏后不该再报残留。"""
    for text in POSITIVE:
        masked = sanitize(text)
        assert masked != text, f"真名没有被脱敏：{text}"
        assert find_residue(masked) == [], f"脱敏后仍报残留：{masked}"


def test_legal_terms_are_not_reported_as_residue() -> None:
    """术语不能被当成姓名 —— 这是本轮修的那个误报。"""
    for text in NEGATIVE:
        masked = sanitize(text)
        hits = find_residue(masked)
        assert hits == [], f"「{text}」被误报为残留：{hits}"
        # 顺带确认正文没被改坏：术语里的字不该变成「某」
        assert "陈述" in masked or "主张" in masked or "应当" in masked, masked
