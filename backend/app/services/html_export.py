"""把编辑页的富文本 HTML 导出成 Word / PDF。

## 为什么要单独一条导出链路

原有的导出链路是 **Markdown → Word/PDF**（``docs._write_docx`` / ``_write_pdf``）。
那条链路的前提是"正文由模板渲染器产出、结构已知"。但编辑页的正文是
``contenteditable`` 的 HTML —— 律师在页面上加了粗、调了字号、把落款推到右边，
这些信息**在 Markdown 里没有对应的写法**。继续走 Markdown 就等于：
页面上明明加粗了，下载下来是普通字（用户选的正是"要，所见即所得"）。

所以这里直接吃 HTML。

## 覆盖范围

**刻意的子集**：字体 / 字号 / 加粗 / 斜体 / 下划线 / 删除线 / 字色 /
对齐 / 首行缩进 / 表格（含合并单元格）。恰好是工具栏能改的那些 ——
工具做不到的样式不会出现在 HTML 里，写再全也是死代码。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, NavigableString, Tag

logger = logging.getLogger(__name__)

# 工具栏 `fontSize` 用的是 execCommand 的 1-7 档，要换成磅值。
# 这套映射沿用的是浏览器传统档位（3 = 默认 = 12pt 一档）。
_FONT_SIZE_SCALE = {1: 8, 2: 10, 3: 12, 4: 14, 5: 18, 6: 24, 7: 36}

# 会被当成块级段落的标签
_BLOCK_TAGS = frozenset({"p", "div", "h1", "h2", "h3", "h4", "blockquote", "li"})
# 行内语义标签 → 样式
_INLINE_TAGS: dict[str, dict[str, Any]] = {
    "b": {"bold": True}, "strong": {"bold": True},
    "i": {"italic": True}, "em": {"italic": True},
    "u": {"underline": True},
    "s": {"strike": True}, "strike": {"strike": True}, "del": {"strike": True},
    "sub": {"sub": True}, "sup": {"sup": True},
}


# --------------------------------------------------------------------------- 解析
def _parse_css(style: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for decl in (style or "").split(";"):
        if ":" not in decl:
            continue
        k, _, v = decl.partition(":")
        out[k.strip().lower()] = v.strip()
    return out


def _pt_of(value: str) -> float | None:
    """``"12pt"`` / ``"16px"`` → 磅。``px`` 按 0.75 折算（96dpi → 72dpi）。"""
    v = (value or "").strip().lower()
    try:
        if v.endswith("pt"):
            return float(v[:-2])
        if v.endswith("px"):
            return float(v[:-2]) * 0.75
    except ValueError:
        return None
    return None


def _style_of(el: Tag) -> dict[str, Any]:
    """把一个元素的"自己的"样式读出来（不含继承）。"""
    st: dict[str, Any] = dict(_INLINE_TAGS.get((el.name or "").lower(), {}))
    css = _parse_css(el.get("style") or "")

    if "font-family" in css:
        st["font"] = css["font-family"].strip().strip("'\"")
    size = _pt_of(css.get("font-size", ""))
    if size:
        st["size"] = size
    if css.get("font-weight", "").strip() in ("bold", "700", "800", "900"):
        st["bold"] = True
    if css.get("font-style", "").strip() == "italic":
        st["italic"] = True
    deco = css.get("text-decoration", "")
    if "underline" in deco:
        st["underline"] = True
    if "line-through" in deco:
        st["strike"] = True
    if "color" in css:
        st["color"] = css["color"].strip()

    # execCommand 的 fontSize 会产出 <font size="7">，要折算成磅
    if (el.name or "").lower() == "font":
        raw = (el.get("size") or "").strip().lstrip("+")
        if raw.isdigit() and int(raw) in _FONT_SIZE_SCALE:
            st["size"] = float(_FONT_SIZE_SCALE[int(raw)])
        if el.get("color"):
            st["color"] = el["color"]
        if el.get("face"):
            st["font"] = el["face"]
    return st


def _para_style_of(el: Tag) -> dict[str, Any]:
    """段落级样式：对齐 / 首行缩进。"""
    css = _parse_css(el.get("style") or "")
    out: dict[str, Any] = {}
    align = (css.get("text-align") or el.get("align") or "").strip().lower()
    if align in ("center", "right", "justify", "left"):
        out["align"] = align
    indent = _pt_of(css.get("text-indent", ""))
    if indent:
        out["indent"] = indent
    size = _pt_of(css.get("font-size", ""))
    if size:
        out["size"] = size
    return out


def _inline_runs(el: Tag, inherited: dict[str, Any] | None = None) -> list[tuple[str, dict[str, Any]]]:
    """把元素下的所有文本拍平成 ``[(文本, 生效样式)]``。

    样式靠**逐层下传**合并：``<span style="font-size:16pt"><b>劳动</b></span>``
    里的 ``劳动`` 要同时拿到 16pt 和加粗。只看最内层标签会丢掉外层样式。
    """
    inh = dict(inherited or {})
    out: list[tuple[str, dict[str, Any]]] = []
    for child in el.children:
        if isinstance(child, NavigableString):
            text = str(child)
            if text.strip() or text == " ":
                out.append((text, inh))
        elif isinstance(child, Tag):
            if (child.name or "").lower() == "br":
                out.append(("\n", inh))
                continue
            out.extend(_inline_runs(child, {**inh, **_style_of(child)}))
    return out


def _blocks(soup: Any) -> list[Tag]:
    """顶层块级元素，**按文档顺序**（段落与表格交错）。

    裸文本（不在任何块里）包一层 ``<p>`` —— 否则直接粘进来的文字会被整段丢掉。
    """
    out: list[Tag] = []
    for child in soup.children:
        if isinstance(child, Tag):
            if (child.name or "").lower() == "table":
                out.append(child)
            elif (child.name or "").lower() in _BLOCK_TAGS:
                out.append(child)
            elif (child.name or "").lower() in ("ul", "ol"):
                out.extend(c for c in child.find_all("li", recursive=False))
            else:
                out.append(child)
        elif isinstance(child, NavigableString) and str(child).strip():
            wrapper = soup.new_tag("p")
            wrapper.string = str(child)
            out.append(wrapper)
    return out


# --------------------------------------------------------------------------- Word
def _apply_run_style(run: Any, st: dict[str, Any]) -> None:
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    if st.get("font"):
        name = str(st["font"])
        run.font.name = name
        # 中文字体必须写进 ``w:eastAsia``，只设 font.name 对汉字不生效
        rPr = run._element.get_or_add_rPr()
        rf = rPr.get_or_add_rFonts()
        for attr in ("w:eastAsia", "w:ascii", "w:hAnsi"):
            rf.set(qn(attr), name)
    if st.get("size"):
        run.font.size = Pt(float(st["size"]))
    if st.get("bold"):
        run.font.bold = True
    if st.get("italic"):
        run.font.italic = True
    if st.get("underline"):
        run.font.underline = True
    if st.get("strike"):
        run.font.strike = True
    if st.get("sub"):
        run.font.subscript = True
    if st.get("sup"):
        run.font.superscript = True
    color = st.get("color")
    if color:
        rgb = _hex_color(color)
        if rgb:
            run.font.color.rgb = rgb


def _hex_color(value: str) -> Any:
    """``#rrggbb`` / ``rgb(r,g,b)`` → ``RGBColor``；认不出来就返回 None。"""
    from docx.shared import RGBColor

    v = (value or "").strip().lower()
    try:
        if v.startswith("#"):
            h = v[1:]
            if len(h) == 3:
                h = "".join(c * 2 for c in h)
            if len(h) == 6:
                return RGBColor.from_string(h.upper())
        if v.startswith("rgb("):
            parts = [int(float(x)) for x in v[4:-1].split(",")[:3]]
            return RGBColor(*parts)
    except (ValueError, TypeError):
        pass
    return None


