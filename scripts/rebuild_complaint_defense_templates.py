"""把「民事起诉状 / 民事答辩状」从**表格式**重建成**段落式**。

## 为什么

这两份模板原本就是**法院那张空白表单**（正文全在一张 `<w:tbl>` 里：「说明」
「当事人信息」「诉讼请求和依据 1~10」「事实和理由 1~7」+ 满屏 ☐/☑）。
`template_service` 忠实地把表单渲染了出来 —— 于是用户拿到手的不是一份能递给
法院的文书，而是一张让他自己填的表。要改的是模板文件，不是渲染逻辑。

做法沿用 2026-09-28 那轮 **31 份劳动仲裁申请书重建**（50 段表单 → 24 段 prose）
的既定约定，范本就是 `广东_劳动仲裁申请书.docx`。

## 版式约定（每一条都有下游代码在看，别随手改）

| 约定 | 谁在看 |
|---|---|
| 纯段落，**没有任何 `<w:tbl>`** | `template_service._iter_body` 的 `tbl` 分支不再产出 section/row |
| 待填空用全角 `＿＿`（**≥2 个**） | `_PLACEHOLDER_RE` / `_extract_fields` / 前端 `highlightPlaceholders` |
| 条目序号写成 `＿＿.` | `_ITEM_NO_RE` —— `apply_amount_consistency` 与自动重新编号全靠它 |
| 落款 `w:jc=right` | 不能用"居中+手打空格"（`_norm_align` 的 6 空格阈值会让预览/Word 分叉），也不能用制表位（`docx_html` 不处理 `w:tab`） |
| 标题居中**且无前导空格** | 同上 |
| 字体写在 **run 级** `rFonts/@w:eastAsia` | `docx_html._run_font` 完全不读段落样式 |
| 不写 `（请…填写…）` 独立说明段 | 会被判成 `hint`：预览看得见、导出 Word 被 `obj.clear()` 清空 |
| 第 2 段起就带 `：` | 段落式模板下 `parse_docx` 的 `not table_seen` 保护永久失效，开头连续 3 段「≤60 字且无冒号」都会被吃成标题 |
| 空段落只撑版式 | `_iter_body` 与 `_iter_docx_elements` **都跳过空段**，所以空行不占字段号，两侧游标不会错位 |
| **块下标就是字段 id** `p{bi}` | `bi` = 非空段落的序号，从 0 起 |

页面沿用**原起诉状模板的 A4 与页边距** —— 不能用 `Document()` 的默认 Letter，
否则起诉状导出会从 A4 变成 Letter（`tests/test_html_export.py` 那条抄纸张的用例）。

## 跑法

    D:\\an\\envs\\langchain\\python.exe scripts\\rebuild_complaint_defense_templates.py

会先备份到 `文书模板_bak_before_prose_<日期>\\`，再写两个目录：
`.env` 的 `DOC_TEMPLATE_DIR`（生效的那份）与 `data/raw/劳动法/文书模板`（兜底副本）。
顺带把生效目录里的**全部** .docx 同步到兜底副本 —— 实测 31 份仲裁申请书里
有 22 份是旧版，一旦有人注释掉 `.env` 就会悄悄退回改版前的模板。

写完后**自己解析一遍**（`parse_docx`）核对块下标，对不上就报错退出。
"""

from __future__ import annotations

import re
import shutil
import sys
from datetime import date
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Emu, Pt

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------- 版式常量
# 原起诉状模板的 A4 与页边距（EMU）。**不要改成 Document() 的默认值**（那是 Letter）。
PAGE_W, PAGE_H = 7560310, 10692130
MARGIN_LR, MARGIN_TB = 1143000, 914400

FONT_TITLE, FONT_BODY = "黑体", "宋体"
SIZE_TITLE, SIZE_BODY = 16, 12
TITLE_SPACE_AFTER = Pt(12)
BODY_INDENT = Pt(24.1)          # ＝2 字符，实测仲裁模板用的就是这个值
LINE_SPACING = 1.3

