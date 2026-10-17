"""条款正文的收尾修剪：末条不许吃进落款，条文不许粘着章标题/页标/页码。

真机事故（2026-09-29）：`split_articles` 切末条是"切到文件尾"（法规这么切没问题，
末条后面本来就没东西），可**合同末条后面恰恰是落款签名区**。于是
`第二十五条` 的正文里挂着「甲方：…／乙方：…／盖章：／2024 年 2 月 28 日」，
改写时连同落款一起被替换掉 —— 交付出去的合同没有签署页，那是废纸。

同一个机制还把下一节的章标题（`八、劳动争议处理`）和页标页码
（`<!-- 第 3 页 -->`、孤立的一个 `3`）粘在前一条的尾巴上。

⚠ 这里同时钉住**法规那条线不受影响** —— `split_articles` 是全站共用的
（`pipeline.py` / `statute_tools.py` 直接调它），法条末条没有落款，规则绝不能改到它头上。
"""

from __future__ import annotations

from backend.parser.structure import _trim_clause_tail, split_articles, split_clauses

# 真机材料的一段（task_7535a07a13a5），落款区一字未改地搬过来。
SIGN_BLOCK = "\n".join([
    "甲方：上海具识智能科技有限公司 甲方代表人（委托人）：陈志远",
    "地址：上海市浦东新区张江高科技园区科苑路88号3号楼5层",
    "乙方：李明轩 性别：男 身份证号：310115199503124210",
    "联系电话：13801765429",
    "地址：上海市浦东新区锦绣路2588弄15号602室",
    "甲方：上海具识智能科技有限公司",
    "乙方：李明轩",
    "盖章：",
    "2024 年 2 月 28 日",
])

CONTRACT = "\n".join([
    "劳 动 合 同 书",
    "一、劳动合同期限",
    "第一条 本合同为固定期限劳动合同，合同期自 2024年3月1日 起至 2027年2月28日 止，共计 36",
    "个月。",
    "",
    "<!-- 第 2 页 -->",
    "",
    "2",
    "八、劳动争议处理",
    "第二十四条 本合同一式贰份，甲乙双方各执壹份。",
    "第二十五条 本合同盖章签字生效。",
    SIGN_BLOCK,
])


def _clause(clauses, no):
    for c in clauses:
        if str(c.clause_no).strip() == no:
            return c
    raise AssertionError(f"没切出 {no}，实际切出：{[c.clause_no for c in clauses]}")


def test_last_clause_does_not_swallow_the_signature_block():
    """末条只到"这一条"为止 —— 落款区必须留在条款**外面**。

    这是本次最严重的一条：条款正文里带着落款，改写时整块签名区被替换掉。
    """
    text = _clause(split_clauses(CONTRACT), "第二十五条").clause_text
    assert text == "本合同盖章签字生效。", f"末条正文不对：{text!r}"
    # 逐**行**判泄漏，不能直接查子串：条文本身就有「本合同盖章签字生效」，
    # 查 "盖章" 会把正常条文判成泄漏（这里栽过一次）。
    for ln in text.splitlines():
        for leak in ("甲方", "乙方", "地址", "身份证号", "联系电话", "盖章", "2024"):
            assert not ln.strip().startswith(leak), f"落款串进了末条正文：{ln!r}"


def test_clause_does_not_carry_the_next_chapter_heading():
    """章标题粘在条文尾巴上（`第二十四条` 后面挂着 `八、劳动争议处理`）。"""
    text = _clause(split_clauses(CONTRACT), "第二十四条").clause_text
    assert text == "本合同一式贰份，甲乙双方各执壹份。", f"条文正文不对：{text!r}"
    assert "八、" not in text, "下一节的章标题粘在条文尾巴上"


def test_clause_does_not_carry_page_markers_or_page_numbers():
    """页标与孤立页码不进条款正文 —— 那是页脚的事。"""
    for c in split_clauses(CONTRACT):
        t = c.clause_text or ""
        assert "<!--" not in t, f"{c.clause_no} 的正文里有页标：{t!r}"
        assert "第 2 页" not in t, f"{c.clause_no} 的正文里有页标：{t!r}"
        assert not any(ln.strip() == "2" for ln in t.splitlines()), \
            f"{c.clause_no} 的正文里有孤立页码：{t!r}"


def test_pdf_hard_wrap_is_not_treated_as_a_boundary():
    """PDF 的硬换行不能当条款边界 —— 第一条仍是完整的一句（折行照旧保留）。

    ⚠️ 只断言"没被腰斩"，不断言"合并成一行"：并回自然段是**交付层**
    （`revise_service._contract_markdown`）的活，解析层保持原样，
    因为左栏要按行回收、字数要对得上（`scripts/contract_logic_check.mjs`）。
    """
    text = _clause(split_clauses(CONTRACT), "第一条").clause_text
    assert "共计 36" in text and "个月。" in text, f"第一条被切断了：{text!r}"


def test_declaration_style_clause_starting_with_party_words_is_not_emptied():
    """以「甲方：」开头的**定义条**不能被清空（命中位置必须在正文内部）。

    合同里「甲方：指……」「乙方：指……」是常见写法，如果允许在位置 0 截断，
    整条会被清成空串 —— 比不修剪更糟。
    """
    body = "甲方：指依法设立并合法存续的用人单位。\n乙方：指与甲方建立劳动关系的劳动者。"
    assert _trim_clause_tail(body) == body, "定义条被误清空了"


def test_statute_path_is_untouched():
    """**法规那条线一个字都不能变。**

    `split_articles` 是法规入库与合同解析共用的（`pipeline.py:205`、
    `statute_tools.py:471`）。合同末条后面是落款，法规末条后面是**公布令**
    （主席令 / 签署人 / 日期）—— 长得像，但法规这条线**必须原样保留**：
    入库的全文、检索命中、引用核对全都依赖它。
    所以修剪只能加在 `split_clauses` 那一层，绝不能下沉进 `split_articles`。

    这里故意用一段"末条后面跟着主席令"的法规，把这条边界钉死：
    有人要是图省事把修剪挪进 `split_articles`，这条立刻红。
    """
    law = ("第一条 为了保护劳动者的合法权益，根据宪法，制定本法。\n"
           "第二条 在中华人民共和国境内的企业、个体经济组织（以下统称用人单位）"
           "和与之形成劳动关系的劳动者，适用本法。\n"
           "第九十八条 本法自2008年1月1日起施行。\n"
           "中华人民共和国主席 习近平\n"
           "2021 年 1 月 22 日")
    arts = split_articles(law)
    assert [a.article_no for a in arts] == ["第一条", "第二条", "第九十八条"]
    assert arts[0].text == "为了保护劳动者的合法权益，根据宪法，制定本法。"
    # 末条**切到文件尾**（含后面的公布令）：法规这条线的既有行为，改了就是回归
    assert arts[-1].text == ("本法自2008年1月1日起施行。\n"
                             "中华人民共和国主席 习近平\n"
                             "2021 年 1 月 22 日")
    assert arts[-1].text == law.split("第九十八条 ")[1]
