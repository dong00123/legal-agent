"""把 .docx 渲染成 HTML —— 为了**保住模板的版式**。

## 为什么不复用 `template_service._iter_body` / `render_markdown`

那一条链路是**为"抽字段"设计的**，它的取舍和渲染恰好相反：

| | `_iter_body`（抽字段） | 本模块（渲染） |
|---|---|---|
| 空段落 | 跳过（没有可填的东西） | **保留**（模板靠它撑竖向节奏） |
| 表格 | 拆成 `label` + 第 2 列，**第 3 列起丢弃** | 原样重建 `<table>`，含合并格 |
| 字体字号 | 不读 | 必读（黑体 16pt 标题、宋体 12pt 正文） |
| 首行缩进 | 不读 | 必读（正文靠它缩两字） |

用户报的「模版生成出来跟我的模板不一样」正是这张表的左列造成的：Markdown 的语法
**装不下**表格边框、合并单元格、字体、字号、首行缩进、页边距。所以这里另起一条链路，
两条并存、互不改。

## 为什么不引 mammoth / pandoc

环境里 `python-docx` + `lxml` 已经够用（实测能读出上面全部属性），而引一个转换库
要动 `requirements.lock.txt`，还要对付它的字体/样式映射策略 —— 我们只需要"把模板
已有的版式如实搬过来"，不需要它替我们做归一化。

## 安全

**所有文本一律 `html.escape`。** 这里的文本不只是模板自带的固定条款，还包括
`fills` 里**模型生成**的字段值（如"事实与理由"整段）。直接拼进 HTML 就是注入面 ——
前端会把它塞进 `contenteditable` 并回存。
"""

from __future__ import annotations

import html
import logging
from typing import Any

from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph
from docx.text.run import Run

from backend.app.services.template_service import _norm_align

logger = logging.getLogger(__name__)

# OOXML 主命名空间。docx 是"带命名空间的 XML"，python-docx 只包了常用属性，
# 边框 / 合并 / 底纹这些要自己按 qn 找。
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# 1 磅 = 12700 EMU（python-docx 的长度单位）。缩进/边距都存 EMU。
_EMU_PER_PT = 12700
_EMU_PER_IN = 914400


# --------------------------------------------------------------------------- 小工具
def _esc(text: str | None) -> str:
    """文本节点转义。``quote=False``：只转 ``& < >``，别把引号也转成实体，
    否则正文里正常的引号在页面上会显示成 ``&quot;``。"""
    return html.escape(text or "", quote=False)


def _pt(emu: Any) -> float | None:
    """EMU → 磅。``None`` / 0 都返回 ``None``（0 缩进没有渲染意义，不必写进 style）。"""
    if not emu:
        return None
    return round(int(emu) / _EMU_PER_PT, 1)


def _style(parts: list[str]) -> str:
    body = ";".join(p for p in parts if p)
    return f' style="{body}"' if body else ""


def _run_font(run: Run) -> str | None:
    """取运行的中文字体名。

    **必须读 run 级 ``rFonts`` 的 ``w:eastAsia``。** 段落样式（``Normal``）在这个
    环境里是 ``Calibri 10.5pt``，跟中文模板的实际观感完全不是一回事 ——
    实测 `劳动争议纠纷起诉状.docx` 的 ``Normal`` 是 Calibri，而正文真正的字体
    ``宋体``、标题 ``方正小标宋简体`` 全写在每个 run 上。
    """
    rPr = run._element.rPr
    if rPr is None or rPr.rFonts is None:
        return None
    rf = rPr.rFonts
    return (rf.get(f"{_W}eastAsia") or rf.get(f"{_W}ascii")
            or rf.get(f"{_W}hAnsi") or None)


def _run_html(run: Run) -> str:
    """一个 run → ``<span style="…">文本</span>``（无样式时退化成裸文本）。"""
    text = run.text or ""
    if not text:
        return ""

    parts: list[str] = []
    font = _run_font(run)
    if font:
        # 字体名可能带引号（如 '仿宋_GB2312'），用双引号包住并转义内部的引号
        parts.append(f"font-family:'{font.replace(chr(39), '')}'")

    f = run.font
    if f.size is not None:
        parts.append(f"font-size:{f.size.pt:g}pt")
    if f.bold:
        parts.append("font-weight:700")
    if f.italic:
        parts.append("font-style:italic")

    # 下划线 / 删除线共用一个 CSS 属性，要合并而不是各写一遍（后者会互相覆盖）
    deco = []
    if f.underline:
        deco.append("underline")
    if f.strike:
        deco.append("line-through")
    if deco:
        parts.append("text-decoration:" + " ".join(deco))

    try:
        rgb = f.color.rgb if f.color is not None else None
        # 黑色是默认值，写出来只会让 HTML 变长；主题色取 .rgb 会抛，故整段 try
        if rgb is not None and str(rgb).upper() not in ("000000",):
            parts.append(f"color:#{rgb}")
    except Exception:  # noqa: BLE001 主题色/自动色取值失败很正常，不值得中断渲染
        pass

    inner = _esc(text)
    return f"<span{_style(parts)}>{inner}</span>" if parts else inner


