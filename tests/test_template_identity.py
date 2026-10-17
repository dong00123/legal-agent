"""`apply_identity` 要认得出**两种**写法，以及重建后的起诉状/答辩状模板的形状。

## 为什么单独一个文件

`apply_identity` 是「能算的必须由代码钉死」那条硬约束的落点：出生日期、性别、
身份证号三项都能从 18 位身份证号算出来，交给模型去写只是多给它一次写岔的机会。
（实测出过身份证 `…19900512…` 配出生日期 `2023-03-01` —— 那是入职日期。）

## 这里防的是哪种失败

**静默失效。** 2026-09-28 起诉状/答辩状从表格式重建成段落式后，它们把出生日期写在
「出生」**后面**（`＿＿＿＿年＿＿月＿＿日出生`）、把身份证叫「公民身份号码」——
两个写法都跟 31 份仲裁申请书（`出生日期：…` / `身份证号码：…`）不一样。

当时改 `_ID_SLOT_IN_PARA_RE` 有个很隐蔽的坑：`身份证号码` 里**并不含**
`身份号码` 这个子串（中间夹着「证」），所以顺手写下的 `身份号码[：:]` 会
把 31 份仲裁申请书的身份证号**一个都填不进去** —— 不报错、不抛异常，
只是那 31 份文书里留着 18 个下划线交到仲裁委。所以下面两条写法都要有断言。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.app.services import template_service as ts

# 第 7~14 位 = 1988-03-12，第 17 位 1 = 男
IDNO = "110101198803124512"
BIRTH = "1988年3月12日"


# 当事人给的案情原文 —— `compose` 就是把它当 `source_text` 传进去的
# （`template_service.py:847`）。身份证号的三个来源里，这一条最常命中。
CASE_TEXT = f"我叫张伟，男，1988年3月12日生，身份证号是{IDNO}，在北京鼎盛嘉业公司上班。"


def _pin(para: str, *, source_text: str = "", blocks: list[dict] | None = None) -> str:
    """把一段模板文字过一遍 `apply_identity`，返回钉完的结果。

    ⚠️ 不传 `source_text` 且段落里也没有 18 位号时，`apply_identity` 会**原样返回**
    —— 那是设计好的（没有号就没有可对齐的口径，凭空造一个更糟），
    别把它当成"钉不上"的 bug。
    """
    fields = [{"id": "p1", "kind": "para", "label": "", "key": para[:40], "hint": para}]
    out = ts.apply_identity({"p1": para}, fields, blocks or [], source_text)
    return out["p1"]


# --------------------------------------------------------------------- 起诉状写法
def test_pins_birth_written_after_the_date() -> None:
    """起诉状/答辩状：`＿＿＿＿年＿＿月＿＿日出生，公民身份号码：…`。

    号从案情原文来（段落里还是空的），钉完必须**把标签留着** ——
    替换时漏掉 group(1) 的话，「公民身份号码：」这五个字会被 18 位数字吞掉。
    """
    got = _pin("原告：张伟，男，汉族，＿＿＿＿年＿＿月＿＿日出生，"
               "公民身份号码：＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿，住址：北京市。",
               source_text=CASE_TEXT)
    assert f"{BIRTH}出生" in got, f"出生日期没钉到身份证口径：{got}"
    assert IDNO in got, f"身份证号没落进正文：{got}"
    assert "公民身份号码：" in got, "标签被号吞掉了（替换时没把 group(1) 拼回去）"
    assert "，男，汉族" in got, f"性别没钉住：{got}"


def test_pins_from_id_already_in_the_paragraph() -> None:
    """号已经在段落里（模型填的）时，出生日期照样要按号钉 —— 不能只在空着时才钉。"""
    got = _pin("原告：张伟，男，汉族，2023年3月1日出生，"
               f"公民身份号码：{IDNO}，住址：北京市。")
    assert f"{BIRTH}出生" in got, f"模型写错的出生日期没被订正：{got}"
    assert "2023年3月1日" not in got, f"错的日期还在：{got}"


# ------------------------------------------------------------------ 申请书写法（回归）
def test_still_pins_arbitration_style_birth_label() -> None:
    """31 份仲裁申请书的写法不能被这次改动带坏：`出生日期：＿＿＿＿年＿＿月＿＿日`。"""
    got = _pin("申请人：＿＿＿＿，＿＿，＿＿族，出生日期：＿＿＿＿年＿＿月＿＿日，"
               "身份证号码：＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿，住址：＿＿＿＿＿＿。",
               source_text=CASE_TEXT)
    assert f"出生日期：{BIRTH}" in got, f"申请书式的出生日期没钉住：{got}"
    assert "＿＿＿＿年" not in got, f"出生日期那一格还空着：{got}"


def test_still_pins_arbitration_style_id_label() -> None:
    """`身份证号码：` 与 `公民身份号码：` 两种标签都要认（差一个「证」字）。

    ⚠️ 这条是**回归防线**：`身份证号码` 里并不含 `身份号码` 这个子串
    （中间夹着「证」），所以正则写成 `身份号码[：:]` 会把 31 份仲裁申请书的
    身份证号一个都填不进去 —— 不报错，只是那 31 份文书里留着 18 个下划线。
    """
    for label in ("身份证号码", "公民身份号码", "公民身份证号码"):
        got = _pin(f"申请人：张伟，{label}：＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿。",
                   source_text=CASE_TEXT)
        assert IDNO in got, f"{label} 那一格没填上：{got}"
        assert label in got, f"{label} 的标签被号吞掉了：{got}"


# ----------------------------------------------------------------------- 不许误伤
def test_does_not_touch_contract_effective_date() -> None:
    """`合同自 2022 年 7 月 1 日出生效` 与「出生」同形，不能被当成出生日期。

    （`(?!效)` 挡的就是这个。）
    """
    got = _pin("双方约定，本合同自 2022 年 7 月 1 日出生效，至 2025 年 6 月 30 日止。",
               source_text=CASE_TEXT)
    assert "2022 年 7 月 1 日出生效" in got, f"「出生效」被误当成出生日期改了：{got}"
    assert BIRTH not in got, f"「出生效」被填成了出生日期：{got}"


def test_no_id_number_leaves_fills_untouched() -> None:
    """找不到身份证号就原样返回 —— 没有号就没有可对齐的口径，凭空造一个更糟。"""
    para = "原告：张伟，男，汉族，＿＿＿＿年＿＿月＿＿日出生，住址：北京市。"
    assert _pin(para) == para
    assert _pin(para, source_text="当事人没提供身份证号，只有姓名和电话。") == para


# --------------------------------------------------------------- 截断尾巴接回去
def test_repairs_paragraph_the_model_stopped_halfway() -> None:
    """模型把整段写了一半 —— 用模板原文把尾巴接回去。

    实测：起诉状的 `原告：……联系电话：＿＿。` 一填到身份证号就停了，
    `住址`/`工作单位`/`职务`/`联系电话` 整截没了（提示词第 6 条写了"不许截断"，
    但模型对长段落会中途收笔）。法院立案要的正是住址和电话，截了递不进去。

    接回去的不是编的内容：尾巴里剩下的 `＿＿` 照常渲染成"待填"，让人工补。
    """
    hint = ("原告：＿＿＿＿，＿＿，＿＿族，＿＿＿＿年＿＿月＿＿日出生，"
            "公民身份号码：＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿，"
            "住址：＿＿＿＿＿＿＿＿＿＿＿＿，联系电话：＿＿＿＿＿＿＿＿＿＿。")
    fields = [{"id": "p1", "kind": "para", "label": "", "key": hint[:40], "hint": hint}]
    truncated = "原告：张伟，男，汉族，1988年3月12日出生，公民身份号码：110101198803124512"

    got = ts.repair_truncated_paras({"p1": truncated}, fields)["p1"]
    assert got.startswith(truncated), "模型填的那半截被改动了"
    assert "住址：" in got and "联系电话：" in got, f"尾巴没接回来：{got}"
    assert got.endswith("＿＿＿＿＿＿＿＿＿＿。"), f"尾巴该留成待填空：{got}"


def test_repair_leaves_faithful_and_deliberate_values_alone() -> None:
    """**误伤的代价比截断更大** —— 把模型正常回填的整段当截断处理，
    等于用模板原文覆盖掉它填进去的值，而且是静默的。所以下面一条都不许动。
    """
    hint = "原告：＿＿＿＿，住址：＿＿＿＿＿＿＿＿＿＿＿＿。"
    fields = [{"id": "p1", "kind": "para", "label": "", "key": hint[:40], "hint": hint}]

    cases = {
        "原样回填（没改一个字）": hint,
        "正常填满": "原告：张伟，住址：北京市朝阳区建国路88号。",
        # ← 关键分枝：改了措辞，但**写成了完整的句子**（有句号），不是戛然而止
        "模型自己改了措辞": "原告：张伟，住所：北京市朝阳区。",
        # ← 自己加了个句号收尾，也算写完了，别去接尾巴
        "自己收尾了": "原告：张伟，男，1988年3月12日出生。",
        "删除哨兵": ts.DELETE_MARK,
        "空值": "",
        "开头就对不上": "申请人张伟的情况说明",
    }
    for what, val in cases.items():
        got = ts.repair_truncated_paras({"p1": val}, fields)["p1"]
        assert got == val, f"{what} 被误改了：{val!r} → {got!r}"


def test_repair_on_the_real_complaint_template() -> None:
    """拿真模板 + 真截断串跑一遍（这条是 E2E 里实际发生的那次）。

    用真 hint 而不是手搓的字符串：`，＿＿，＿＿族，` / `日出生` / `公民身份号码：`
    这几处的**真实排布**才是要防的东西，搓一个简化版就把它们绕过去了。
    """
    tpl = ts.parse_docx(_real("劳动争议纠纷起诉状"))
    fields = ts.fields_for_prompt(tpl)
    truncated = ("原告：张伟，男，汉族，1988年3月12日出生，"
                 "公民身份号码：110101198803124512")

    got = ts.repair_truncated_paras({"p1": truncated}, fields)["p1"]
    assert got.startswith(truncated), "模型填的那半截被改动了"
    assert "110101198803124512" in got, "已填的身份证号被冲掉了"
    for label in ("住址：", "工作单位：", "职务：", "联系电话："):
        assert label in got, f"{label} 没接回来：{got}"
    assert got.endswith("。"), f"没接到模板该有的结尾：{got}"
    # 没接到内容的空必须留成待填空，不能凭空填上 —— 这是"不编"的底线
    assert "＿＿" in got, f"没内容的地方没留成待填：{got}"


def test_repair_never_touches_the_delete_sentinel() -> None:
    """**删除哨兵一个字符都不许动**，尤其是模板以 `＿＿` 开头的那几条诉请。

    这是端到端才抓出来的一处真事故：诉请条目的模板文字是
    `＿＿. 判令被告向原告支付＿＿…元；`，按空切出来的第一个字面段是**空串**，
    而空串在 `"__DELETE__"` 里当然找得到 —— 于是"模板字面段匹配上了"，
    哨兵被当成"写到一半的段落"，尾巴接上去变成
    `__DELETE__. 判令被告向原告支付＿＿…元；`。

    后果不只是难看：预览与 Word 两侧都是 `val == DELETE_MARK` **整串判等**，
    拼接之后再也认不出来 —— 该删的那条诉请删不掉，哨兵还会原样印进递法院的正文。
    （上一版用例的 hint 以 `原告：` 开头，第一个字面段对不上，于是侥幸通过了。）
    """
    cases = [
        # ← 以空开头，就是漏掉的那种形状
        "＿＿. 判令被告向原告支付＿＿＿＿＿＿＿＿＿＿人民币＿＿＿＿＿＿元；",
        "＿＿. 本案诉讼费用由被告承担。",
        "原告：＿＿＿＿，住址：＿＿＿＿＿＿＿＿＿＿＿＿。",
    ]
    for hint in cases:
        fields = [{"id": "p1", "kind": "para", "label": "", "key": hint[:40], "hint": hint}]
        got = ts.repair_truncated_paras({"p1": ts.DELETE_MARK}, fields)["p1"]
        assert got == ts.DELETE_MARK, f"哨兵被改了：\n  hint={hint!r}\n  got ={got!r}"


def test_repair_does_not_fire_on_hint_starting_with_a_blank() -> None:
    """同样是以 `＿＿` 开头的段落：正常的回填也不能被误接尾巴。

    空字面段在**任何**字符串里都"找得到"，所以判定"有没有匹配上"必须看
    **非空**字面段 —— 只判 `last < 0` 会被开头那个空串顶过去。
    """
    hint = "＿＿. 判令被告向原告支付＿＿＿＿＿＿＿＿＿＿人民币＿＿＿＿＿＿元；"
    fields = [{"id": "p1", "kind": "para", "label": "", "key": hint[:40], "hint": hint}]
    for val in ("1. 判令被告向原告支付违法解除赔偿金人民币288000元；",
                "3. 判令被告向原告支付未休年休假工资人民币66206元；"):
        got = ts.repair_truncated_paras({"p1": val}, fields)["p1"]
        assert got == val, f"正常条目被接了尾巴：{val!r} → {got!r}"


# ----------------------------------------------------------------- 模板形状（真文件）
def _real(path: str) -> Path:
    d = ts.template_dir()
    f = d / f"{path}.docx"
    if not f.exists():
        pytest.skip(f"模板 {path}.docx 不在 {d.name} 目录（换机器/CI 属正常缺失）")
    return f


@pytest.mark.parametrize("name,block_count", [
    ("劳动争议纠纷起诉状", 23),
    ("劳动争议纠纷答辩状", 19),
])
def test_rebuilt_templates_are_pure_prose(name: str, block_count: int) -> None:
    """重建后的两份模板：纯段落、无表格块、字段全是 `kind="para"`。

    段落式模板的字段 id 是**位置派生**的（`p{bi}`，`bi` = 非空段落的序号），
    所以块数本身就是契约的一部分 —— 多一段空行、少一段正文，回填就会整体错位
    一位，而且**不报任何错**（回填侧靠同一个游标对齐，错位是静默的）。
    """
    tpl = ts.parse_docx(_real(name))
    assert tpl["kind"] == "blank", f"{name} 不该被判成实例"
    assert len(tpl["blocks"]) == block_count, \
        f"{name} 块数变了（{len(tpl['blocks'])}≠{block_count}），字段 id 会整体错位"

    kinds = {b["t"] for b in tpl["blocks"]}
    assert not (kinds & {"row", "section"}), f"{name} 还残留表格块：{kinds}"
    assert not (kinds & {"hint"}), f"{name} 有填表说明段（导出 Word 时会被清空）"
    assert tpl["blocks"][0]["t"] == "title", "第一段应该是标题"

    for b in tpl["blocks"][1:]:
        assert b["t"] == "para", f"{name} 出现了非段落块：{b['t']}"

    fields = ts.fields_for_prompt(tpl)
    assert fields, f"{name} 一个待填字段都没有"
    assert {f["kind"] for f in fields} == {"para"}, "字段类型不止 para"


def test_complaint_request_items_are_renumberable() -> None:
    """诉请条目必须以 `＿＿.` 开头 —— `_ITEM_NO_RE` 靠它认出"这是可删可重编的条目"。

    认不出的后果有两个，都是静默的：整条请求删不掉（`__DELETE__` 无从下手），
    以及 `apply_amount_consistency` 拿这些条目**当准绳**去纠正别的段落
    （它们本该是被纠正的一方）。
    """
    tpl = ts.parse_docx(_real("劳动争议纠纷起诉状"))
    items = [b["text"] for b in tpl["blocks"] if ts._ITEM_NO_RE.match(b["text"] or "")]
    assert len(items) == 5, f"起诉状的诉请条目数是 {len(items)}，预期 5 条：{items}"
    assert any("诉讼费用" in t for t in items), "最后一条「诉讼费用由被告承担」不在里面"


def test_defense_paragraphs_relabel_respondent_first() -> None:
    """答辩状用 `被答辩人：`，且**不能**带「（原告）」后缀。

    `_fill_para` 的 `_SLOT_RULES` 是 `endswith` 匹配，标签多一个尾巴就认不出来，
    那一格会永远空着。而 `被答辩人` 又必须排在 `答辩人` 前面 ——
    「被答辩人」里含有「答辩人」，反过来写会把公司的名字填到劳动者那一格。
    """
    tpl = ts.parse_docx(_real("劳动争议纠纷答辩状"))
    labels = [b["text"].split("：")[0] for b in tpl["blocks"]
              if b["t"] == "para" and "：" in b["text"]]
    assert "被答辩人" in labels, f"没有「被答辩人：」这一段：{labels[:6]}"
    assert not any("（原告）" in lb for lb in labels), f"标签带了尾巴：{labels[:6]}"


# --------------------------------------------------------- 当事人段落不许整段删
def _para_fields(name: str) -> tuple[list[dict], dict[str, str]]:
    """真模板的段落字段 + 「栏目标签 → 字段 id」索引。"""
    fields = [f for f in ts.fields_for_prompt(ts.parse_docx(_real(name)))
              if f.get("kind") == "para"]
    by_label = {re.split(r"[：:]", f["hint"], maxsplit=1)[0].strip(): f["id"]
                for f in fields}
    return fields, by_label


def test_restores_the_respondent_line_the_model_tried_to_delete() -> None:
    """模型把答辩状的 `被答辩人：…` 整行删了 —— 要救回来（不是救回哨兵）。

    实况：材料里既给了被答辩人姓名、也写了「委托诉讼代理人肖明」，模型仍把
    `被答辩人：＿＿＿＿，＿＿，＿＿族，…` 和 `委托诉讼代理人：…` 两行都填成
    `__DELETE__` —— 那两段里它能填上的只有一格，其余（民族/出生/身份证号/
    住址/律所）材料里没有，于是把「这一格没材料」读成了「整段该删」
    （提示词第 9 条那句"材料里没有的整段删掉"太宽了）。

    后果不是版式问题：**当事人栏缺一块的答辩状立案庭不收**，而这份文书在页面上
    看着完全正常 —— 那两行就是没了，没有任何报错。
    """
    fields, by_label = _para_fields("劳动争议纠纷答辩状")
    p = by_label["被答辩人"]

    got = ts.restore_protected_paras({p: ts.DELETE_MARK}, fields, CASE_TEXT)
    assert got.get(p) != ts.DELETE_MARK, "被答辩人那一行还是被删了"
    assert got[p].startswith("被答辩人：张伟"), f"材料里能确定的姓名没填上：{got[p]}"
    # 材料里没有的格子必须留成待填 —— 这是"不编"的底线
    assert "＿＿" in got[p], f"材料里没有的信息被凭空填上了：{got[p]}"


def test_never_fills_a_label_word_as_the_party_name() -> None:
    """材料写成 `答辩人姓名：张伟` 时，不能把「姓名」两个字当成名字填进文书。

    `_NAME_RE` 的触发词里有「答辩人」，于是它先吃掉紧跟其后的「姓名」两个字 ——
    这一格填出来就是 `被答辩人：姓名，＿＿，…`，一份当事人栏写着"姓名"的答辩状。
    抓不到真名就留白，别拿栏目名充数。
    """
    fields, by_label = _para_fields("劳动争议纠纷答辩状")
    p = by_label["被答辩人"]

    got = ts.restore_protected_paras({p: ts.DELETE_MARK}, fields, "被答辩人姓名：张伟。")
    assert got.get(p) != ts.DELETE_MARK, "被答辩人那一行被删了"
    assert "姓名，" not in got.get(p, ""), f"栏目名被当成名字填进去了：{got.get(p)}"


# 用户那份答辩状案文里的**对方**代理人（起诉状那边她填在原告下面）。
# 材料里只有这一句 —— 我方（答辩人）没请代理人。
THEIR_AGENT_SRC = ("原告张伟委托诉讼代理人王敏，北京泽衡律师事务所律师，"
                   "联系电话：139 1023 4567。")
OUR_AGENT_SRC = ("我公司委托诉讼代理人肖明，北京泽衡律师事务所律师，"
                 "联系电话：010-85992244。")


def test_agent_line_is_kept_only_for_our_own_agent() -> None:
    """`委托诉讼代理人：…` 是当事人栏里**唯一**能合法删掉的一行，判据是**归谁**。

    ⚠️ 旧判据是"材料里出现过『代理人』三个字就保留"，于是用户那份案文里
       （材料只写了**原告**的代理人王敏）这一行被钉住，留成高亮空白。
       而照抄王敏进去更糟：那是"被告由原告的律师代理"，立案庭会挑出来。
    """
    fields, by_label = _para_fields("劳动争议纠纷答辩状")
    p = by_label["委托诉讼代理人"]

    no_agent = ts.restore_protected_paras({p: ts.DELETE_MARK}, fields,
                                          "我公司被张伟起诉了，需要一份答辩状。",
                                          "defense")
    assert no_agent[p] == ts.DELETE_MARK, "材料里没有代理人，这一行的删除被拦了"

    theirs = ts.restore_protected_paras({p: ts.DELETE_MARK}, fields,
                                        THEIR_AGENT_SRC, "defense")
    assert theirs[p] == ts.DELETE_MARK, "材料里只有对方的代理人，这一行该删掉"
    assert "王敏" not in str(theirs.get(p)), "把对方的律师填成我方的委托代理人了"

    ours = ts.restore_protected_paras({p: ts.DELETE_MARK}, fields,
                                      OUR_AGENT_SRC, "defense")
    assert ours.get(p) != ts.DELETE_MARK, "材料里给了我方代理人，那一行却被删了"
    assert "肖明" in ours[p], f"我方代理人的姓名没填上：{ours[p]}"
    assert "北京泽衡律师事务所律师" in ours[p], f"律所没填上或多填了后缀：{ours[p]}"


def test_agent_owner_follows_the_documents_side() -> None:
    """「我方」是谁**随文书立场变** —— 判反了就是删错人的律师。

    ⚠️ 两个真坑：
       · 同一份材料里 `被申请人中国XX公司委托诉讼代理人赵磊`，在**答辩状**里
         是我方（被诉的公司）的律师，在**仲裁申请书**里是对方的 —— 立场不同结论相反；
       · `被申请人` 里含有 `申请人`，顺序/护栏一松就会把"我方的"读成"对方的"。
       · 材料只写 `委托诉讼代理人王敏…`（没写谁委托的）时按立场兜底：
         答辩状按对方的算（材料里对方的主张是主角），起诉状按我方的算。
    """
    ours_bs = "被申请人中国XX公司委托诉讼代理人赵磊，北京天元律师事务所律师。"
    assert ts._agent_is_ours(ours_bs, "defense"), "答辩状里被申请人请的律师是我方的"
    assert not ts._agent_is_ours(ours_bs, "arbitration_application"), \
        "仲裁申请书里被申请人请的律师是对方的"

    # 起诉状里那行是**原告自己的**律师（材料没写"原告"两个字，靠立场兜底），
    # 不能按答辩状的判法删掉 —— 这条是上面那个 lookbehind 的回归。
    bare = "张伟，男，汉族。委托诉讼代理人王敏，北京泽衡律师事务所律师。"
    assert ts._agent_is_ours(bare, "complaint"), "起诉状里当事人自己的律师被判成对方的"
    assert not ts._agent_is_ours(bare, "defense"), "答辩状的材料里单提一个律师更可能是对方的"
    assert ts._agent_is_ours(bare), "认不出文书类型时不许删错 —— 默认按我方算"


def test_agent_line_is_filled_from_the_material_when_its_ours() -> None:
    """模型把这一行空着交（用户那份草稿 `p3` 就是缺的）→ 由材料填上。

    三格分开填。⚠️ 不能退回去用 `_fill_para`：那里「联系电话」的口径是材料里
    **第一个**电话（常常是当事人自己的手机号），「律所」那格更是永远认不出来 ——
    它前面紧挨着的正是还没填的姓名空。
    """
    fields, by_label = _para_fields("劳动争议纠纷答辩状")
    p = by_label["委托诉讼代理人"]
    hint = next(f["hint"] for f in fields if f["id"] == p)

    got = ts.resolve_agent_line({}, fields, OUR_AGENT_SRC)[p]
    assert "肖明" in got and "北京泽衡" in got and "010-85992244" in got, f"没填全：{got}"
    assert "律师事务所律师事务所" not in got, f"律所后缀重了：{got}"
    assert "＿＿" not in got, f"三格都该填上：{got}"
    assert got != hint


def test_agent_line_is_dropped_when_the_material_only_names_the_opponent() -> None:
    """用户 2026-09-28 定的口径：**有就填、没有就删掉**。

    那一行是当事人栏里唯一可以合法不存在的，而"填谁"必须先看归谁。
    这条正是答辩状上那个高亮空白的回归：材料里只有原告的律师王敏。
    """
    fields, by_label = _para_fields("劳动争议纠纷答辩状")
    p = by_label["委托诉讼代理人"]

    got = ts.resolve_agent_line({}, fields, THEIR_AGENT_SRC, "defense")
    assert got[p] == ts.DELETE_MARK, "材料里没有我方的代理人，这一行该整行删掉"
    assert "王敏" not in str(got), "把对方律师填成我方代理人了"


def test_never_touches_an_agent_line_the_model_already_filled() -> None:
    """模型自己填了的，一个字都不动 —— 哪怕材料里只有对方的代理人。

    删是"这一行没有材料可填"的处置，不是"材料里有律师就删"。
    """
    fields, by_label = _para_fields("劳动争议纠纷答辩状")
    p = by_label["委托诉讼代理人"]
    model_line = ("委托诉讼代理人：肖明，＿＿＿＿律师事务所律师，"
                  "联系电话：010-85992244。")

    got = ts.resolve_agent_line({p: model_line}, fields, THEIR_AGENT_SRC, "defense")
    assert got[p] == model_line, f"模型填过的值被动过：{got[p]}"


def test_drops_the_line_when_the_model_copied_the_other_sides_lawyer() -> None:
    """模型自己把**对方**的律师抄进我方那一行 → 照样清掉。

    这不是多余的：材料里写着「原告张伟委托诉讼代理人王敏…」，模型图省事照抄一遍，
    出来就是"答辩人委托诉讼代理人：王敏"—— 一份写着"被告由原告的律师代理"的
    答辩状，法院会挑出来。
    """
    fields, by_label = _para_fields("劳动争议纠纷答辩状")
    p = by_label["委托诉讼代理人"]
    copied = ("委托诉讼代理人：王敏，北京泽衡律师事务所律师，"
              "联系电话：139 1023 4567。")

    got = ts.resolve_agent_line({p: copied}, fields, THEIR_AGENT_SRC, "defense")
    assert got[p] == ts.DELETE_MARK, f"对方律师那一行没被清掉：{got[p]}"

    # 反过来，**起诉状**里王敏本来就是原告自己的律师 —— 一个字都不许动
    c_fields, c_by_label = _para_fields("劳动争议纠纷起诉状")
    cp = c_by_label["委托诉讼代理人"]
    kept = ts.resolve_agent_line({cp: copied}, c_fields,
                                 "张伟，男，汉族。"
                                 "委托诉讼代理人王敏，北京泽衡律师事务所律师。", "complaint")
    assert kept[cp] == copied, f"起诉状里当事人自己的律师被动过：{kept[cp]}"


def test_restores_the_signature_date_row() -> None:
    """落款 `日期：＿＿＿＿年＿＿月＿＿日` 被删时要填回今天。

    这一格不需要模型判断 —— 落款日期就是填表那天（律师会按实际提交日改），
    `rule_extract` 的规则路径本来就填今天，这里把同一条口径接到救回路径上。
    """
    fields, by_label = _para_fields("劳动争议纠纷起诉状")
    p = by_label["日期"]

    got = ts.restore_protected_paras({p: ts.DELETE_MARK}, fields, "张伟起诉北京鼎盛嘉业。")
    assert got.get(p) != ts.DELETE_MARK, "落款日期那一行被删掉了"
    assert got[p] == f"日期：{ts._today_cn()}", f"落款日期没填成今天：{got[p]}"


def test_still_honours_delete_on_request_items_and_argument_slots() -> None:
    """该删的还得能删 —— 这条兜底不许把「材料里没主张的请求」救回来。

    那正是提示词第 9 条存在的意义：没主张的诉请连同专门论述它的正文段落一起
    拿掉。救回逻辑一旦放宽到所有段落，这两处就会悄悄长回来。
    """
    fields, _ = _para_fields("劳动争议纠纷起诉状")
    items = [f["id"] for f in fields if ts._ITEM_NO_RE.match(f["hint"] or "")]
    assert items, "起诉状里没找到 `＿＿.` 开头的诉请条目"

    got = ts.restore_protected_paras({i: ts.DELETE_MARK for i in items}, fields, CASE_TEXT)
    assert all(got[i] == ts.DELETE_MARK for i in items), \
        f"诉请条目的删除被这条兜底救活了：{[got[i] for i in items]}"


# ------------------------------------------------------------------- 地址那格
# 模型返回的**真实** p1（2026-09-28 那份起诉状草稿）：整段都填了，只空着住址。
# 用真实串而不是手搓的，因为要防的正是「长段落里单独漏一格」这个形状。
ADDR = "北京市朝阳区常营北路9号院B区7号楼1203室（现住址）"
MODEL_P1_NO_ADDR = ("原告：张伟，男，汉族，1988年3月12日出生，"
                    "公民身份号码：110105198803124218，住址：＿＿＿＿＿＿＿＿＿＿＿＿，"
                    "工作单位：北京鼎盛嘉业信息技术有限公司，职务：软件研发工程师，"
                    "联系电话：138 1055 6789。")
ADDR_SRC = ("原告张伟，男，汉族，1988年3月12日出生，身份证号 110105198803124218，"
            f"住址：{ADDR}，联系电话：138 1055 6789。"
            "被告北京鼎盛嘉业信息技术有限公司，住所地：北京市朝阳区建国路88号"
            "SOHO现代城A座1801室。")


def test_backfills_the_address_the_model_left_blank() -> None:
    """材料里给了住址、模型把那一格空着 —— 照抄进去。

    实况：同一份案文在 31 份仲裁申请书里连着三次都把住址填对了，换成重建后的
    起诉状却把 `住址：＿＿` 原样留着（整段里别的格子一个没落下）。地址是
    「照抄就能对」的东西，不该交给模型掷骰子。
    """
    fields, by_label = _para_fields("劳动争议纠纷起诉状")
    p = by_label["原告"]

    got = ts.backfill_address_slots({p: MODEL_P1_NO_ADDR}, fields, ADDR_SRC)[p]
    assert f"住址：{ADDR}，" in got, f"住址没补上：{got}"
    assert "＿＿" not in got, f"还留着没填的空：{got}"
    # 只许动地址那一格 —— 别的格子模型填了什么就还是什么
    assert "工作单位：北京鼎盛嘉业信息技术有限公司" in got, f"把别的格子也改了：{got}"
    assert "职务：软件研发工程师" in got and "138 1055 6789" in got, got


def test_never_overwrites_an_address_the_model_already_chose() -> None:
    """模型自己填了地址就一个字都不动 —— 哪怕材料里另有一个。

    材料里常常有两个地址（`住址` 与 `通讯地址`/`户籍地址`），模型可能挑了更合适
    的那个口径；用正则覆盖回去等于拿"先出现的那个"去猜，比留白更糟。
    """
    fields, by_label = _para_fields("劳动争议纠纷起诉状")
    p = by_label["原告"]
    chosen = MODEL_P1_NO_ADDR.replace(
        "住址：＿＿＿＿＿＿＿＿＿＿＿＿", "住址：北京市朝阳区双桥东路12号院3号楼502室")

    got = ts.backfill_address_slots({p: chosen}, fields, ADDR_SRC)[p]
    assert got == chosen, f"模型选好的地址被覆盖了：{got}"


def test_leaves_the_address_slot_blank_when_the_material_has_none() -> None:
    """材料里没有地址就让它空着 —— 空格子由人工补，不许拿别处的地址凑。

    这条防的是「反向用力过猛」：兜底一旦变成"必须填上地址"，就会把合同里的
    「工作地点」「合同签订地」抓过来填进当事人栏。
    """
    fields, by_label = _para_fields("劳动争议纠纷起诉状")
    p = by_label["原告"]

    got = ts.backfill_address_slots({p: MODEL_P1_NO_ADDR}, fields,
                                    "张伟在北京市朝阳区建国路88号上班，月工资两万。")[p]
    assert got == MODEL_P1_NO_ADDR, f"材料里没地址却填了：{got}"


def test_address_labels_are_bound_to_their_own_party() -> None:
    """`住址`（劳动者）与 `住所地`（用人单位）是**两个**键 —— 共用一个就张冠李戴。

    这正是 `_SLOT_RULES` 里那对「被申请人/申请人」的老坑：一个标签是另一个的
    子串、或者干脆同义时，填错的后果是把原告的住址印在被告那一行上。
    """
    fields, by_label = _para_fields("劳动争议纠纷起诉状")
    p_yg, p_bg = by_label["原告"], by_label["被告"]
    hints = {f["id"]: f["hint"] for f in fields}

    got = ts.backfill_address_slots({p_yg: hints[p_yg], p_bg: hints[p_bg]}, fields, ADDR_SRC)
    assert "常营北路" in got[p_yg], f"原告的住址没填上：{got[p_yg]}"
    assert "SOHO" not in got[p_yg], f"把被告的住所地填进原告那格了：{got[p_yg]}"
    assert "SOHO" in got[p_bg], f"被告的住所地没填上：{got[p_bg]}"
    assert "常营北路" not in got[p_bg], f"把原告的住址填进被告那格了：{got[p_bg]}"