# 段落样式标签。`gap` 是空段，只用来撑竖向节奏 —— 解析与回填**两侧都跳过空段**，
# 所以它不占字段号，插在哪里都不会让 `p{bi}` 错位。
TITLE, HEADING, BODY, SIGN, GAP = "title", "heading", "body", "sign", "gap"


# ================================================================ 生成
def _write_para(doc: Document, style: str, text: str) -> None:
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.line_spacing = LINE_SPACING

    if style == GAP:
        return
    if style == TITLE:
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        pf.space_after = TITLE_SPACE_AFTER
        font, size = FONT_TITLE, SIZE_TITLE
    elif style == SIGN:
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        font, size = FONT_BODY, SIZE_BODY
    else:                       # body / heading
        pf.first_line_indent = BODY_INDENT
        font, size = FONT_BODY, SIZE_BODY

    run = p.add_run(text)
    run.bold = style == HEADING
    run.font.size = Pt(size)
    run.font.name = font
    # ⚠️ 中文字体必须落在 run 级 rFonts/@w:eastAsia 上 —— docx_html._run_font 只读这里，
    #    只写 font.name（=ascii）会让预览退回 Calibri。
    run._element.rPr.rFonts.set(qn("w:eastAsia"), font)


def build(blocks: list[tuple[str, str]]) -> Document:
    doc = Document()
    # 默认样式也设成宋体，免得 Word 里出现 Calibri 的中间态
    style = doc.styles["Normal"]
    style.font.name = FONT_BODY
    style.font.size = Pt(SIZE_BODY)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), FONT_BODY)

    sec = doc.sections[0]
    sec.page_width, sec.page_height = Emu(PAGE_W), Emu(PAGE_H)
    sec.left_margin = sec.right_margin = Emu(MARGIN_LR)
    sec.top_margin = sec.bottom_margin = Emu(MARGIN_TB)

    for st, text in blocks:
        _write_para(doc, st, text)
    return doc


