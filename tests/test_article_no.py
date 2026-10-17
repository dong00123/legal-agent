"""条文号转换与精确条文定位回归测试。

覆盖的缺陷（都曾真实存在，且都是**静默失效**——不崩溃、不报错、格式正常）：

1. ``parse_exact_cites`` 返回的是**归一后的阿拉伯形**（``第585条``），
   而法规正文里的条文号一律是**中文数字**（``第五百八十五条``）。
   拿阿拉伯形去搜 BM25 返回 **0 条**（不是"排得靠后"，是彻底空手而归），
   精确条文定位静默落空，风险点依据只能退回语义召回碰运气 ——
   表现是每条风险点都写着「需补充法律依据」，而库里明明有这些条文。

2. 两字体裁后缀（「**法典**」）的法规名解析不出来：名称中段写成 ``{2,30}?`` 时
   「民法典」整体至少要 4 字，永远匹配不上，于是《民法典》第五百八十五条
   被当成"没有条文要求"，退回语义召回。

3. ``_law_name_eq`` 若放宽成子串匹配，「劳动法」会冒充「劳动合同法」——
   《劳动法》第二十四条（工作时间）钉进《劳动合同法》第二十四条（竞业限制）的位置，
   还顶着 ``source=exact`` 的高可信标记。**错得毫无痕迹**，比不命中更糟。
"""

from __future__ import annotations

import pytest

from backend.parser.structure import (
    article_key,
    article_no_cn,
    cn_to_int,
    int_to_cn,
)
from backend.tools.statute_tools import _law_name_eq, _probe_forms, parse_exact_cites


# --------------------------------------------------------------------------- 数词互转
@pytest.mark.parametrize("number,expected", [
    (1, "一"), (2, "二"), (9, "九"),
    (10, "十"),          # 法律文本写「第十条」，不是「第一十条」
    (11, "十一"), (19, "十九"),
    (20, "二十"), (24, "二十四"), (87, "八十七"),
    (100, "一百"), (101, "一百零一"),   # 「零」不能丢
    (110, "一百一十"), (111, "一百一十一"),
    (585, "五百八十五"), (590, "五百九十"),
    (1000, "一千"), (1201, "一千二百零一"),
])
def test_int_to_cn(number: int, expected: str) -> None:
    assert int_to_cn(number) == expected


@pytest.mark.parametrize("number", [1, 9, 10, 11, 19, 20, 24, 87, 100, 101, 110,
                                    111, 120, 200, 585, 586, 590, 1000, 1201, 1260])
def test_int_to_cn_round_trip(number: int) -> None:
    """正反两向必须可逆：单向写对也可能漏掉「零」这类边界。"""
    assert cn_to_int(int_to_cn(number)) == number


def test_int_to_cn_non_positive() -> None:
    assert int_to_cn(0) == ""
    assert int_to_cn(-3) == ""


# --------------------------------------------------------------------------- 条文号还原
@pytest.mark.parametrize("raw,expected", [
    ("第585条", "第五百八十五条"),
    ("585", "第五百八十五条"),
    ("第五百八十五条", "第五百八十五条"),   # 已是中文形，原样返回
    ("第87条", "第八十七条"),
    ("第19条", "第十九条"),
    ("第87条之二", "第八十七条之二"),
])
def test_article_no_cn(raw: str, expected: str) -> None:
    assert article_no_cn(raw) == expected


@pytest.mark.parametrize("raw", ["", "第X条", "abc", "第条"])
def test_article_no_cn_invalid(raw: str) -> None:
    assert article_no_cn(raw) == ""


def test_article_key_and_cn_agree() -> None:
    """两种写法必须落到同一个比较键，否则同一法条在两条路径上算出不同的键。"""
    assert article_key("第五百八十五条") == article_key("第585条") == "585"


# --------------------------------------------------------------------------- 检索式形态
def test_probe_forms_prefers_chinese_numeral() -> None:
    """中文数字形态必须排在最前 —— 排在后面时每次都要先白跑一次必然落空的检索。"""
    forms = _probe_forms("民法典", "第585条")
    assert forms[0] == "民法典 第五百八十五条"
    assert "民法典 第585条" in forms
    assert forms[-1] == "民法典"          # 最后的兜底只给法规名


def test_probe_forms_dedupes() -> None:
    """已经是中文形时不该出现两条一模一样的检索式。"""
    assert _probe_forms("民法典", "第五百八十五条").count("民法典 第五百八十五条") == 1


# --------------------------------------------------------------------------- 精确条文解析
def test_parse_exact_cites_two_char_suffix() -> None:
    """「民法典」以两字体裁词收尾，曾被前置字符数下限卡掉。"""
    assert parse_exact_cites("《民法典》第五百八十五条") == [("民法典", "第585条")]


def test_parse_exact_cites_multiple_keeps_order() -> None:
    q = ("《民法典》第五百八十五条 《民法典》第五百八十六条 "
         "《民法典》第五百六十三条 《民法典》第五百九十条 劳动合同")
    assert parse_exact_cites(q) == [
        ("民法典", "第585条"),
        ("民法典", "第586条"),
        ("民法典", "第563条"),
        ("民法典", "第590条"),
    ]


def test_parse_exact_cites_arabic_form() -> None:
    assert parse_exact_cites("劳动合同法第24条") == [("劳动合同法", "第24条")]


def test_parse_exact_cites_dedupes() -> None:
    q = "《劳动合同法》第二十四条 劳动合同法第24条"
    assert parse_exact_cites(q) == [("劳动合同法", "第24条")]


@pytest.mark.parametrize("text", [
    "因本合同发生争议，适用第八条",      # 正文片段，不是法规名
    "依据该法第3条处理",                 # 回指词，指不出是哪部法
    "按照上述规定第5条",                 # 回指
    "没有条文要求的普通提问",
])
def test_parse_exact_cites_ignores_non_statute(text: str) -> None:
    """凭空构造出一个不存在的精确检索要求，会让检索整条跑偏。"""
    assert parse_exact_cites(text) == []


# --------------------------------------------------------------------------- 法规名比较
def test_law_name_eq_strips_prefix_and_marks() -> None:
    assert _law_name_eq("民法典", "《中华人民共和国民法典》")
    assert _law_name_eq("劳动合同法", "《中华人民共和国劳动合同法》")


@pytest.mark.parametrize("a,b", [
    ("劳动法", "劳动合同法"),        # 前者是后者的子串 —— 但这是两部不同的法律
    ("民法典", "民事诉讼法"),
    ("民法典", ""),
    ("", "民法典"),
])
def test_law_name_eq_rejects_false_positives(a: str, b: str) -> None:
    """引错法比不引法更糟：错的那条会被对方律师直接拿去打。"""
    assert not _law_name_eq(a, b)
