"""``docx_to_html`` 的保真度与安全性。

**为什么这个测试值得单独存在。** 渲染器的失败方式全都是"安静"的 ——
字号丢了、表格合并没了、空段落被吞了，HTML 照样合法、页面照样显示，
只是长得不像那份模板。而用户提出的正是「模版严格按照真正的生成」。
所以这里逐条钉住每一种"会静默丢东西"的情况。

安全那条同样重要：``fills`` 是 LLM 产出的字段值，会经这个渲染器
变成 HTML、进 ``contenteditable``、再存库渲染回来。
"""

from __future__ import annotations

import re

import pytest
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt

from backend.app.services.docx_html import docx_to_html, page_geometry

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _cjk(run, font: str, size: float | None = None) -> None:
    """给 run 设中文字体（run 级 ``rFonts/@w:eastAsia``，模板的真实存法）。"""
    rPr = run._element.get_or_add_rPr()
    rPr.get_or_add_rFonts().set(qn("w:eastAsia"), font)
    if size is not None:
        run.font.size = Pt(size)


# --------------------------------------------------------------------------- 转义
def test_escapes_llm_filled_text() -> None:
    """``fills`` 里的模型输出必须被转义。

    这条不是假想威胁：``fills`` 的值由 LLM 生成，而渲染结果会被前端塞进
    ``contenteditable``、存库、再渲染回来。不转义就是一个存储型 XSS。
    """
    doc = Document()
    doc.add_paragraph().add_run("<script>alert(1)</script>")
    doc.add_paragraph().add_run('"><img src=x onerror=alert(2)>')

    h = docx_to_html(doc)
    assert "<script>" not in h and "&lt;script&gt;" in h
    assert "<img" not in h and "&lt;img" in h
    # 属性闭合逃逸：`">` 那个引号也不能原样落地
    assert 'onerror' in h and "<img" not in h


def test_escapes_ampersand_without_mangling_quotes() -> None:
    """``&`` 要转义，但正文里的引号不该变成 ``&quot;``。

    法律文书里引号很常见（「《劳动合同法》」条款引用、当事人陈述），
    转成实体虽然浏览器显示一样，但存进库的 HTML 会变得很难读、
    也让"内容没变"的比较逻辑失真。
    """
    doc = Document()
    doc.add_paragraph().add_run('A&B 公司称"已结清"')
    h = docx_to_html(doc)
    assert "&amp;" in h
    assert "&quot;" not in h
    assert '"已结清"' in h


# --------------------------------------------------------------------------- 段落
def test_keeps_empty_paragraphs() -> None:
    """空段落必须保留 —— 模板靠它们撑竖向节奏。

    抽字段那条链路（``_iter_body``）会跳过空段落，渲染不能跟着跳：
    起诉状的标题与正文之间、落款之前都靠空行分隔。
    """
    doc = Document()
    doc.add_paragraph("甲")
    doc.add_paragraph("")          # 纯空
    doc.add_paragraph("   ")       # 只有空格
    doc.add_paragraph("乙")

    h = docx_to_html(doc)
    assert h.count("<p") == 4, f"空段落被吞了：{h}"
    assert "<br/>" in h, "空段落要有 <br/> 撑着，否则高度塌成 0"


def test_reads_run_level_cjk_font_and_size() -> None:
    """字体字号要读 **run 级** ``rFonts/@w:eastAsia``。

    模板的段落样式（``Normal``）在这个环境里是 Calibri 10.5pt，
    跟中文模板的实际观感完全不是一回事 —— 只读样式会渲染成错误的字体。
    """
    doc = Document()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _cjk(p.add_run("劳动仲裁申请书"), "黑体", 16)

    h = docx_to_html(doc)
    assert "font-family:'黑体'" in h
    assert "font-size:16pt" in h
    assert "text-align:center" in h