def _para_inner(p: Paragraph) -> str:
    """段落内容。

    **不用 ``p.runs``**：它只返回 ``w:p`` 的直接 ``w:r`` 子元素，落在
    ``w:hyperlink`` 里的 run 会被漏掉（超链接文本直接消失）。
    """
    out: list[str] = []
    for child in p._p.iterchildren():
        if child.tag == f"{_W}r":
            out.append(_run_html(Run(child, p)))
        elif child.tag == f"{_W}hyperlink":
            for rc in child.iterchildren(f"{_W}r"):
                out.append(_run_html(Run(rc, p)))
        elif child.tag == f"{_W}br":
            out.append("<br/>")
    return "".join(out)


def _para_html(p: Paragraph) -> str:
    """段落 → ``<p>``。空段落也输出（``<br/>`` 撑着），否则竖向节奏全塌。"""
    raw = p.text or ""
    inner = _para_inner(p)
    if not inner:
        return '<p><br/></p>'

    parts: list[str] = []
    align = _norm_align(p.alignment, raw)
    if align in ("center", "right"):
        parts.append(f"text-align:{align}")

    pf = p.paragraph_format
    # 右对齐时不写 text-indent：CSS 的 text-indent 会把右对齐那一行的**右边缘**
    # 也往左推，而 Word 里右对齐的文本永远贴着右边界、首行缩进在那个方向上不可见。
    # 两者一起写就是"缩两次"，落款会比模板更靠左。
    indent = None if align == "right" else _pt(pf.first_line_indent)
    if indent:
        parts.append(f"text-indent:{indent:g}pt")
    before = _pt(pf.space_before)
    if before:
        parts.append(f"margin-top:{before:g}pt")
    if align == "right":
        # 右对齐 + 模板自带的前导空格 = 双倍缩进。归一成右对齐后要把那些
        # 「手推」出来的空格去掉，否则落款会比预期更靠左。
        inner = inner.lstrip()
    return f"<p{_style(parts)}>{inner}</p>"


# --------------------------------------------------------------------------- 表格
def _span_of(tc: Any) -> int:
    """``gridSpan`` → colspan，缺省 1。"""
    tcPr = tc.find(f"{_W}tcPr")
    if tcPr is None:
        return 1
    gs = tcPr.find(f"{_W}gridSpan")
    if gs is None:
        return 1
    try:
        return max(1, int(gs.get(f"{_W}val") or 1))
    except (TypeError, ValueError):
        return 1


def _vmerge_of(tc: Any) -> str | None:
    """``vMerge`` 状态：``"restart"``（起始格）/ ``"continue"``（被合并掉的续格）/ ``None``。

    ``<w:vMerge/>`` 不带 ``val`` 就是 ``continue`` —— 属性缺省值不是"没有"。
    """
    tcPr = tc.find(f"{_W}tcPr")
    if tcPr is None:
        return None
    vm = tcPr.find(f"{_W}vMerge")
    if vm is None:
        return None
    return vm.get(f"{_W}val") or "continue"


def _border_from(holder: Any) -> str:
    """从持有 ``w:tblBorders`` 的元素里读出边框 CSS。没有声明就返回空串。"""
    if holder is None:
        return ""
    borders = holder.find(f"{_W}tblBorders")
    if borders is None:
        return ""
    for side in borders:
        # `nil` 和 `none` 都是"这条边不画"；val 缺省（空串）当没声明处理
        val = (side.get(f"{_W}val") or "").lower()
        if val and val not in ("none", "nil"):
            sz = side.get(f"{_W}sz")
            try:
                width = max(1, round(int(sz) / 8)) if sz else 1  # sz 单位是 1/8 磅
            except (TypeError, ValueError):
                width = 1
            return f"{width}px solid #000"
    return ""


def _table_border(table: Table) -> str:
    """表格边框 → CSS。模板没声明边框时返回空（不要凭空加线）。

    先看表格自身的 ``tblPr``，**再退回表格样式**：Word 里"给表加框线"最常用的
    操作是套用内置的「网格型」样式，此时 ``tblBorders`` 写在 ``styles.xml`` 的
    样式里、表格自己一个边框属性都没有。只看表格自身的话，这类模板的框线
    会整片消失 —— 而框线正是表单型起诉状的骨架。

    ⚠️ 两处坑，都踩过：``style.element`` 上**没有** ``tblPr`` 这个 Python 属性
    （``CT_Style`` 没定义它），只能用 ``find``；而 ``find`` 只找**直接子元素**，
    ``tblBorders`` 是 ``w:style`` 的孙子，得先进 ``w:tblPr`` 再取。
    少进这一层就永远返回空 —— 边框静默消失，而这正是本模块要防的失败方式。
    """
    css = _border_from(table._tbl.tblPr)
    if css:
        return css
    try:
        style = table.style
        el = style.element if style is not None else None
        return _border_from(el.find(f"{_W}tblPr") if el is not None else None)
    except Exception:  # noqa: BLE001 样式名指向不存在的样式时会抛，不值得中断渲染
        return ""