# ================================================================ 起诉状
# 当事人信息 / 诉讼请求 / 事实与理由 的顺序与措辞对着用户给的样张（图一）排的。
# ⚠️ 几处「正则钩子」的写法不能随手改（改了对应的自动能力会**静默**失效）：
#   · `＿＿＿＿年＿＿月＿＿日出生`  —— _BIRTH_BEFORE_RE 认这个后缀
#   · `公民身份号码：＿＿＿＿…`    —— _ID_SLOT_IN_PARA_RE 认这个标签
#   · `＿＿，＿＿族，`              —— _SEX_IN_PARA_RE 要求性别空夹在两逗号之间、后跟「…族」
#   · `……元／月 × ＿＿个月 × 2倍 ＝ ＿＿＿＿元。` —— _FORMULA_RESULT_RE 拿等式右边当金额准绳
COMPLAINT: list[tuple[str, str]] = [
    (TITLE, "民事起诉状"),

    (BODY, "原告：＿＿＿＿，＿＿，＿＿族，＿＿＿＿年＿＿月＿＿日出生，公民身份号码："
           "＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿，住址：＿＿＿＿＿＿＿＿＿＿＿＿，"
           "工作单位：＿＿＿＿＿＿＿＿＿＿，职务：＿＿＿＿，联系电话：＿＿＿＿＿＿＿＿＿＿。"),
    (BODY, "委托诉讼代理人：＿＿＿＿，＿＿＿＿＿＿＿＿＿＿＿＿律师事务所律师，"
           "联系电话：＿＿＿＿＿＿＿＿＿＿。"),
    (BODY, "被告：＿＿＿＿＿＿＿＿＿＿＿＿，住所地：＿＿＿＿＿＿＿＿＿＿＿＿，"
           "统一社会信用代码：＿＿＿＿＿＿＿＿＿＿＿＿＿＿。"),
    (BODY, "法定代表人：＿＿＿＿，职务：＿＿＿＿，联系电话：＿＿＿＿＿＿＿＿＿＿。"),

    (GAP, ""),
    (HEADING, "诉讼请求："),
    (BODY, "＿＿. 判令确认被告于＿＿＿＿年＿＿月＿＿日解除与原告的劳动合同行为违法；"),
    (BODY, "＿＿. 判令被告向原告支付＿＿＿＿＿＿＿＿＿＿人民币＿＿＿＿＿＿元；"),
    (BODY, "＿＿. 判令被告向原告支付＿＿＿＿＿＿＿＿＿＿人民币＿＿＿＿＿＿元；"),
    (BODY, "＿＿. 判令被告向原告支付＿＿＿＿＿＿＿＿＿＿人民币＿＿＿＿＿＿元；"),
    (BODY, "＿＿. 本案诉讼费用由被告承担。"),

    (GAP, ""),
    (HEADING, "事实与理由："),
    (BODY, "原告与被告存在劳动关系。原告于＿＿＿＿年＿＿月＿＿日入职被告处，担任＿＿＿＿岗位，"
           "双方于＿＿＿＿年＿＿月＿＿日签订《劳动合同书》，约定合同期限自＿＿＿＿年＿＿月＿＿日起"
           "至＿＿＿＿年＿＿月＿＿日止，工作地点为＿＿＿＿＿＿＿＿＿＿，约定月工资为人民币＿＿＿＿＿＿元"
           "（由＿＿＿＿＿＿＿＿＿＿＿＿构成），被告自＿＿＿＿年＿＿月起为原告缴纳社会保险。"
           "原告实际工作期间为＿＿＿＿年＿＿月＿＿日至＿＿＿＿年＿＿月＿＿日。"),
    (BODY, "＿＿＿＿年＿＿月＿＿日，被告以「＿＿＿＿＿＿＿＿＿＿」为由，向原告送达"
           "《解除劳动合同通知书》，单方解除双方劳动关系。被告在作出解除决定前，"
           "未提前三十日以书面形式通知原告，亦未额外支付一个月工资作为代通知金；"
           "被告未就变更劳动合同内容与原告进行协商，直接以经营自主决策为由终止用工。"
           "此外，被告未依法向原告支付任何经济补偿或赔偿金。"),
    (BODY, "原告认为，被告的上述解除行为严重违反法律规定，构成违法解除劳动合同。"
           "根据《中华人民共和国劳动合同法》第四十一条之规定，用人单位因生产经营发生严重困难"
           "等情形需要裁减人员二十人以上或者裁减不足二十人但占企业职工总数百分之十以上的，"
           "应当提前三十日向工会或者全体职工说明情况、听取意见并向劳动行政部门报告等法定程序。"
           "本案中，被告仅以内部「＿＿＿＿＿＿＿＿」为由单方解除合同，既不符合经济性裁员的"
           "法定实体要件，也未履行法定的民主程序和行政报告义务。同时，根据《中华人民共和国"
           "劳动合同法》第二十六条第（三）项之规定，即使属于「劳动合同订立时所依据的客观情况"
           "发生重大变化」，用人单位也应当先与劳动者协商变更劳动合同，只有在协商不成时方可解除，"
           "且需提前三十日书面通知或支付代通知金。被告未履行上述任何法定前置程序，"
           "其解除行为缺乏法律依据和程序合法性，应认定为违法解除。"),
    (BODY, "关于赔偿金的计算，根据《中华人民共和国劳动合同法》第八十七条规定，"
           "用人单位违反本法规定解除或者终止劳动合同的，应当依照本法第四十七条规定的"
           "经济补偿标准的二倍向劳动者支付赔偿金。依据《中华人民共和国劳动合同法》"
           "第四十七条规定，经济补偿按劳动者在本单位工作的年限，每满一年支付一个月工资的标准"
           "向劳动者支付；六个月以上不满一年的，按一年计算；不满六个月的，向劳动者支付"
           "半个月工资的经济补偿。原告在被告处工作年限为＿＿＿＿＿＿，故经济补偿年限应按"
           "＿＿＿＿个月计算。原告离职前十二个月平均工资为人民币＿＿＿＿＿＿元，因此，"
           "被告应支付的违法解除劳动合同赔偿金计算方式为：＿＿＿＿＿＿元／月 × ＿＿＿＿个月 × 2倍"
           " ＝ ＿＿＿＿＿＿＿＿元。"),
    (BODY, "关于未休年休假工资，原告在职期间累计工作已满＿＿年不满＿＿年，"
           "依法享有每年＿＿天的带薪年休假。＿＿＿＿年度及＿＿＿＿年度，被告均未安排原告休年假，"
           "亦未支付未休年休假工资报酬。根据相关规定，用人单位经职工同意不安排年休假或者"
           "安排职工年休假天数少于应休年休假天数的，应当在本年度内对职工应休未休年休假天数，"
           "按照其日工资收入的300%支付未休年休假工资报酬，其中包含用人单位支付职工"
           "正常工作期间的工资收入。因此，被告应另行支付200%的工资差额。具体计算如下："
           "日工资基数为＿＿＿＿＿＿元 ÷ 21.75天 ≈ ＿＿＿＿元；未休天数合计＿＿天；"
           "应付金额 ＝ ＿＿＿＿元 × 200% × ＿＿天 ＝ ＿＿＿＿＿＿元。"),
    (BODY, "原告已于＿＿＿＿年＿＿月＿＿日向＿＿＿＿＿＿＿＿＿＿＿＿劳动人事争议仲裁委员会"
           "申请仲裁，该委于＿＿＿＿年＿＿月＿＿日作出＿＿＿＿＿＿＿＿＿＿号仲裁裁决书，"
           "＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿"
           "＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿。原告对该裁决不服，"
           "在法定期限内提起诉讼，符合法律规定。"),
    (BODY, "综上所述，被告违法解除劳动合同的事实清楚，证据确凿，其应承担相应的法律责任。"
           "为维护原告合法权益，特向贵院提起诉讼，望判如所请。"),

    (GAP, ""),
    (BODY, "此致"),
    (BODY, "＿＿＿＿＿＿＿＿＿＿人民法院"),

    (GAP, ""),
    (SIGN, "具状人：＿＿＿＿＿＿"),
    (SIGN, "日期：＿＿＿＿年＿＿月＿＿日"),
]

