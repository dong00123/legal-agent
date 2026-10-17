"""改后合同（交付正文）的形状：落款一字不少、页码不在正文里、版式标记打对。

`build_rewritten` 拼出来的东西是**律师直接拿去用的那一份**，
但这条链此前一个测试都没有（全仓 grep `build_rewritten` 只命中 `chat.py` 与探针脚本）。
"格式什么都没了""里面还有页数""落款整块消失"就是从这里漏到用户手里的（2026-09-29）。
"""

from __future__ import annotations

import re

from backend.app.services import revise_service
from backend.app.services.revise_service import (
    build_rewritten,
    to_plain_text,
    _contract_markdown,
)

# 真机材料（task_7535a07a13a5）的原样摘录：页标、孤立页码、章标题、PDF 硬换行、
# 落款签名区，五样都在，才能一次把它们钉住。
BODY = "\n".join([
    "<!-- 第 1 页 -->",
    "",
    "1",
    "劳 动 合 同 书",
    "根据《中华人民共和国劳动法》及有关法律法规，经甲乙双方平等自愿协商一致，同意签订本",
    "合同，共同遵守本合同所列条款。",
    "一、劳动合同期限",
    "第一条 本合同为固定期限劳动合同，合同期自 2024年3月1日 起至 2027年2月28日 止，共计 36",
    "个月。",
    "",
    "<!-- 第 3 页 -->",
    "",
    "3",
    "九、其他规定",
    "第二十五条 本合同盖章签字生效。",
    "甲方：上海具识智能科技有限公司 甲方代表人（委托人）：陈志远",
    "地址：上海市浦东新区张江高科技园区科苑路88号3号楼5层",
    "乙方：李明轩 性别：男 身份证号：310115199503124210",
    "联系电话：13801765429",
    "盖章：",
    "2024 年 2 月 28 日",
])

SIGN_LINES = [
    "甲方：上海具识智能科技有限公司 甲方代表人（委托人）：陈志远",
    "地址：上海市浦东新区张江高科技园区科苑路88号3号楼5层",
    "乙方：李明轩 性别：男 身份证号：310115199503124210",
    "联系电话：13801765429",
    "盖章：",
    "2024 年 2 月 28 日",
]


def _picked(clause_no: str, original: str, start: int, end: int) -> dict:
    return {"seq": 1, "clause_no": clause_no, "level": "high",
            "start": start, "end": end, "original": original}


def _rewrite_one(contract_text: str, clause_no: str, original: str,
                 new_text: str) -> str:
    start = contract_text.index(original)
    picked = [_picked(clause_no, original, start, start + len(original))]
    rewrites = {1: {"revised_text": new_text, "note": "", "ok": True}}
    return build_rewritten(contract_text, picked, rewrites)["rewritten_text"]


# ---------------------------------------------------------- 落款签名区（最严重）

def test_signature_block_survives_a_rewrite():
    """改写末条之后，落款区**一行都不能少**。

    少一个签署页的合同是废纸 —— 这是本轮最严重的一条。
    """
    out = _rewrite_one(BODY, "第二十五条", "本合同盖章签字生效。",
                       "本合同自双方签字或盖章之日起生效。")
    for ln in SIGN_LINES:
        assert ln in out, f"落款丢了：{ln!r}"
    assert "本合同自双方签字或盖章之日起生效。" in out


def test_rewrite_never_lands_inside_the_signature_block():
    """改写区间**不许跨进签名区**（第二道防线：`_tighten` 也在落款处收口）。

    就算定位再跑偏（条款正文又带上了落款），交付出去的落款也必须还在。
    """
    # 模拟"解析层没修好"的旧数据：末条的正文里就挂着落款
    dirty = BODY  # 解析层已修，这里靠 _tighten 兜底
    span_original = "本合同盖章签字生效。\n" + "\n".join(SIGN_LINES[:3])
    out = _rewrite_one(dirty, "第二十五条", span_original,
                       "本合同自双方签字或盖章之日起生效。")
    for ln in SIGN_LINES[3:]:
        assert ln in out, f"落款被改写区间吃掉了：{ln!r}"


# ---------------------------------------------------------- 页标与页码

def test_deliverable_has_no_page_markers_or_lone_page_numbers():
    """交付正文里不许有页标或孤立页码 —— 页数走**页脚**，不走正文。

    用户 2026-09-29：「页数是页脚，不是里面的文字」。
    """
    out = _rewrite_one(BODY, "第二十五条", "本合同盖章签字生效。",
                       "本合同自双方签字或盖章之日起生效。")
    assert "<!--" not in out, "交付正文里还有页标"
    assert "第 1 页" not in out and "第 3 页" not in out
    for ln in out.splitlines():
        assert not re.fullmatch(r"\d{1,3}", ln.strip() or "x"), \
            f"交付正文里有孤立页码：{ln!r}"


# ---------------------------------------------------------- 版式标记

