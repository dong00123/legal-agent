"""富文本净化器的攻防用例。

**这份 HTML 会存库、再渲染回来**，所以每一条漏过都等于一个存储型 XSS ——
不是"当次请求脏了"，而是"每个打开这份草稿的人都中招"。用例按攻击面写，
不按函数分支写。
"""

from __future__ import annotations

import pytest

from backend.app.services.html_sanitize import sanitize


# --------------------------------------------------------------------------- 脚本
@pytest.mark.parametrize("payload", [
    "<script>alert(1)</script>",
    "<SCRIPT>alert(1)</SCRIPT>",
    "<script src=//evil.com/x.js></script>",
    "<style>body{background:url(javascript:alert(1))}</style>",
    "<iframe src=//evil.com></iframe>",
    "<object data=//evil.com></object>",
    "<embed src=//evil.com>",
    "<svg><script>alert(1)</script></svg>",
    "<template><script>alert(1)</script></template>",
])
def test_dangerous_tags_are_dropped_entirely(payload: str) -> None:
    """危险标签连内容一起丢掉（脱壳会把脚本正文留在页面上）。"""
    out = sanitize(payload)
    for bad in ("script", "iframe", "object", "embed", "svg", "template", "url("):
        assert bad not in out.lower(), f"{bad} 漏过了：{out!r}"


def test_comment_is_dropped() -> None:
    """注释节点要丢 —— IE 条件注释可以当脚本用，而且注释里能藏东西。"""
    out = sanitize("<p>正常</p><!-- <script>alert(1)</script> -->")
    assert "正常" in out
    assert "script" not in out.lower()
    assert "<!--" not in out


# --------------------------------------------------------------------------- 事件属性
@pytest.mark.parametrize("payload,needle", [
    ("<img src=x onerror=alert(1)>", "onerror"),
    ("<p onclick=alert(1)>点我</p>", "onclick"),
    ("<div onmouseover=alert(1)>x</div>", "onmouseover"),
    ('<span style="x:1" ONLOAD="alert(1)">x</span>', "onload"),
])
def test_event_handlers_are_stripped(payload: str, needle: str) -> None:
    out = sanitize(payload)
    assert needle not in out.lower(), f"事件属性漏过了：{out!r}"


def test_img_is_unwrapped_but_keeps_text() -> None:
    """``img`` 不在白名单里，但它没有文字，脱壳后应当什么都不剩。"""
    out = sanitize('<p>前<img src=x onerror=alert(1)>后</p>')
    assert "img" not in out.lower()
    assert "前" in out and "后" in out, "脱壳不该把周围的文字一起吃掉"


# --------------------------------------------------------------------------- 协议与 CSS
@pytest.mark.parametrize("payload", [
    '<a href="javascript:alert(1)">点我</a>',
    '<p style="background:url(javascript:alert(1))">x</p>',
    '<p style="background-image:url(//evil.com/a.png)">x</p>',
    '<p style="width:expression(alert(1))">x</p>',
    '<p style="color:red;@import url(//evil.com)">x</p>',
])
def test_javascript_and_url_vectors_are_blocked(payload: str) -> None:
    out = sanitize(payload)
    assert "javascript:" not in out.lower()
    assert "url(" not in out.lower()
    assert "expression(" not in out.lower()
    assert "@import" not in out.lower()
    assert "href" not in out.lower()


def test_link_is_unwrapped_keeping_text() -> None:
    """``<a>`` 不在白名单 → 脱壳保文字。文书里的链接文字不能凭空消失。"""
    out = sanitize('<p>依据 <a href="https://x.com">《劳动合同法》</a> 第十条</p>')
    assert "《劳动合同法》" in out
    assert "<a" not in out


def test_safe_css_survives() -> None:
    """该留的要留 —— 净化器把工具栏能改的样式全删掉就等于把功能删了。"""
    style = ("font-family:'黑体';font-size:16pt;font-weight:700;"
             "text-align:center;text-indent:24.1pt;color:#ff0000")
    out = sanitize(f'<p style="{style}">标题</p>')
    for prop in ("font-family", "font-size", "font-weight", "text-align",
                 "text-indent", "color"):
        assert prop in out, f"{prop} 被误删了：{out!r}"
    assert "黑体" in out and "24.1pt" in out