# ================================================================ 答辩状
# 答辩人（用人单位）一侧。`被答辩人：` 后面**不要**加「（原告）」——
# `_fill_para` 的 `_SLOT_RULES` 是 endswith 匹配，多了尾巴就认不出这一格。
DEFENSE: list[tuple[str, str]] = [
    (TITLE, "民事答辩状"),

    (BODY, "答辩人：＿＿＿＿＿＿＿＿＿＿＿＿，住所地：＿＿＿＿＿＿＿＿＿＿＿＿，"
           "统一社会信用代码：＿＿＿＿＿＿＿＿＿＿＿＿＿＿。"),
    (BODY, "法定代表人：＿＿＿＿，职务：＿＿＿＿，联系电话：＿＿＿＿＿＿＿＿＿＿。"),
    (BODY, "委托诉讼代理人：＿＿＿＿，＿＿＿＿＿＿＿＿＿＿＿＿律师事务所律师，"
           "联系电话：＿＿＿＿＿＿＿＿＿＿。"),
    (BODY, "被答辩人：＿＿＿＿，＿＿，＿＿族，＿＿＿＿年＿＿月＿＿日出生，公民身份号码："
           "＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿，住址：＿＿＿＿＿＿＿＿＿＿＿＿，"
           "联系电话：＿＿＿＿＿＿＿＿＿＿。"),

    (GAP, ""),
    (HEADING, "答辩请求："),
    (BODY, "＿＿. 请求依法驳回被答辩人的全部诉讼请求；"),
    (BODY, "＿＿. 本案诉讼费用由被答辩人承担。"),

    (GAP, ""),
    (HEADING, "事实与理由："),
    (BODY, "答辩人因与被答辩人劳动争议一案，现依法提出答辩意见如下："),
    (BODY, "关于被答辩人主张的＿＿＿＿＿＿＿＿＿＿＿＿，答辩人不予认可。"
           "＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿"
           "＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿。"),
    (BODY, "关于被答辩人主张的＿＿＿＿＿＿＿＿＿＿＿＿，答辩人不予认可。"
           "＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿"
           "＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿。"),
    (BODY, "关于被答辩人主张的＿＿＿＿＿＿＿＿＿＿＿＿，答辩人不予认可。"
           "＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿"
           "＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿。"),
    (BODY, "除上述答辩意见外，＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿"
           "＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿。"),
    (BODY, "综上所述，被答辩人的诉讼请求缺乏事实和法律依据。答辩人恳请贵院查明事实，"
           "依法驳回被答辩人的全部诉讼请求。"),

    (GAP, ""),
    (BODY, "此致"),
    (BODY, "＿＿＿＿＿＿＿＿＿＿人民法院"),

    (GAP, ""),
    (SIGN, "答辩人：＿＿＿＿＿＿"),
    (SIGN, "日期：＿＿＿＿年＿＿月＿＿日"),
]