def test_first_line_indent_is_emitted_in_points() -> None:
    """首行缩进 EMU → 磅。``Length`` 是 EMU，直接写数字会缩成 0。"""
    doc = Document()
    p = doc.add_paragraph()
    p.paragraph_format.first_line_indent = Pt(24.1)
    _cjk(p.add_run("事实与理由"), "宋体", 12)

    h = docx_to_html(doc)
    m = re.search(r"text-indent:([\d.]+)pt", h)
    assert m, f"首行缩进丢了：{h}"
    assert abs(float(m.group(1)) - 24.1) < 0.15, f"缩进值不对：{m.group(1)}pt"


def test_center_with_leading_spaces_becomes_right_aligned() -> None:
    """「居中 + 手打一串空格」要归一成右对齐，并把那些空格去掉。

    落款在模板里是 ``CENTER`` + 11 个空格推出来的假右下角。HTML 折叠连续空格，
    原样渲染就会掉回最左边 —— 这正是用户报过的"署名日期没有在右下角"。
    """
    doc = Document()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.add_run("           具状人（签字、盖章）：")

    h = docx_to_html(doc)
    assert "text-align:right" in h, f"没归一成右对齐：{h}"
    assert "text-align:center" not in h
    assert "  " not in h, "归一成右对齐后前导空格要清掉，否则缩两次"


def test_right_aligned_paragraph_drops_first_line_indent() -> None:
    """右对齐段落不写 ``text-indent``。

    CSS 的 ``text-indent`` 会把右对齐那一行的**右边缘**也往左推，
    而 Word 里右对齐文本永远贴着右边界。两个一起写 = 缩两次。
    """
    doc = Document()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    p.paragraph_format.first_line_indent = Pt(60)
    p.add_run("落款")

    h = docx_to_html(doc)
    assert "text-align:right" in h
    assert "text-indent" not in h, f"右对齐还带着缩进，落款会偏左：{h}"


# --------------------------------------------------------------------------- 表格
def test_table_keeps_colspan_and_border() -> None:
    """横向合并与边框要还原。

    抽字段链路只取 ``cells[0]`` / ``cells[1]``，第 3 列起直接丢弃、结构全塌 ——
    渲染必须走另一条路，这条测试就是那道分界线的守卫。
    """
    doc = Document()
    t = doc.add_table(rows=2, cols=3)
    t.style = "Table Grid"  # 带 tblBorders
    t.cell(0, 0).merge(t.cell(0, 1)).text = "当事人信息"
    t.cell(1, 0).text = "姓名"
    t.cell(1, 1).text = "张三"
    t.cell(1, 2).text = "性别"

    h = docx_to_html(doc)
    assert "<table" in h
    assert 'colspan="2"' in h, f"横向合并丢了：{h}"
    assert "border:" in h, "表格边框丢了"
    assert "张三" in h and "性别" in h, "第 3 列被丢了 —— 又走回抽字段那条路了"


def test_vertical_merge_becomes_rowspan() -> None:
    """``vMerge`` → ``rowspan``，续格要被吃掉。

    **现有 35 份模板里没有一份用纵向合并**，所以这条分支在真实语料上永远不会
    被走到 —— 也就意味着它写错了也没人会发现，直到有人放进来一份带纵向合并的
    模板。这里手工造一份，把行数数对。
    """
    doc = Document()
    t = doc.add_table(rows=3, cols=2)
    t.cell(0, 0).text = "项目"
    t.cell(0, 1).text = "内容"
    t.cell(1, 0).merge(t.cell(2, 0)).text = "事实与理由"
    t.cell(1, 1).text = "第一段"
    t.cell(2, 1).text = "第二段"

    h = docx_to_html(doc)
    assert 'rowspan="2"' in h, f"纵向合并没数出行数：{h}"
    # 续格不能再产出 <td>，否则这一行会多出一格、把后面的列挤歪
    assert h.count("<tr>") == 3, f"行数不对：{h}"
    assert "事实与理由" in h


