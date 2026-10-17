"""富文本正文的 HTML 白名单净化器。

## 为什么必须有这一层

编辑页的正文是 ``contenteditable`` 的 ``innerHTML``，它的**来源有三条**，
每一条都可能带进 HTML：

1. ``docx_to_html`` 渲染出来的模板正文；
2. ``fills`` 里的**模型输出**（"事实与理由"整段由 LLM 生成）；
3. 浏览器 ``document.execCommand`` 的产物（含用户粘贴进来的任意内容 ——
   从网页复制一段带 ``<script>`` 的东西是很容易的事）。

这份 HTML 会**存库、再渲染回来**。也就是说一旦漏了，就是一个**存储型 XSS**，
不是"当次请求被污染"那么轻。项目里 ``web/app.js`` 开头就写着「绝不用 innerHTML
直插后端文本」，这个编辑页恰好是那条规矩最容易被绕开的地方 —— 所以净化放在
**服务端**：前端可以是旧的、可以被绕过，服务端不会。

## 为什么不装 bleach / nh3

两者都没装，``requirements.lock.txt`` 里也没有。而 ``backend/memory/sanitizer.py``
**不是这个东西** —— 那是个人信息脱敏：``<script>`` 原样穿过，而且它会把法律
文书里的人名一起改写掉，拿来做净化会把正文毁掉。项目已经有 ``lxml``，
白名单逻辑本身不到 100 行，不值得为它动依赖锁。

## 口径

- **不在白名单的标签一律"脱壳"**（去掉标签、保留文字），而不是整段删除 ——
  律师写的字不能因为包在怪标签里就消失；
- **例外**：``script/style/iframe/…`` 这些"内容本身就是危险载荷"的，
  连内容一起丢掉；
- **属性**：只留白名单里的；``on*`` 一律丢；``style`` 再过一道值白名单，
  挡掉 ``url()`` / ``expression()`` / ``javascript:``。
"""

from __future__ import annotations

import logging
import re
from html import escape
from typing import Any

from lxml import html as lxml_html

logger = logging.getLogger(__name__)

# 允许保留的标签。比 plan 里那张表多了 ``div`` —— 浏览器在 contenteditable 里
# 按回车默认插的就是 ``<div>``，若把它脱壳，换行会**静默消失**（用户在页面上
# 敲出来的分行、存库再读回来就没了）。div 本身没有任何危险能力。
ALLOWED_TAGS = frozenset({
    "p", "br", "div", "span",
    "strong", "b", "em", "i", "u", "s", "strike", "sub", "sup",
    "ul", "ol", "li",
    "table", "thead", "tbody", "tr", "td", "th",
    "h1", "h2", "h3", "h4", "blockquote",
})

# 危险标签：整个丢掉（连同文字），脱壳反而会把脚本正文留在页面上。
DROP_TAGS = frozenset({
    "script", "style", "iframe", "object", "embed", "link", "meta", "base",
    "form", "input", "button", "textarea", "select", "option",
    "svg", "math", "template", "noscript", "frame", "frameset", "applet",
})

# 每个标签允许保留的属性。``colspan`` / ``rowspan`` 是模板表格的骨架，
# 丢了合并单元格就会散架（``docx_to_html`` 刚把它们算出来）。
ALLOWED_ATTRS: dict[str, frozenset[str]] = {
    "p": frozenset({"style", "align"}),
    "div": frozenset({"style", "align"}),
    "span": frozenset({"style"}),
    "td": frozenset({"style", "colspan", "rowspan"}),
    "th": frozenset({"style", "colspan", "rowspan"}),
    "table": frozenset({"style"}),
    "tr": frozenset({"style"}),
    "ul": frozenset({"style"}), "ol": frozenset({"style"}), "li": frozenset({"style"}),
    "h1": frozenset({"style"}), "h2": frozenset({"style"}),
    "h3": frozenset({"style"}), "h4": frozenset({"style"}),
    "blockquote": frozenset({"style"}),
}
_DEFAULT_ATTRS: frozenset[str] = frozenset()

# 允许留在 style 里的 CSS 属性 —— 恰好是工具栏能改的那些。
ALLOWED_CSS = frozenset({
    "font-family", "font-size", "font-weight", "font-style",
    "color", "background-color", "text-decoration", "text-align",
    "text-indent", "margin-top", "margin-left", "line-height",
    "border", "border-collapse", "width",
})