# ================================================================ 两份「实例」
# 同一套骨架、同一套版式，只是把 ＿＿ 换成老实例里那份案情（刘某某 vs 北京XX公司）。
# 未知项沿用老实例的 `XX` 掩码写法 —— 它本来就是**样例**，不是交付稿。
# `kind` 仍是 "example"：`pick_template` 只在没有空白模板时才会用到它们。
COMPLAINT_EXAMPLE: list[tuple[str, str]] = [
    (TITLE, "民事起诉状"),

    (BODY, "原告：刘某某，男，汉族，1973年XX月XX日出生，公民身份号码：XXXXXXXXXX，"
           "住址：北京市门头沟区XX路XX号，工作单位：北京XX公司，职务：职员，"
           "联系电话：XXXXX。"),
    (BODY, "委托诉讼代理人：汪某某，XX律师事务所律师，联系电话：XXXXX。"),
    (BODY, "被告：北京XX公司，住所地：北京市平谷区XX路XX号，"
           "统一社会信用代码：XXXXXXXXXX。"),
    (BODY, "法定代表人：张某某，职务：董事长，联系电话：XXXXX。"),

    (GAP, ""),
    (HEADING, "诉讼请求："),
    (BODY, "1. 判令被告向原告支付未订立书面劳动合同的二倍工资差额人民币33,000元；"),
    (BODY, "2. 判令被告向原告支付解除劳动合同经济补偿人民币3,000元；"),
    (BODY, "3. 本案诉讼费用由被告承担。"),

    (GAP, ""),
    (HEADING, "事实与理由："),
    (BODY, "原告与被告存在劳动关系。原告于2019年XX月XX日入职被告处，从事清洁工作，"
           "约定每月工资人民币3,000元，劳动合同期限1年，但双方未签订书面劳动合同。"
           "在职期间，原告勤勉尽责，正常提供劳动。"),
    (BODY, "合同期满后，因被告不同意续签合同，2020年XX月XX日，双方劳动关系终止。"
           "被告未向原告支付任何经济补偿。"),
    (BODY, "关于未订立书面劳动合同的二倍工资差额。根据《中华人民共和国劳动合同法》"
           "第八十二条规定，用人单位自用工之日起超过一个月不满一年未与劳动者订立书面劳动合同的，"
           "应当向劳动者每月支付二倍的工资。被告自2019年XX月XX日用工之日起，至2020年XX月XX日"
           "劳动关系终止，始终未与原告订立书面劳动合同，依法应当支付二倍的工资差额。"
           "计算方式为：3,000元／月 × 11个月 ＝ 33,000元。"),
    (BODY, "关于解除劳动合同经济补偿。根据《中华人民共和国劳动合同法》第四十六条、"
           "第四十七条规定，劳动合同期满终止，除用人单位维持或者提高劳动合同约定条件续订"
           "而劳动者不同意续订的情形外，用人单位应当向劳动者支付经济补偿。经济补偿按劳动者"
           "在本单位工作的年限，每满一年支付一个月工资的标准向劳动者支付。原告在被告处"
           "工作1年，月工资3,000元，故经济补偿为：3,000元／月 × 1个月 ＝ 3,000元。"),
    (BODY, "原告已于2020年XX月XX日向北京市XX区劳动人事争议仲裁委员会申请仲裁，"
           "该委于2020年XX月XX日作出XXX号裁决书，确认原告与被告在2019年XX月XX日至"
           "2020年XX月XX日期间存在劳动关系，并驳回了原告的其他仲裁请求。原告对该裁决不服，"
           "在法定期限内提起诉讼，符合法律规定。"),
    (BODY, "综上所述，被告未与原告订立书面劳动合同、且未依法支付经济补偿的事实清楚，"
           "证据确凿，其应承担相应的法律责任。为维护原告合法权益，特向贵院提起诉讼，"
           "望判如所请。"),

    (GAP, ""),
    (BODY, "此致"),
    (BODY, "北京市XX区人民法院"),

    (GAP, ""),
    (SIGN, "具状人：刘某某"),
    (SIGN, "日期：2021年XX月XX日"),
]