def _write_paragraph(p: Any, el: Tag) -> None:
    """把一个块级元素的内容写进**已有的**段落对象。

    收段落而不是自己建，是为了让调用方能复用 ``Document()`` 预置的那个空段落 ——
    自己建的话文首会永远多出一个空行。
    """
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    ps = _para_style_of(el)
    align = ps.get("align")
    if align == "center":
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    elif align == "right":
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    elif align == "justify":
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    if ps.get("indent"):
        from docx.shared import Pt

        p.paragraph_format.first_line_indent = Pt(float(ps["indent"]))

    runs = _inline_runs(el)
    if not runs:
        return
    # 段落级字号作为兜底：`<p style="font-size:12pt">文字</p>` 里文字没有
    # 自己的 span，样式写在段落上，不兜底就会退回 Word 默认的 10.5pt
    for text, st in runs:
        if not text:
            continue
        merged = {**({"size": ps["size"]} if ps.get("size") else {}), **st}
        for i, chunk in enumerate(text.split("\n")):
            if i:
                p.add_run().add_break()
            if chunk:
                run = p.add_run(chunk)
                _apply_run_style(run, merged)


# 网格里的两种"非锚点"位置。**必须区分开**：
#   _TAKEN = 被上方 rowspan 或左侧 colspan 占掉的位置（新格子要跳过它）
#   _EMPTY = 还没排到的空位（新格子要落在这里）
# 两者都用 None 表示的话，"跳过被占的列"就失去判据 —— 续行上的格子会把
# 上一行 rowspan 的位置**直接覆盖掉**，导出成一张错位的表。
_TAKEN = object()
_EMPTY = object()


