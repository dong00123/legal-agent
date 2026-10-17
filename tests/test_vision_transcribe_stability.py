"""整页转写的「字数稳定」契约。

**来历**（用户报障：「合同识别每次识别的字数都不一样」）：
同一张扫描件连跑 6 次，前端显示的字数是 811 / 807 / 809 / 797 / 810 / 804 —— **6 次 6 种**。
逐字比对归类后，47 处差异里 **38 处是空白**（这次断行断在哪）、5 处是填空线下划线长度、
只有 4 处是真实汉字分歧。⇒ 处置两条：``normalize_scan_text`` 清掉非正文噪声，
``count_chars`` 计数时不计空白。实测极差从 14 落到 1。

⚠️ 先排除过的假设：以为是 VLM 硬编码 ``temperature: 0.1`` 的采样随机 —— **实测否掉**
（temp=0 一样漂：811/813/807）。所以下面**没有**任何一条断言"temperature=0 就能稳定"。

⛔ 这里也有**反向**断言：那 4 处真实汉字差异**不许**被抹平
（把「劳动合用」归一化成「劳动合同」等于把 OCR 错字改成对的，是做假）。
"""

from __future__ import annotations

from backend.app.api.v1.chat import count_chars
from backend.parser.vision.client import normalize_scan_text

# 两次真实识别结果的缩样（同一张图）：差异只在空白与填空线长度上。
SAMPLE_A = "劳动合 同 书\n根据《中华人民共和国劳动法》及有关法律法规,\n月底薪______元，____元全勤奖\n"
SAMPLE_B = "劳动合同书\n根据《中华人民共和国劳动法》及有关法律法规,\n月底薪____元，______元全勤奖\n"


def test_count_chars_excludes_whitespace() -> None:
    """字数不计空白：空格 / 换行 / 制表 / 全角空格都不算。"""
    assert count_chars("劳动合同书") == 5
    assert count_chars("劳动 合同\n书\t") == 5
    assert count_chars("劳动\u3000合同\u3000书") == 5
    assert count_chars("") == 0
    assert count_chars(None) == 0  # type: ignore[arg-type]


def test_whitespace_and_underline_noise_does_not_change_count() -> None:
    """**核心契约**：只有空白与填空线长度不同时，字数必须完全一致。

    这两段就是同一张图两次识别出来的样子。曾经前端取 ``text.length``，
    于是同一份合同每次显示的字数都不同 —— 这条断言锁的就是那个病。
    """
    a, b = normalize_scan_text(SAMPLE_A), normalize_scan_text(SAMPLE_B)
    assert a != "" and b != ""
    assert count_chars(a) == count_chars(b), f"{a!r} 与 {b!r} 的字数不一致"


def test_normalize_is_idempotent() -> None:
    """归一化必须幂等，否则"再存一次/再渲染一次"会继续漂。"""
    once = normalize_scan_text(SAMPLE_A)
    assert normalize_scan_text(once) == once


def test_cjk_inner_space_removed_but_meaningful_space_kept() -> None:
    """中文之间的空格是噪声要删；中文与数字/英文之间、表格分隔符周围的空格有含义，保留。"""
    assert normalize_scan_text("劳动合 同 书") == "劳动合同书"
    # 数字两侧的空格保留（"月 5000 元" 里的分隔是排版，不是模型抖动）
    assert "5000" in normalize_scan_text("月 5000 元")
    # 表格行的 | 分隔与两侧空格不能被吃掉
    assert normalize_scan_text("姓名 | 张三 | 金额") == "姓名 | 张三 | 金额"


def test_underline_runs_are_fixed_length() -> None:
    """填空线长度是估出来的排版值：长的短的都归到同一个固定长度。"""
    assert normalize_scan_text("底薪______元") == normalize_scan_text("底薪__________元")
    assert normalize_scan_text("底薪______元") == "底薪____元"


def test_illegible_marker_count_is_kept() -> None:
    """``□`` 的个数**故意保留**：它虽然也是估的，但"这一页有多少字看不清"对律师是有用信号。"""
    assert normalize_scan_text("此处□□□无法辨认") == "此处□□□无法辨认"


def test_real_character_difference_is_not_erased() -> None:
    """⛔ 反向断言：真实的识别分歧**不许**被归一化掉 —— 那是把错字改成对的。

    「劳动合用」与「劳动合同」是模型真读出了不同的字；漏掉一个「同」则会让字数差 1。
    这两种情况都必须"露出来"，而不是被磨平成一个统一结果。
    """
    assert normalize_scan_text("本合同为期限劳动合用") != normalize_scan_text("本合同为期限劳动合同")
    assert count_chars(normalize_scan_text("劳动合")) != count_chars(normalize_scan_text("劳动合同"))