DEFENSE_EXAMPLE: list[tuple[str, str]] = [
    (TITLE, "民事答辩状"),

    (BODY, "答辩人：北京XX公司，住所地：北京市平谷区XX路XX号，"
           "统一社会信用代码：XXXXXXXXXX。"),
    (BODY, "法定代表人：张某某，职务：董事长，联系电话：XXXXX。"),
    (BODY, "委托诉讼代理人：肖某某，北京XX公司职员，联系电话：XXXX。"),
    (BODY, "被答辩人：刘某某，男，汉族，1973年XX月XX日出生，公民身份号码：XXXXXXXXXX，"
           "住址：北京市门头沟区XX路XX号，联系电话：XXXXX。"),

    (GAP, ""),
    (HEADING, "答辩请求："),
    (BODY, "1. 请求依法驳回被答辩人的全部诉讼请求；"),
    (BODY, "2. 本案诉讼费用由被答辩人承担。"),

    (GAP, ""),
    (HEADING, "事实与理由："),
    (BODY, "答辩人因与被答辩人劳动争议一案，现依法提出答辩意见如下："),
    (BODY, "关于被答辩人主张的未订立书面劳动合同二倍工资，答辩人不予认可。"
           "答辩人已多次通知被答辩人签订书面劳动合同，系被答辩人个人原因没有签订，"
           "答辩人对此不存在过错，不应承担支付二倍工资的责任。"),
    (BODY, "关于被答辩人主张的解除劳动合同经济补偿，答辩人不予认可。"
           "被答辩人严重违反答辩人的规章制度，双方劳动合同期满后，答辩人依法不予续签，"
           "不属于应当支付经济补偿的情形。"),
    (BODY, "综上所述，被答辩人的诉讼请求缺乏事实和法律依据。答辩人恳请贵院查明事实，"
           "依法驳回被答辩人的全部诉讼请求。"),

    (GAP, ""),
    (BODY, "此致"),
    (BODY, "北京市XX区人民法院"),

    (GAP, ""),
    (SIGN, "答辩人：北京XX公司"),
    (SIGN, "日期：2021年XX月XX日"),
]

TARGETS: list[tuple[str, list[tuple[str, str]]]] = [
    ("劳动争议纠纷起诉状", COMPLAINT),
    ("劳动争议纠纷答辩状", DEFENSE),
    ("劳动争议纠纷起诉状实例", COMPLAINT_EXAMPLE),
    ("劳动争议纠纷答辩状实例", DEFENSE_EXAMPLE),
]