def _rows_layout(table: Table) -> list[list[tuple[Any, int, str | None]]]:
    """整表布局 → ``[[(cell, colspan, vmerge), …], …]``。

    **直接遍历 ``w:tc`` 元素，不能用 ``row.cells``。** python-docx 的
    ``_Row.cells`` 会按表格网格做映射，把纵向合并里"被合并掉"的那个续格
    **还原成它上面那一行的起始格**（同一个 ``w:tc``）。于是 ``vMerge`` 状态
    在两行里读出来都是 ``restart``、"续格"永远不存在 —— 纵向合并就静默失效，
    渲染成两行各写一遍重复文本。``gridSpan`` 同理（重复格要按 ``w:tc`` 去重）。
    所以这里绕开那层糖，自己数网格列。
    """
    rows: list[list[tuple[Any, int, str | None]]] = []
    for tr in table._tbl.iterchildren(f"{_W}tr"):
        row: list[tuple[Any, int, str | None]] = []
        for tc in tr.iterchildren(f"{_W}tc"):
            row.append((_Cell(tc, table), _span_of(tc), _vmerge_of(tc)))
        rows.append(row)
    return rows


def _table_html(table: Table) -> str:
    layouts = _rows_layout(table)
    # 每行每个格子的"网格起始列"，用来判断纵向合并是否落在同一列
    starts: list[list[int]] = []
    for lay in layouts:
        pos, acc = [], 0
        for _c, span, _vm in lay:
            pos.append(acc)
            acc += span
        starts.append(pos)

    def continues_at(row_idx: int, grid_col: int) -> bool:
        return any(p == grid_col and vm == "continue"
                   for (_c, _s, vm), p in zip(layouts[row_idx], starts[row_idx]))

    rows_html: list[str] = []
    for ri, lay in enumerate(layouts):
        tds: list[str] = []
        for (cell, span, vm), gc in zip(lay, starts[ri]):
            if vm == "continue":
                continue  # 已被上面那格的 rowspan 吃掉
            attrs = []
            if span > 1:
                attrs.append(f' colspan="{span}"')
            if vm == "restart":
                n = 1
                while ri + n < len(layouts) and continues_at(ri + n, gc):
                    n += 1
                if n > 1:
                    attrs.append(f' rowspan="{n}"')

            css: list[str] = []
            try:
                width = _pt(cell.width)
            except Exception:  # noqa: BLE001 少数模板的 tcW 是百分比/auto
                width = None
            if width:
                css.append(f"width:{width:g}pt")
            body = "".join(_para_html(p) for p in cell.paragraphs) or "<p><br/></p>"
            tds.append(f"<td{''.join(attrs)}{_style(css)}>{body}</td>")
        rows_html.append("<tr>" + "".join(tds) + "</tr>")

    border = _table_border(table)
    css = ["border-collapse:collapse", "width:100%"]
    if border:
        css.append(f"border:{border}")
    return f'<table{_style(css)}>' + "".join(rows_html) + "</table>"


# --------------------------------------------------------------------------- 入口
def page_geometry(doc: Any) -> dict[str, Any]:
    """页面尺寸与页边距（英寸）。

    预览要按这个把正文渲染成"一张纸"，否则 A4 的起诉状和 Letter 的仲裁申请书
    在页面上长得一样、跟导出结果对不上。
    """
    try:
        sec = doc.sections[0]
    except (IndexError, AttributeError):
        return {}
    return {
        "width": round(sec.page_width / _EMU_PER_IN, 3) if sec.page_width else None,
        "height": round(sec.page_height / _EMU_PER_IN, 3) if sec.page_height else None,
        "margin_top": round(sec.top_margin / _EMU_PER_IN, 3) if sec.top_margin else None,
        "margin_right": round(sec.right_margin / _EMU_PER_IN, 3) if sec.right_margin else None,
        "margin_bottom": round(sec.bottom_margin / _EMU_PER_IN, 3) if sec.bottom_margin else None,
        "margin_left": round(sec.left_margin / _EMU_PER_IN, 3) if sec.left_margin else None,
    }


def docx_to_html(doc: Any) -> str:
    """整篇 .docx → HTML。

    **按 ``body`` 的子元素顺序遍历**，段落与表格交错还原 —— 表单型模板（起诉状）
    是"表里有段、段间有表"，按 `doc.paragraphs` + `doc.tables` 分开取会把顺序打乱。
    """
    out: list[str] = []
    for child in doc.element.body.iterchildren():
        if child.tag == f"{_W}p":
            out.append(_para_html(Paragraph(child, doc)))
        elif child.tag == f"{_W}tbl":
            out.append(_table_html(Table(child, doc)))
    return "\n".join(out)