# CSS 值里的危险构造。``url(`` 能外带请求（含 ``url(javascript:…)``），
# ``expression(`` 是老的 IE 执行面，``@import`` 能拉外部样式表。
_BAD_CSS_VALUE = re.compile(r"url\s*\(|expression\s*\(|@import|javascript:|vbscript:",
                            re.IGNORECASE)

# 只允许"属性名:值"的朴素声明；`\Z` 防止用换行/反斜杠拼接绕过
_CSS_DECL = re.compile(r"^\s*([a-zA-Z-]+)\s*:\s*([^;{}]*)\s*\Z")


def _clean_style(value: str) -> str:
    """过滤 ``style`` 属性的值，只留白名单属性 + 干净的值。"""
    kept: list[str] = []
    for decl in (value or "").split(";"):
        m = _CSS_DECL.match(decl)
        if not m:
            continue
        prop, val = m.group(1).strip().lower(), m.group(2).strip()
        if prop not in ALLOWED_CSS or not val:
            continue
        if _BAD_CSS_VALUE.search(val):
            logger.debug("净化器丢掉了可疑 CSS：%s: %s", prop, val[:60])
            continue
        kept.append(f"{prop}:{val}")
    return ";".join(kept)


def sanitize(html_text: str) -> str:
    """把任意 HTML 片段净化成白名单子集。输入不是 HTML 时原样返回。"""
    if not html_text or "<" not in html_text:
        return html_text or ""
    try:
        # create_parent 会套一层 div 当载体；lxml 同时会把 <p> 这类块级标签
        # 正常解析出来（fragment_fromstring 才是"只能有一个根"的那套）。
        root = lxml_html.fragment_fromstring(html_text, create_parent="div")
    except Exception:  # noqa: BLE001 解析不了的输入按纯文本返回，不能反过来丢内容
        logger.warning("富文本净化：HTML 解析失败，按纯文本处理")
        return lxml_html.escape(html_text)

    _walk(root)
    return _serialize(root)


def _serialize(root: Any) -> str:
    """把清理过的子树还原成 HTML 片段。

    ⚠️ **不能只遍历子元素。** ``drop_tag()`` 脱壳时会把文字提到**父节点的
    ``.text``/``.tail`` 上**，而这两者不是元素、迭代取不到 —— 于是
    ``<a>点我</a>`` 这种直接挂在根下的脱壳结果会被序列化成空串，**用户写的字
    就这么没了**（包在 ``<p>`` 里的那种碰巧没事，因为文字落进了 ``<p>`` 里，
    所以这个 bug 只在特定嵌套深度出现，很容易漏掉）。

    ``tostring`` 默认 ``with_tail=True``，所以 tail 不用另外拼。
    """
    parts: list[str] = []
    if root.text:
        # 裸文本要先转义：lxml 里存的是未转义的原文
        parts.append(escape(root.text, quote=False))
    for child in root:
        parts.append(lxml_html.tostring(child, encoding="unicode"))
    return "".join(parts)


def _walk(node: Any) -> None:
    """深度优先清理：丢危险标签、给其余标签脱壳或过滤属性。"""
    for child in list(node):
        tag = (child.tag or "").lower() if isinstance(child.tag, str) else ""

        if tag in DROP_TAGS:
            node.remove(child)
            continue

        # 注释 / CDATA / 处理指令：tag 不是字符串。IE 的条件注释可以当脚本用，
        # 一律丢掉。
        if not isinstance(child.tag, str):
            node.remove(child)
            continue

        _walk(child)  # 先清后代，再决定自己（脱壳时子树已经被清干净了）

        if tag not in ALLOWED_TAGS:
            # 脱壳：把子节点提到当前位置，标签本身不要。文字因此不会丢。
            child.drop_tag()
            continue

        allowed = ALLOWED_ATTRS.get(tag, _DEFAULT_ATTRS)
        for name in list(child.attrib):
            low = name.lower()
            if low not in allowed or low.startswith("on"):
                del child.attrib[name]
                continue
            if low == "style":
                cleaned = _clean_style(child.attrib[name])
                if cleaned:
                    child.attrib[name] = cleaned
                else:
                    del child.attrib[name]
            elif low in ("colspan", "rowspan"):
                # 只收正整数 —— 这两个值会被原样拼回 HTML 属性位
                if not child.attrib[name].strip().isdigit():
                    del child.attrib[name]