# ================================================================ 落盘
def template_dirs() -> list[Path]:
    """生效目录（.env 的 DOC_TEMPLATE_DIR）+ 项目内兜底副本。

    生效目录优先从 .env 里读 —— 但**不引 settings**：跑这个脚本不该顺带把整个
    backend 配置（含 DB、模型密钥）拉起来。读不到就退回兜底目录。
    """
    fallback = PROJECT_ROOT / "data" / "raw" / "劳动法" / "文书模板"
    active = None
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("DOC_TEMPLATE_DIR="):
                raw = line.split("=", 1)[1].strip().strip('"').strip("'")
                if raw:
                    active = Path(raw)
                break
    return [p for p in (active, fallback) if p and p.exists()]


def backup(dirs: list[Path]) -> Path:
    """把每一份现存 .docx 备份到同一个日期后缀目录下。

    备份放在**生效目录的兄弟位置**（跟历史几次重建保持一致）。
    ⚠️ 两个来源目录都叫「文书模板」，直接拿 `d.name` 当子目录名会让第二份把第一份覆盖掉，
    所以按来源加前缀区分。
    """
    today = date.today().strftime("%Y%m%d")
    base = dirs[0].parent / f"{dirs[0].name}_bak_before_prose_{today}"
    if base.exists():
        print(f"[跳过备份] {base} 已存在，不覆盖")
        return base

    for i, d in enumerate(dirs):
        label = "生效目录" if i == 0 else f"兜底副本_{i}"
        dest = base / label
        dest.mkdir(parents=True, exist_ok=True)
        n = 0
        for f in sorted(d.glob("*.docx")):
            shutil.copy2(f, dest / f.name)
            n += 1
        print(f"[备份] {d} → {dest}（{n} 份）")
    print(f"[备份完成] {base}")
    return base


def sync_copies(dirs: list[Path]) -> None:
    """把生效目录里的**全部** .docx 同步到兜底副本。

    实测 31 份仲裁申请书里有 22 份两边不一致（改版只落在了生效目录），
    一旦有人注释掉 .env 就会悄悄退回旧模板。这里一次抹平。
    """
    if len(dirs) < 2:
        print("[跳过同步] 只有一个模板目录")
        return
    src, dst = dirs[0], dirs[1]
    n = 0
    for f in sorted(src.glob("*.docx")):
        target = dst / f.name
        if not target.exists() or target.read_bytes() != f.read_bytes():
            shutil.copy2(f, target)
            n += 1
    print(f"[同步完成] {src.name} → {dst.name}：{n} 份有更新")