def test_table_cell_paragraphs_are_rendered() -> None:
    """单元格里可能有多个段落，不能只取第一个。"""
    doc = Document()
    t = doc.add_table(rows=1, cols=1)
    c = t.cell(0, 0)
    c.text = "第一行"
    c.add_paragraph("第二行")

    h = docx_to_html(doc)
    assert "第一行" in h and "第二行" in h, f"单元格里的段落被吞了：{h}"


# --------------------------------------------------------------------------- 其它
def test_body_order_interleaves_paragraphs_and_tables() -> None:
    """段落与表格要**按文档真实顺序**还原。

    起诉状是"表里有段、段间有表"，按 ``doc.paragraphs`` + ``doc.tables``
    分开取会让顺序整个错乱（标题跑到表单后面去）。
    """
    doc = Document()
    doc.add_paragraph("标题")
    doc.add_table(rows=1, cols=1).cell(0, 0).text = "表格内容"
    doc.add_paragraph("落款")

    h = docx_to_html(doc)
    assert h.index("标题") < h.index("表格内容") < h.index("落款"), f"顺序乱了：{h}"


def test_page_geometry_returns_inches() -> None:
    """页面尺寸与页边距（英寸）—— 预览要靠它渲染成"一张纸"。"""
    doc = Document()
    geo = page_geometry(doc)
    assert abs(geo["width"] - 8.5) < 0.02, geo
    assert abs(geo["height"] - 11.0) < 0.02, geo
    assert 0 < geo["margin_left"] < 3, geo


def test_page_geometry_survives_a_document_with_no_sections() -> None:
    """取不到节时返回空字典，不抛 —— 预览缺页边距总比整页 500 强。"""

    class _Bare:
        @property
        def sections(self):
            raise IndexError("no sections")

    assert page_geometry(_Bare()) == {}


# --------------------------------------------------------------------------- 真模板
def _real(path: str):
    from backend.app.services import template_service as ts

    d = ts.template_dir()
    f = d / f"{path}.docx"
    if not f.exists():
        pytest.skip(f"模板 {path}.docx 不在 {d.name} 目录（换机器/CI 属正常）")
    return Document(str(f))


def test_real_arbitration_template_fidelity() -> None:
    """真·仲裁申请书：字体、字号、缩进、标题居中。"""
    h = docx_to_html(_real("广东_劳动仲裁申请书"))
    assert "font-family:'黑体'" in h, f"标题字体丢了：{h[:300]}"
    assert "font-size:16pt" in h, "标题字号丢了"
    assert "text-align:center" in h, "标题没居中"
    assert re.search(r"text-indent:2[0-9](\.\d)?pt", h), "正文首行缩进丢了"
    assert "font-family:'宋体'" in h, "正文字体丢了"


def test_real_complaint_template_is_prose_not_form() -> None:
    """真·起诉状：是**段落式文书**，不是法院那张空白表单。

    这两份模板 2026-09-28 从「表格式表单」重建成了「段落式文书」（用户要的是
    一份能直接递给法院的起诉状，拿到手的却是一张满屏 ☐/☑ 的表让他自己填）。
    所以这里断言的正是**重建后的形态**。

    ⚠️ 历史上这条断言的是 ``<table`` 和 ``colspan`` —— 与现在正好相反。
    那两条断言的失效恰恰是这次重建的信号，别照抄回来。
    """
    h = docx_to_html(_real("劳动争议纠纷起诉状"))
    assert "<table" not in h, "起诉状又变回表格式了"
    assert "text-align:center" in h, "标题没居中"
    assert "font-family:'黑体'" in h and "font-size:16pt" in h, "标题字体/字号丢了"
    assert "font-family:'宋体'" in h, "正文字体丢了"
    assert re.search(r"text-indent:2[0-9](\.\d)?pt", h), "正文首行缩进丢了"
    assert "text-align:right" in h, "落款没右对齐"
    assert "Calibri" not in h, "读成段落样式的 Calibri 了，应该读 run 级 eastAsia"