def test_css_with_bad_declaration_keeps_the_rest() -> None:
    """一条声明可疑，不该把整个 style 丢掉。"""
    out = sanitize('<p style="text-align:center;background:url(x)">x</p>')
    assert "text-align:center" in out
    assert "url(" not in out


# --------------------------------------------------------------------------- 结构
def test_table_structure_survives() -> None:
    """表格骨架必须原样留下 —— 表单型起诉状全靠它，``colspan`` 丢了就散架。"""
    src = ('<table style="border-collapse:collapse;border:1px solid #000">'
           '<tbody><tr><td colspan="2" rowspan="3" style="width:100pt">'
           '<p>当事人信息</p></td><td>张三</td></tr></tbody></table>')
    out = sanitize(src)
    assert "<table" in out and "<td" in out
    assert 'colspan="2"' in out and 'rowspan="3"' in out
    assert "border-collapse" in out


@pytest.mark.parametrize("value", ['colspan="x"', 'colspan="2;"', 'rowspan="a"'])
def test_non_numeric_span_is_dropped(value: str) -> None:
    """``colspan`` / ``rowspan`` 会被原样拼回属性位，只收正整数。"""
    out = sanitize(f"<table><tr><td {value}>x</td></tr></table>")
    assert "colspan=\"x\"" not in out and "colspan=\"2;\"" not in out
    assert "rowspan=\"a\"" not in out


def test_div_survives_because_enter_produces_it() -> None:
    """``div`` 必须在白名单里。

    浏览器在 contenteditable 里按回车默认插的就是 ``<div>``。把它脱壳的话，
    用户在页面上敲出来的换行会在存库读回之后**静默消失** —— 而"敲了回车
    结果没了"是几乎不可能被归因到净化器的。
    """
    out = sanitize("<div>第一行</div><div>第二行</div>")
    assert "<div" in out
    assert "第一行" in out and "第二行" in out


def test_plain_text_passes_through_unchanged() -> None:
    """没有标签的纯文本原样返回（绝大多数草稿的 content_html 就是空的）。"""
    assert sanitize("") == ""
    assert sanitize("就是一段普通文字，没有标签") == "就是一段普通文字，没有标签"


def test_chinese_and_quotes_are_not_mangled() -> None:
    """中文与引号不能被转义坏 —— 法律文书里全是「」和书名号。"""
    out = sanitize('<p style="text-align:right">具状人（签字、盖章）："张三"</p>')
    assert "具状人（签字、盖章）" in out
    assert '"张三"' in out
    assert "&quot;" not in out


def test_unknown_tag_is_unwrapped_not_deleted() -> None:
    """不在白名单的普通标签脱壳保文字 —— 律师写的字不能因标签怪就消失。"""
    out = sanitize("<article><p>重要内容</p></article>")
    assert "重要内容" in out
    assert "article" not in out


@pytest.mark.parametrize("payload,needle", [
    ("<a href='#'>点我</a>", "点我"),               # 脱壳后文字直接挂在根下
    ("<article>重要内容</article>", "重要内容"),      # 同上
    ("前<a href='#'>中</a>后", "后"),               # 脱壳节点的 tail 也要留住
    ("<font size=7>加字号</font>", "加字号"),         # execCommand 的 fontSize 产物
])
def test_text_survives_when_unwrapped_at_the_root(payload: str, needle: str) -> None:
    """**脱壳发生在根一层时，文字不能丢。**

    这是个真踩过的坑：序列化时只遍历子**元素**，而 ``drop_tag()`` 会把文字提到
    父节点的 ``.text`` / ``.tail`` —— 那两者不是元素，迭代取不到。于是
    ``<a>点我</a>`` 变成空串，**律师写的字凭空消失**。

    包在 ``<p>`` 里的那种碰巧没事（文字落进了 ``<p>`` 里），所以这个 bug 只在
    特定嵌套深度冒出来，靠"随手试一个"是发现不了的 —— 才要按四种位置都钉一遍。
    """
    out = sanitize(payload)
    assert needle in out, f"文字被吞了：{payload!r} → {out!r}"
    for bad in ("<a ", "<article", "<font"):
        assert bad not in out, f"脱壳不干净：{out!r}"