def verify(name: str, blocks: list[tuple[str, str]], dirs: list[Path]) -> None:
    """核对生成结果。分两层查：

    ① **docx 本身** —— 有没有表格、标题/落款的对齐、run 级中文字体、纸张尺寸。
       这些是 `parse_docx` 的 block 里**存不下**的（`title` 块根本不落 `align`），
       但恰恰是 Word 里肉眼看得见的那部分。
    ② **解析产物** —— 逐个核对块下标。字段 id 是位置派生的，写少一段、多一段空行，
       `p{bi}` 就全错位，而且**不会报任何错**。
    """
    sys.path.insert(0, str(PROJECT_ROOT))
    from backend.app.services import template_service as ts
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn

    path = dirs[0] / f"{name}.docx"
    doc = Document(str(path))

    # ---- ① docx 本身
    if doc.tables:
        raise SystemExit(f"✗ {name}：还残留 {len(doc.tables)} 张表格")
    body = [p for p in doc.paragraphs if (p.text or "").strip()]
    if body[0].alignment != WD_ALIGN_PARAGRAPH.CENTER:
        raise SystemExit(f"✗ {name}：标题没有居中（{body[0].alignment}）")
    for p in body[-2:]:
        if p.alignment != WD_ALIGN_PARAGRAPH.RIGHT:
            raise SystemExit(f"✗ {name}：落款『{p.text}』没有右对齐（{p.alignment}）")

    sec = doc.sections[0]
    if (sec.page_width, sec.page_height) != (Emu(PAGE_W), Emu(PAGE_H)):
        raise SystemExit(
            f"✗ {name}：纸张 {sec.page_width}x{sec.page_height}，预期 {PAGE_W}x{PAGE_H}"
            "（test_html_export 拿起诉状当『抄纸张』的样本）")
    if (sec.left_margin, sec.top_margin) != (Emu(MARGIN_LR), Emu(MARGIN_TB)):
        raise SystemExit(f"✗ {name}：页边距对不上")

    for p in body:                      # run 级中文字体：docx_html 只读这里
        for r in p.runs:
            rpr = r._element.rPr
            ea = rpr.rFonts.get(qn("w:eastAsia")) if (rpr is not None
                                                      and rpr.rFonts is not None) else None
            if not ea:
                raise SystemExit(f"✗ {name}：『{p.text[:20]}…』有 run 没写 w:eastAsia")

    # ---- ② 解析产物
    parsed = ts.parse_docx(path)
    got = parsed["blocks"]
    want = [(st, tx) for st, tx in blocks if st != GAP]
    if len(got) != len(want):
        raise SystemExit(f"✗ {name}：解析出 {len(got)} 块，预期 {len(want)} 块")

    for i, (b, (st, tx)) in enumerate(zip(got, want)):
        expect_t = "title" if st == TITLE else "para"
        if b["t"] != expect_t:
            raise SystemExit(f"✗ {name} p{i}：块类型是 {b['t']!r}，预期 {expect_t!r}")
        if b.get("text") != tx:
            raise SystemExit(
                f"✗ {name} p{i} 文字对不上\n  实际：{b.get('text')!r}\n  预期：{tx!r}")
        if st == SIGN and b.get("align") != "right":
            raise SystemExit(f"✗ {name} p{i}：落款块没有带上右对齐")
        if st == HEADING and not b.get("text", "").endswith("："):
            raise SystemExit(f"✗ {name} p{i}：小标题丢了冒号（标题判定要靠它）")

    if any(b["t"] in ("row", "section", "hint") for b in got):
        raise SystemExit(f"✗ {name}：出现了 row/section/hint 块")

    fields = ts.fields_for_prompt(parsed)
    kinds = {f["kind"] for f in fields}
    if kinds - {"para"}:
        raise SystemExit(f"✗ {name}：出现了非 para 字段 {kinds}")

    # 该有哪些字段是**从段落表本身推出来的**：正文/落款里带 `＿＿` 的段落才可填，
    # 固定段（`综上所述…`、`此致`）与小标题不该出现在字段清单里。
    # 这条能兜住两类手滑：固定段里误留了下划线（于是模型会去改一句本该钉死的话）、
    # 可填段漏了下划线（于是那一格永远填不上，且**不报任何错**）。
    want_ids = [f"p{i}" for i, (st, tx) in enumerate(want)
                if st in (BODY, SIGN) and re.search(r"[＿_]{2,}", tx)]
    got_ids = [f["id"] for f in fields]
    if got_ids != want_ids:
        raise SystemExit(
            f"✗ {name}：字段 id 对不上\n  实际：{got_ids}\n  预期：{want_ids}")

    tail = f"（{got_ids[0]}…{got_ids[-1]}）" if got_ids else "（无填空，全篇固定文字）"
    print(f"[校验通过] {name}：{len(got)} 块 / {len(fields)} 个待填字段 {tail}")


def main() -> None:
    dirs = template_dirs()
    if not dirs:
        raise SystemExit("找不到任何模板目录")
    print("目标目录：" + "、".join(str(d) for d in dirs))

    backup(dirs)

    for name, blocks in TARGETS:
        doc = build(blocks)
        for d in dirs:
            doc.save(str(d / f"{name}.docx"))
        print(f"[已生成] {name}.docx")

    # 先把 4 份新建的同步过去，再把生效目录里其余的历史漂移一并抹平
    sync_copies(dirs)

    for name, blocks in TARGETS:
        verify(name, blocks, dirs)

    print("\n完成。⚠️ 后端模板索引有 600 秒内存缓存，换完文件要重启后端"
          "（或先 GET /api/v1/doc/templates?rebuild=1），否则会拿旧 blocks 去回填新文件。")


if __name__ == "__main__":
    main()