def _form_and_prose_doc():
    """手工造一份**同时**含真表格与段落版式的 doc。

    原来这份样本就是真·起诉状 —— 它重建之后不再有表格，用它做样本就再也
    测不到 ``colspan``/``border`` 那几项了。与其让覆盖面对半砍，不如自己造：
    表格那几项本来也只是"碰巧"来自真文件。
    """
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt

    def cjk(run, font: str, size: int) -> None:
        run.font.name = font
        run.font.size = Pt(size)
        run._element.rPr.rFonts.set(qn("w:eastAsia"), font)

    doc = Document()

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    cjk(p.add_run("民事起诉状"), "黑体", 16)

    p = doc.add_paragraph()
    p.paragraph_format.first_line_indent = Pt(24.1)
    cjk(p.add_run("原告：某某。"), "宋体", 12)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    cjk(p.add_run("具状人：某某"), "宋体", 12)

    # 表格：横向合并出 gridSpan，纵向合并出 vMerge，再自己声明框线
    # （python-docx 的默认表格样式不带 `tblBorders`，`_table_border` 就会返回空）
    table = doc.add_table(rows=2, cols=3)
    table.cell(0, 0).merge(table.cell(0, 1))
    table.cell(0, 2).merge(table.cell(1, 2))
    borders = OxmlElement("w:tblBorders")
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{side}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), "8")
        borders.append(el)
    table._tbl.tblPr.append(borders)
    cjk(table.cell(0, 0).paragraphs[0].add_run("当事人信息"), "宋体", 12)
    return doc


def test_sanitize_preserves_template_fidelity() -> None:
    """净化一遍之后，模板版式**一个都不能少**。

    预览链路是 ``docx_to_html`` → ``sanitize`` → 前端。净化器认不出
    ``text-indent`` 或 ``colspan`` 的话，用户看到的又是一份"跑了版式"的文书 ——
    和这次要修的问题一模一样，只是换了个环节出问题。

    所以这条不是"顺手测一下"，它是**两个模块之间的契约**：渲染器产出的
    每一项版式，净化器的白名单里都必须有对应项。
    """
    from backend.app.services.html_sanitize import sanitize

    raw = docx_to_html(_form_and_prose_doc())
    out = sanitize(raw)

    for marker, what in (
        ("<table", "表格"),
        ("colspan=", "横向合并"),
        ("rowspan=", "纵向合并"),
        ("border:", "表格边框"),
        ("font-family:", "字体"),
        ("font-size:", "字号"),
        ("text-align:center", "标题居中"),
        ("text-align:right", "落款右对齐"),
        ("text-indent:", "首行缩进"),
    ):
        assert marker in raw, f"合成样本本身就没渲染出{what}（{marker}），样本要修"
        assert marker in out, f"净化把{what}删掉了：{marker}"

    # 内容也不能少：正文文字数量应当基本一致（允许标签被规范化带来的差量）
    import re as _re
    strip = lambda s: _re.sub(r"<[^>]+>", "", s)  # noqa: E731
    assert len(strip(out)) >= len(strip(raw)) * 0.98, "净化吃掉了正文文字"


def test_no_path_leak_in_rendered_template() -> None:
    """渲染结果里不能出现服务器路径。

    这份 HTML 会直接发给前端、也会被存进草稿。与 ``test_no_path_leak.py``
    同一个原则，只是多了一条出口。
    """
    for name in ("广东_劳动仲裁申请书", "劳动争议纠纷起诉状"):
        h = docx_to_html(_real(name))
        assert not re.search(r"[A-Za-z]:[\\/]", h), f"{name} 渲染出了盘符"
        assert "data/raw" not in h and "data\\raw" not in h, f"{name} 渲染出了语料路径"