def test_deliverable_marks_title_chapters_and_signature():
    """标题/章标题/落款各自带上导出层认得的标记。

    导出层（`docs._write_docx` / `_write_pdf`）是**按行认标记**的：
    没有 `#` 就地居中不了，没有 `###` 章标题就和正文一样大，
    没有 `<p align="…">` 落款就和正文一样缩进两格。
    """
    out = _rewrite_one(BODY, "第二十五条", "本合同盖章签字生效。",
                       "本合同自双方签字或盖章之日起生效。")
    assert out.splitlines()[0] == "# 劳 动 合 同 书", "大标题没打 `#` 标记"
    assert "### 一、劳动合同期限" in out, "章标题没打 `###` 标记"
    assert '### 九、其他规定' in out
    assert '<p align="left">甲方：上海具识智能科技有限公司' in out, "落款没打 left 标记"
    assert '<p align="right">2024 年 2 月 28 日</p>' in out, "落款日期没靠右"


def test_pdf_hard_wrap_is_merged_back_into_one_paragraph():
    """PDF 的硬换行并回自然段（原件是一句话，不并就断成两段）。"""
    out = _rewrite_one(BODY, "第二十五条", "本合同盖章签字生效。",
                       "本合同自双方签字或盖章之日起生效。")
    assert ("根据《中华人民共和国劳动法》及有关法律法规，经甲乙双方平等自愿协商一致，"
            "同意签订本合同，共同遵守本合同所列条款。") in out, "第一段没并回一句"
    # 断行处的空格本来就没了（PDF 抽取按行 strip），并回来是「36个月。」——
    # 只把断开的字接上，**不凭空补空格**（交付合同不添一个字）。
    assert "共计 36个月。" in out, "第一条的折行没并回去"
    # 并过头也不行：章标题不能被并进上一条
    assert "### 一、劳动合同期限" in out
    for ln in out.splitlines():
        assert not ln.startswith("第一条 本合同为固定期限劳动合同，合同期自 2024年3月1日 起至"
                                 " 2027年2月28日 止，共计 36 个月。一、"), "章标题被并进了正文"


def test_continuation_is_merged_but_finished_sentence_is_not():
    """折行合并的判据是**上一行有没有说完**（与前端 CT_RE_END 同源）。

    说完了就不并 —— 否则两条独立条款会被粘成一段。
    """
    src = "第一条 这是一句话的一半，\n后半句在这儿。\n第二条 这是另一条完整的话。\n第三条 这条没有句号"
    out = _contract_markdown(src)
    assert "第一条 这是一句话的一半，后半句在这儿。" in out
    assert "第二条 这是另一条完整的话。" in out
    # 没有句号的下一行会被并上去（宁可并错也不能丢字）
    assert "第三条 这条没有句号" in out


def test_annotate_mode_deliverable_is_untouched():
    """对照稿（`build_revision`）不走这套标记 —— 它的 `~~`/`++` 是**按行**解析的。

    重排段落会让导出层认不出修订标记，Word 里直接显示 `~~…`。
    所以 `_contract_markdown` 只服务 rewrite 模式。
    """
    out = _rewrite_one(BODY, "第二十五条", "本合同盖章签字生效。",
                       "本合同自双方签字或盖章之日起生效。")
    assert "~~" not in out and "++" not in out
    # rebuild 后依然是纯交付正文，没有被塞进对照稿的头部说明
    assert "本修订版由 AI" not in out


# ---------------------------------------------------------- 剪贴板用的纯文本

def test_to_plain_text_strips_presentation_marks_only():
    """「复制改后合同」走纯文本剪贴板：版式标记要脱掉，**分段与内容一个字不丢**。"""
    marked = _contract_markdown(BODY)
    plain = to_plain_text(marked)
    assert "#" not in plain and "###" not in plain
    assert '<p align="' not in plain
    assert "劳 动 合 同 书" in plain
    assert "甲方：上海具识智能科技有限公司 甲方代表人（委托人）：陈志远" in plain
    assert "2024 年 2 月 28 日" in plain
    # 行数一致：脱标记不该顺手改分段
    assert len(plain.splitlines()) == len(marked.splitlines())


def test_plain_text_is_the_revision_not_the_original():
    """纯文本必须是**改后**的合同，不是原文。

    真机缺陷：`chat.py` 回给「复制改后合同」按钮的 `clean_text` 曾经是
    `_strip_marks(revised_markdown)` —— 那是**改之前的原文**：
    律师点"复制改后合同"，粘出来的是没改过的合同，而且毫无提示。
    """
    new = "本合同自双方签字或盖章之日起生效，一式两份。"
    out = to_plain_text(_rewrite_one(BODY, "第二十五条", "本合同盖章签字生效。", new))
    assert new in out, "复制的还是原文"
    assert "本合同盖章签字生效。" not in out, "旧条文还在，复制到的是原文"


def test_guess_title_skips_file_and_page_markers():
    """抬头要落到真正的「劳 动 合 同 书」上，不能是文件名或页标。

    文件名顶着抬头 → 导出文件名成了「【合同（演示样例）.pdf】（改后版）.pdf」，
    用户直接看到（2026-09-29）。
    """
    text = "【合同（演示样例）.pdf】\n<!-- 第 1 页 -->\n\n1\n劳 动 合 同 书\n根据《劳动法》…"
    assert revise_service._guess_title(text, "labor") == "劳动合同书"