def _int_attr(el: Tag, name: str, default: int) -> int:
    raw = (el.get(name) or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


def _table_grid(table_el: Tag) -> tuple[int, int, list[tuple[int, int, int, int, Tag]]]:
    """HTML 表格 → ``(行数, 列数, [(起始行, 起始列, 跨行, 跨列, 单元格元素)])``。

    这里做的正是 ``docx_html`` 里那套的**逆运算**（那边 span → 网格，
    这边网格 → 合并目标），两边对数不上就会导出成错位的表。
    """
    grid: list[list[Any]] = []

    def put(ri: int, ci: int, val: Any) -> None:
        while len(grid) <= ri:
            grid.append([])
        row = grid[ri]
        while len(row) <= ci:
            row.append(_EMPTY)
        row[ci] = val

    anchors: list[tuple[int, int, int, int, Tag]] = []
    for ri, tr in enumerate(table_el.find_all("tr")):
        col = 0
        for cell in tr.find_all(["td", "th"], recursive=False):
            # 跳过被上方 rowspan 占掉的位置
            while ri < len(grid) and col < len(grid[ri]) and grid[ri][col] is _TAKEN:
                col += 1
            cs = _int_attr(cell, "colspan", 1)
            rs = _int_attr(cell, "rowspan", 1)
            anchors.append((ri, col, rs, cs, cell))
            for dr in range(rs):
                for dc in range(cs):
                    put(ri + dr, col + dc, cell if (dr == 0 and dc == 0) else _TAKEN)
            col += cs

    return len(grid), max((len(r) for r in grid), default=0), anchors


def _add_table(doc: Any, el: Tag) -> None:
    nrows, ncols, anchors = _table_grid(el)
    if not nrows or not ncols:
        return
    table = doc.add_table(rows=nrows, cols=ncols)
    try:
        table.style = "Table Grid"      # 与渲染端的"有边框才画"对应
    except KeyError:  # noqa: PERF203 模板里没有这个样式名时静默降级为无框
        logger.debug("python-docx 默认模板里没有 Table Grid 样式，导出为无框表")

    # 先全部合并、再统一写内容：写一半再合并会让 python-docx 的网格簿记错位。
    # 锚点之间互不覆盖（被合并的位置永远不是锚点），所以这个顺序是安全的。
    targets: list[tuple[Any, Tag]] = []
    for ri, ci, rs, cs, cell_el in anchors:
        target = table.cell(ri, ci)
        if rs > 1 or cs > 1:
            target = target.merge(table.cell(ri + rs - 1, ci + cs - 1))
        targets.append((target, cell_el))
    for target, cell_el in targets:
        _fill_cell(target, cell_el)


def _fill_cell(cell: Any, el: Tag | None) -> None:
    """把单元格内容写进去。python-docx 建表时每格自带一个空段落，要复用它。"""
    cell.text = ""
    if el is None:
        return
    spare = cell.paragraphs[0]
    inner = el.find_all(_BLOCK_TAGS, recursive=False)
    if not inner:
        # 单元格里直接是文字/行内标签，没有 <p> 包着
        _write_runs_into(spare, _inline_runs(el), {})
        return
    for b in inner:
        if spare is not None:
            p, spare = spare, None
        else:
            p = cell.add_paragraph()
        _write_paragraph(p, b)


def _write_runs_into(p: Any, runs: list[tuple[str, dict[str, Any]]], ps: dict[str, Any]) -> None:
    for text, st in runs:
        if not text:
            continue
        merged = {**({"size": ps["size"]} if ps.get("size") else {}), **st}
        for i, chunk in enumerate(text.split("\n")):
            if i:
                p.add_run().add_break()
            if chunk:
                _apply_run_style(p.add_run(chunk), merged)


def _apply_page_setup(doc: Any, template_id: str) -> None:
    """把原模板的纸张大小与页边距抄过来。

    **不抄的话**：律师改了一个字再导出，页面就从 A4 变成 Letter、
    页边距也从 1.25 英寸变成 1 英寸 —— 打印出来才发现对不上归档要求。
    """
    if not template_id:
        return
    try:
        from docx import Document

        from backend.app.services import template_service as ts

        tpl = ts.get_template(template_id)
        if tpl is None:
            return
        src = ts.template_dir() / tpl["file"]
        if not src.exists():
            return
        s = Document(str(src)).sections[0]
        d = doc.sections[0]
        if s.page_width:
            d.page_width, d.page_height = s.page_width, s.page_height
        if s.left_margin:
            d.left_margin, d.right_margin = s.left_margin, s.right_margin
        if s.top_margin:
            d.top_margin, d.bottom_margin = s.top_margin, s.bottom_margin
    except Exception:  # noqa: BLE001 拿不到模板就退回默认纸张，不该因此导出失败
        logger.warning("抄模板页面设置失败（template_id=%s），退回默认纸张", template_id)


def register_cjk_fonts() -> str:
    """注册系统中文字体，返回正文字体名。找不到就抛 ``ImportError``。

    与 ``docs._write_pdf`` 共用（原先两边各写一份，改一处忘一处就会有一端
    输出成豆腐块）。**找不到字体时明确报错**，而不是静默输出一堆 tofu ——
    律师拿到一份全是方块的 PDF，会以为是我们的排版坏了。
    """
    from pathlib import Path as _Path

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for name, path in (("Chinese", r"C:\Windows\Fonts\simsun.ttc"),
                       ("Chinese", r"C:\Windows\Fonts\msyh.ttc")):
        if _Path(path).exists():
            try:
                pdfmetrics.registerFont(TTFont(name, path))
                break
            except Exception:  # noqa: BLE001 换下一个候选
                continue
    else:
        raise ImportError("未找到系统自带中文字体（simsun.ttc / msyh.ttc），无法导出 PDF。")

    for name, path in (("Chinese-Bold", r"C:\Windows\Fonts\simsunb.ttf"),
                       ("Chinese-Bold", r"C:\Windows\Fonts\msyhbd.ttc")):
        if _Path(path).exists():
            try:
                pdfmetrics.registerFont(TTFont(name, path))
            except Exception:  # noqa: BLE001 粗体缺失时退回常规体，不该中断导出
                pass
            break
    return "Chinese"


def _pdf_inline(el: Tag, inherited: dict[str, Any] | None = None) -> str:
    """HTML 行内内容 → reportlab 的 Paragraph 标记子集。

    reportlab **不认 CSS**，只认 ``<b> <i> <u> <strike> <font>`` 这几个标签，
    所以这里做一次翻译。不认识的样式丢掉而不是原样透传 —— 透传会让
    reportlab 抛异常、整份 PDF 导不出来。
    """
    from xml.sax.saxutils import escape

    inh = dict(inherited or {})
    out: list[str] = []
    for child in el.children:
        if isinstance(child, NavigableString):
            out.append(escape(str(child)))
            continue
        if not isinstance(child, Tag):
            continue
        if (child.name or "").lower() == "br":
            out.append("<br/>")
            continue
        st = {**inh, **_style_of(child)}
        inner = _pdf_inline(child, st)
        if not inner:
            continue
        if st.get("bold"):
            inner = f"<b>{inner}</b>"
        if st.get("italic"):
            inner = f"<i>{inner}</i>"
        if st.get("underline"):
            inner = f"<u>{inner}</u>"
        if st.get("strike"):
            inner = f"<strike>{inner}</strike>"
        if st.get("size"):
            inner = f'<font size="{float(st["size"]):g}">{inner}</font>'
        out.append(inner)
    return "".join(out)


def write_pdf_from_html(target: Path, html_text: str, title: str = "",
                        citations: list[dict[str, Any]] | None = None) -> None:
    """编辑过的正文 → PDF。保对齐 / 缩进 / 字号 / 粗斜下划 / 表格。"""
    from reportlab.lib import enums
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    base = register_cjk_fonts()
    # 中文 TrueType 里没有 ☐/☑ 的字形，会印成空白方块；换成确实存在的 □/■
    soup = BeautifulSoup((html_text or "").replace("☑", "■").replace("☐", "□"),
                         "html.parser")

    body_style = ParagraphStyle("h_body", fontName=base, fontSize=12, leading=20,
                                wordWrap="CJK")
    align_map = {"center": enums.TA_CENTER, "right": enums.TA_RIGHT,
                 "justify": enums.TA_JUSTIFY, "left": enums.TA_LEFT}

    flow: list[Any] = []
    for el in _blocks(soup):
        if (el.name or "").lower() == "table":
            data, spans, ncols = _pdf_table_data(el)
            if data:
                t = Table(data, colWidths=[A4[0] / max(1, ncols) * 0.92] * ncols)
                style = [("GRID", (0, 0), (-1, -1), 0.5, (0, 0, 0)),
                         ("VALIGN", (0, 0), (-1, -1), "TOP"),
                         ("FONTNAME", (0, 0), (-1, -1), base),
                         ("FONTSIZE", (0, 0), (-1, -1), 10)]
                style += [("SPAN", (c1, r1), (c2, r2)) for c1, r1, c2, r2 in spans]
                t.setStyle(TableStyle(style))
                flow.append(t)
                flow.append(Spacer(1, 8))
            continue

        ps = _para_style_of(el)
        inner = _pdf_inline(el)
        if not inner.strip():
            flow.append(Spacer(1, 6))
            continue
        style = ParagraphStyle(
            f"h_{len(flow)}", parent=body_style,
            alignment=align_map.get(ps.get("align", ""), enums.TA_LEFT),
            firstLineIndent=(ps.get("indent") or 0),
        )
        flow.append(Paragraph(inner, style))

    if citations:
        flow.append(Spacer(1, 14))
        flow.append(Paragraph("引用依据", ParagraphStyle(
            "h_cite_t", parent=body_style, fontSize=14)))
        for i, c in enumerate(citations, 1):
            name = c.get("name") or c.get("doc_name") or ""
            article = c.get("article") or c.get("article_no") or ""
            flag = "" if c.get("verified") else "（未通过核验，仅供核对）"
            from xml.sax.saxutils import escape as _e

            flow.append(Paragraph(
                f"{i}. {_e(str(name))}{_e(str(article))}　"
                f"{_e(str(c.get('status') or ''))}{flag}",
                ParagraphStyle(f"h_cite_{i}", parent=body_style, fontSize=10)))

    SimpleDocTemplate(str(target), pagesize=A4, title=title or "").build(flow)


def _pdf_table_data(el: Tag) -> tuple[list[list[Any]], list[tuple[int, int, int, int]], int]:
    """表格 → reportlab 的 ``(data, spans, ncols)``。

    用与 docx 同一个 ``_table_grid``：合并的对数在两条导出链上必须一致，
    各算各的迟早出现"Word 对、PDF 不对"。
    """
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph

    nrows, ncols, anchors = _table_grid(el)
    if not nrows or not ncols:
        return [], [], 0
    cell_style = ParagraphStyle("t1", fontName="Chinese", fontSize=10,
                                leading=14, wordWrap="CJK")
    data: list[list[Any]] = [["" for _ in range(ncols)] for _ in range(nrows)]
    spans: list[tuple[int, int, int, int]] = []
    for ri, ci, rs, cs, cell_el in anchors:
        text = "".join(t for t, _s in _inline_runs(cell_el)).strip()
        data[ri][ci] = Paragraph(text or "&nbsp;", cell_style)
        if rs > 1 or cs > 1:
            spans.append((ci, ri, ci + cs - 1, ri + rs - 1))
    return data, spans, ncols


def _cite_run(paragraph: Any, text: str, *, size: int = 12, bold: bool = False) -> None:
    """引用清单用的一行。**不复用 ``docs._run``** —— html_export 被 docs 导入，
    反过来导入它就成了循环依赖（虽然靠延迟导入能跑，但很脆）。"""
    from docx.oxml.ns import qn
    from docx.shared import Pt

    if not text:
        return
    run = paragraph.add_run(text)
    run.font.size = Pt(size)
    run.bold = bold
    run.font.name = "宋体"
    rPr = run._element.get_or_add_rPr()
    rPr.get_or_add_rFonts().set(qn("w:eastAsia"), "宋体")


def write_docx_from_html(
    target: Path, html_text: str, template_id: str = "",
    citations: list[dict[str, Any]] | None = None,
) -> None:
    """编辑过的正文 → .docx。"""
    from docx import Document

    doc = Document()
    _apply_page_setup(doc, template_id)

    soup = BeautifulSoup(html_text or "", "html.parser")
    # ``Document()`` 新建出来**一个段落都没有**（不是"自带一个空的"）——
    # 所以按块 add_paragraph 就行，文首不会多空行，也不需要复用谁的占位段。
    for el in _blocks(soup):
        if (el.name or "").lower() == "table":
            _add_table(doc, el)
            continue
        _write_paragraph(doc.add_paragraph(), el)

    if citations:
        doc.add_page_break()
        _cite_run(doc.add_paragraph(), "引用依据", size=14, bold=True)
        for i, c in enumerate(citations, 1):
            name = c.get("name") or c.get("doc_name") or ""
            article = c.get("article") or c.get("article_no") or ""
            flag = "" if c.get("verified") else "（未通过核验，仅供核对）"
            _cite_run(doc.add_paragraph(),
                      f"{i}. {name}{article}　{c.get('status') or ''}{flag}", size=10)

    doc.save(str(target))
