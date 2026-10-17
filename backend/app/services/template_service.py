"""文书模板层：**把所里的 .docx 模板变成"发一段文字就能套用"的能力。**

为什么要单独一层（而不是塞进 DraftNode）：

1. `doc_tools.format_normalize` 只做"引用空格规范化"，**从不读用户的模板文件**；
   而律师要的是"我把案情发给你，你产出我们所惯用的那张表"。
2. 模板是 **.docx**，且有三种形态（表格型表单 / 段落型填写稿 / 已填实例），
   解析一次缓存成 JSON 即可，不必每次请求都解压 docx
   （35 份模板，实测全量解析约 1~2 秒，缓存后 <5ms）。
3. **模板结构不让模型碰。** 模型只负责"从用户文字里抽出字段值"，
   行/段落的骨架、固定条款、证据清单全部由模板原样带出 ——
   否则每次生成的结构都不一样，律师反而不敢用。

产物是 **Markdown** 正文：既能直接预览、编辑，也能导出 md/docx（`/drafts/{id}/exports`）。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from backend.app.config import DATA_DIR, PROJECT_ROOT, settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 类型与地区
DOC_TYPE_LABEL = {
    "complaint": "民事起诉状",
    "defense": "民事答辩状",
    "arbitration_application": "劳动仲裁申请书",
}

# 判定顺序有讲究：先"答辩"再"起诉"，最后"仲裁"兜底 ——
# 「劳动争议纠纷答辩状」里同时出现"劳动""争议""答辩"，先命中答辩才对。
_TYPE_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("defense", ("答辩状", "答辩意见", "答辩人", "被答辩人")),
    ("complaint", ("起诉状", "诉状", "具状人", "原告", "起诉")),
    ("arbitration_application", ("仲裁申请书", "劳动仲裁", "仲裁委", "仲裁请求", "申请人")),
]

REGIONS = [
    "北京", "天津", "上海", "重庆", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江",
    "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东",
    "广西", "海南", "四川", "贵州", "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆",
]

# 当事人写案情时通常写**城市**（"杭州""深圳""苏州"），而模板是按**省**分的。
# 没有这张表，31 份省级仲裁申请书就永远只会命中"上海"（默认第一份）。
CITY_TO_REGION = {
    "北京": "北京", "天津": "天津", "上海": "上海", "重庆": "重庆",
    "石家庄": "河北", "唐山": "河北", "保定": "河北", "太原": "山西", "大同": "山西",
    "呼和浩特": "内蒙古", "包头": "内蒙古", "沈阳": "辽宁", "大连": "辽宁",
    "长春": "吉林", "吉林市": "吉林", "哈尔滨": "黑龙江", "南京": "江苏",
    "苏州": "江苏", "无锡": "江苏", "常州": "江苏", "徐州": "江苏", "南通": "江苏",
    "杭州": "浙江", "宁波": "浙江", "温州": "浙江", "嘉兴": "浙江", "金华": "浙江",
    "合肥": "安徽", "芜湖": "安徽", "福州": "福建", "厦门": "福建", "泉州": "福建",
    "南昌": "江西", "赣州": "江西", "济南": "山东", "青岛": "山东", "烟台": "山东",
    "潍坊": "山东", "郑州": "河南", "洛阳": "河南", "武汉": "湖北", "宜昌": "湖北",
    "长沙": "湖南", "株洲": "湖南", "广州": "广东", "深圳": "广东", "佛山": "广东",
    "东莞": "广东", "珠海": "广东", "南宁": "广西", "柳州": "广西", "海口": "海南",
    "三亚": "海南", "成都": "四川", "绵阳": "四川", "贵阳": "贵州", "昆明": "云南",
    "拉萨": "西藏", "西安": "陕西", "宝鸡": "陕西", "兰州": "甘肃", "西宁": "青海",
    "银川": "宁夏", "乌鲁木齐": "新疆",
}

# 模板里的"还没填"长什么样：下划线、全角空格、方括号提示
_PLACEHOLDER_RE = re.compile(r"[＿_]{2,}|[（(][^）)]{0,40}(填写|列明|如涉及|若有|请)[^）)]{0,40}[）)]")

# 「当事人没主张这一项」的哨兵值：整条请求**从文书里拿掉**，不是留白。
# 一份要递到仲裁委的正式文书里，留着一行「裁决被申请人支付＿＿元」比删掉它更糟。
# ⚠️ 认这个哨兵的有**两处**：`render_markdown`（预览）和 `docs.py:filled_template_doc`
#    （Word 导出）。只改一处会出现"网页上删了、下载的 Word 里还在"。
DELETE_MARK = "__DELETE__"

# 纯"填写说明"段落（如"（请在此填写具体事实经过…）"）不是待填字段，
# 把它从字段清单和渲染结果里拿掉，否则模型会把它当正文再写一遍，导致重复。
_HINT_RE = re.compile(r"^[（(].{0,120}(请在此|请按|请根据实际情况|请列明|填写|如涉及|若有|请).{0,120}[）)]$")

# 模板里常把"出生日期"和"年/月/日"拆成四个字段。当"出生日期"已填完整日期时，
# 就不要再显示/回填单独的"年/月/日"，否则会出现"出生日期：1988年3月12日"后
# 又跟"年：1988 / 月：3 / 日：12"的重复。
_DATE_GROUP = {"出生日期": ["年", "月", "日"]}


def _filter_redundant_date_cells(
    cells: list[tuple[str, str, str, bool]],
    fills: dict[str, str],
) -> list[tuple[str, str, str, bool]]:
    """在一行 cells 里，如果主日期字段已填完整日期，就跳过冗余的拆分字段。"""
    master_keys = set(_DATE_GROUP.keys())
    master_id = ""
    master_val = ""
    for fid, key, _hint, _opt in cells:
        if key in master_keys:
            v = (fills.get(fid) or "").strip()
            # 认为"完整日期"：包含 年、月、日 三个字
            if v and "年" in v and "月" in v and "日" in v:
                master_id = fid
                master_val = v
                break
    if not master_id:
        return cells
    redundant = set(_DATE_GROUP.get(next(k for k in master_keys if k in [c[1] for c in cells]), []))
    return [c for c in cells if c[1] not in redundant]


def _is_hint_para(text: str) -> bool:
    text = (text or "").strip()
    if not text.startswith(("（", "(")):
        return False
    # 已经带下划线占位符的段落不是纯指示（如"申请人：＿＿…"），仍按待填处理
    if "＿" in text or "_" in text:
        return False
    return bool(_HINT_RE.search(text))


# ================================================================ 解析 docx
# 前导空白达到这个数，就认为"模板作者在用手打空格推位"。实测两份起诉状/答辩状的
# 落款分别是 11 个和 9 个空格，正文段落一个都没有。
_FAKE_INDENT_MIN = 6


def _norm_align(align: Any, text: str) -> str:
    """把 Word 里读到的对齐方式归一成 ``""``（左） / ``"center"`` / ``"right"``。

    **为什么必须归一，不能原样带出**：所里的模板把落款推到右下角用的手法是
    「段落居中 + 前面手打一串空格」（实测 `劳动争议纠纷起诉状.docx`：
    `具状人（签字、盖章）：` 前面 11 个，`日期：` 前面 9 个）。在 Word 里看着确实
    像右下角，但这份"位置"是**空格撑出来的** —— 一旦渲染成 HTML/Markdown，
    连续空格被折叠，落款就掉回最左边。用户报的"署名日期没有在右下角"就是这个。

    所以这里把「居中 + 明显前导空白」认成作者的真实意图 = **右对齐**，
    后面三端（预览 / Word / PDF）就能用真正的对齐属性还原它，不靠空格。

    返回空串表示左对齐 —— 不写进 block，省 index.json 的体积。
    """
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    if align is None:
        return ""
    if align == WD_ALIGN_PARAGRAPH.CENTER:
        # `　` 是全角空格，也有模板用它推位，一并算。
        lead = len(text) - len(text.lstrip(" 　"))
        return "right" if lead >= _FAKE_INDENT_MIN else "center"
    if align == WD_ALIGN_PARAGRAPH.RIGHT:
        return "right"
    # LEFT / JUSTIFY / DISTRIBUTE：两端对齐的短行视觉上就是左对齐，视作左。
    return ""


def _iter_body(doc: Any):
    """按**文档真实顺序**遍历段落与表格，产出 ``(kind, payload, align)``。

    起诉状/答辩状的正文全在表格里，而标题、落款在表格外 ——
    只用 `doc.paragraphs` 会丢掉整张表单，只用 `doc.tables` 又丢了标题与落款。

    段落的 ``payload`` 是**未 strip 的原文**：`_norm_align` 要靠前导空白判断
    "手推空格"，先 strip 就再也判不出来了。strip 由调用方 `parse_docx` 做，
    这样 `block["text"]` 的形状（标题判定的 `len<=60`、占位符正则）完全不变。
    表格的 ``align`` 恒为空串。
    """
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for child in doc.element.body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            p = Paragraph(child, doc)
            text = p.text or ""
            if text.strip():
                yield ("p", text, _norm_align(p.alignment, text))
        elif tag == "tbl":
            t = Table(child, doc)
            rows = []
            for r in t.rows:
                cells: list[str] = []
                seen: set[int] = set()
                for c in r.cells:
                    # 合并单元格在 python-docx 里会重复返回同一个 tc，按 id 去重
                    if id(c._tc) in seen:
                        continue
                    seen.add(id(c._tc))
                    cells.append((c.text or "").strip())
                if any(cells):
                    rows.append(cells)
            if rows:
                yield ("tbl", rows, "")


def parse_docx(path: Path) -> dict[str, Any]:
    """把一份 .docx 解析成「块序列 + 可填字段」。"""
    from docx import Document

    doc = Document(str(path))
    blocks: list[dict[str, Any]] = []
    titles: list[str] = []
    table_seen = False
    still_title = True     # 只有"开头连续几段短标题"才算标题，见下

    for kind, payload, align in _iter_body(doc):
        if kind == "p":
            text = payload.strip()
            # 「标题」必须是**开头的连续几段**：段落型模板（劳动仲裁申请书）正文里
            # 有一堆以"1. 请求裁决…"开头的短行，按"短且无冒号"判定会把它们误当副标题
            # （实测被吃成 ## 二级标题）。所以一旦出现过正文段，就再也不当标题。
            if (still_title and not table_seen and len(titles) < 3
                    and len(text) <= 60 and "：" not in text):
                titles.append(text)
                blocks.append({"t": "title", "text": text})
            else:
                still_title = False
                if _is_hint_para(text):
                    blocks.append({"t": "hint", "text": text})
                else:
                    # ``align`` 只在**非左对齐**时才落盘（左对齐是绝大多数段落，
                    # 存进去只会让 index.json 白白变大）。
                    blocks.append({"t": "para", "text": text,
                                   **({"align": align} if align else {})})
        else:
            still_title = False
            table_seen = True
            for cells in payload:
                if len(cells) == 1:
                    blocks.append({"t": "section", "text": cells[0]})
                else:
                    lines = [ln.strip() for ln in cells[1].split("\n") if ln.strip()]
                    blocks.append({"t": "row", "label": cells[0], "lines": lines or [""]})

    fields = _extract_fields(blocks)
    name = path.stem
    return {
        "id": name,
        "file": path.name,
        "title": (titles[0] if titles else name),
        "doc_type": detect_doc_type(name + "\n" + "\n".join(titles)),
        "region": detect_region(name),
        "kind": "example" if ("实例" in name or "样例" in name) else "blank",
        "blocks": blocks,
        "field_count": len(fields),
        "chars": sum((0 if b.get("t") == "hint" else len(b.get("text", "")))
                     + sum(len(x) for x in b.get("lines", []))
                     for b in blocks),
    }


def _split_line(line: str) -> list[tuple[str, str]]:
    """把一行拆成 [(key, hint)]。

    docx 表单里常见三种写法挤在一行：
      `姓名：               职务：            联系电话：`
      `是  否`、`有  无`（勾选项）
      `明细：`
    只按第一个冒号切，会把"职务/联系电话"整段塞进上一个值里（实测过）。
    """
    parts = re.split(r"[ 　]{2,}", (line or "").strip())
    out: list[tuple[str, str]] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        m = re.match(r"^([^：:]{1,24})[：:]\s*(.*)$", p)
        out.append((m.group(1).strip(), m.group(2).strip()) if m else (p, ""))
    return out or [("", "")]


# 「勾选项」而不是"待填值"：`是 否`、`有 无`、`男 女`、`有限责任公司 股份有限公司`
_OPTION_RE = re.compile(r"是[ 　]*否|有[ 　]*无|男[ 　]*女|^[^：:\d]{0,60}$")


def _is_option(key: str, hint: str, *, had_colon: bool) -> bool:
    """判断这一行是不是"打勾用的选项"。

    是选项就不该报"待填"—— 律师要的是照着勾，而不是看到一整页"（待填）"。
    """
    if not had_colon:                      # 整行没有冒号：本来就是选项行
        return True
    if not hint:                           # `明细：` 这种：是要填的
        return False
    if "＿" in hint or "_" in hint or _PLACEHOLDER_RE.search(hint):
        return False                       # 有占位符 → 是要填的
    if re.search(r"\d", hint):             # 带数字的基本是已填内容
        return False
    if re.search(r"是[ 　]*否|有[ 　]*无|男[ 　]*女", hint):
        return True
    # 一串 2~8 字的短词用空格分隔（选项组），且不含冒号/数字
    words = [w for w in re.split(r"[ 　]{2,}", hint) if w]
    return len(words) >= 2 and all(len(w) <= 12 and "：" not in w for w in words)


def _option_words(key: str, hint: str) -> list[str]:
    """把"男 女" / "有 无" / "是 否"这类提示拆成可勾选的词。

    ⚠ 预览（render_markdown）和 Word 回填（docs._fill_cell_from_block）**必须共用这一个函数**。
    两边拆出来的词数不一样，方框的"第 N 个"就对不上 ——
    预览里勾的是"男"，Word 里可能被勾成"女"。
    """
    src = (hint or "").strip() or key
    words = [w.strip() for w in re.split(r"[ 　]+", src) if w.strip()]
    return words or [key]


def _row_lines(block: dict[str, Any], bi: int):
    """把一行的各个片段分组：产出 (li, [(field_id, key, hint, is_option), ...])。"""
    for li, line in enumerate(block.get("lines") or []):
        had_colon = bool(re.search(r"[：:]", line or ""))
        cells = []
        for k, (key, hint) in enumerate(_split_line(line)):
            if not key:
                continue
            cells.append((f"r{bi}.{li}.{k}", key, hint, _is_option(key, hint, had_colon=had_colon)))
        if cells:
            yield li, cells


def _row_cells(block: dict[str, Any], bi: int):
    """扁平化版本（抽字段用）。"""
    for _li, cells in _row_lines(block, bi):
        for c in cells:
            yield c


def _extract_fields(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从块序列里找出"该填的地方"。

    - 表单行：每行 `键：值` → 一个字段（值可能是空，也可能是模板里的提示文字）
    - 自由段落：整段里含占位符/提示 → 一个"整段重写"字段
    """
    fields: list[dict[str, Any]] = []
    for bi, b in enumerate(blocks):
        if b["t"] == "row":
            for fid, key, hint, is_opt in _row_cells(b, bi):
                if not key:
                    continue
                fields.append({
                    "id": fid, "kind": "value", "option": is_opt,
                    "label": b.get("label", ""), "key": key, "hint": hint,
                })
        elif b["t"] == "para":
            text = b.get("text") or ""
            if _PLACEHOLDER_RE.search(text) or re.search(r"[＿_]{2,}", text):
                fields.append({
                    "id": f"p{bi}", "kind": "para",
                    "label": "", "key": text[:40], "hint": text,
                })
    return fields


# ---------------------------------------------------------------- 识别
def detect_doc_type(text: str) -> str:
    for doc_type, keys in _TYPE_RULES:
        if any(k in text for k in keys):
            return doc_type
    return "complaint"


def detect_region(text: str) -> str:
    """先认省份全称，再认省会/主要城市（"杭州"→浙江）。认不出返回空串。"""
    for r in REGIONS:
        if r in text:
            return r
    for city, region in CITY_TO_REGION.items():
        if city in text:
            return region
    return ""


# ================================================================ 索引
_INDEX_CACHE: dict[str, Any] = {"at": 0.0, "dir": "", "items": []}


# 当事人写案情时不会说"我要申请劳动仲裁"，他只会说"公司把我辞退了、还拖欠工资"。
# 所以要按**事实词**路由，不能只认"仲裁"两个字。
_LABOR_CASE_RULES = (
    # 术语（律师转述时会用）
    "劳动仲裁", "劳动纠纷", "劳动争议", "违法解除", "解除劳动合同", "未签劳动合同",
    "双倍工资", "经济补偿", "赔偿金", "加班费", "未休年假", "年休假",
    "社会保险", "社保", "工伤", "试用期", "竞业限制", "辞退", "开除", "劝退",
    # 事实（当事人自己写时只会说这些）—— 少了这一半就路由不动，
    # 实测「公司口头通知我第二天不用来了，还拖欠我2月的工资」全军覆没
    "入职", "工资", "劳动合同", "在职", "用人单位", "劳动报酬", "离职", "裁员",
)
# 但这些词一出现，说明当事人要的是去法院，不是仲裁
_LITIGATION_RULES = ("起诉状", "提起诉讼", "向法院起诉", "民事起诉", "法院起诉")


def detect_doc_type_for_case(text: str) -> str:
    """给**当事人写的案情**判类型；判不出就退回 ``detect_doc_type``。

    ⚠️ 与 ``detect_doc_type`` 分开是必须的：那个函数拿去判模板文件名，
       而所里 4 份起诉状/答辩状的文件名就叫「劳动争议纠纷起诉状.docx」，
       把「劳动争议」并进去，它们自己会被判成劳动仲裁申请书。
    """
    if any(k in text for k in _LABOR_CASE_RULES):
        if not any(k in text for k in _LITIGATION_RULES):
            return "arbitration_application"
    return detect_doc_type(text)


def template_dir() -> Path:
    """模板目录。**相对路径按项目根解析，不按进程的工作目录。**

    这条改起来只有一行，挡掉的却是一类会**静默复发**的故障：
    ``list_templates`` 在目录不存在时只记一条 warning 并返回空列表（见其 docstring），
    前端于是恒显示「没有找到模板」。而这个目录历史上已经错过两次 ——
    两次都不是"路径本身写错"，而是**参照系不对**：

    - 2026-09-24：开发机绝对路径 ``C:\\Users\\lzp\\…`` → 换机器必然不存在；
    - 2026-10-12：``legal-agent2/legal-agent/data/raw/劳动法/文书模板`` ——
      相对**工作区根**是对的，但后端进程的工作目录是 ``legal-agent2/legal-agent``，
      于是拼成 ``…/legal-agent2/legal-agent/legal-agent2/legal-agent/data/…``。

    按 :data:`PROJECT_ROOT` 解析之后，"相对路径"只剩一种含义，
    不再随"从哪里启动 uvicorn / 从哪个脚本 import"而变。
    ``PROJECT_ROOT`` 与 ``DATA_DIR`` 同源（``config.py``），所以默认值不受影响。
    """
    raw = (settings.DOC_TEMPLATE_DIR or "").strip()
    if not raw:
        # ``.env`` 里写成 ``DOC_TEMPLATE_DIR=``（空串）时，``Path("")`` 会变成 ``"."``，
        # 拿项目根去 glob ``*.docx`` 同样是 0 份 —— 而且**连"目录不存在"的 warning
        # 都不会打**（项目根当然存在）。所以这里退回 config.py 的默认目录，
        # 与 `DATA_DIR` 同源，不会各写一份而漂移。
        return DATA_DIR / "raw" / "劳动法" / "文书模板"
    p = Path(raw)
    return p if p.is_absolute() else (PROJECT_ROOT / p)


def list_templates(*, rebuild: bool = False) -> list[dict[str, Any]]:
    """列出模板（带缓存）。目录为空时返回空列表，**不抛错** ——
    模板没配好不该让整个页面不可用。"""
    d = template_dir()
    cached = _INDEX_CACHE
    if (not rebuild and cached["items"] and cached["dir"] == str(d)
            and time.time() - cached["at"] < 600):
        return cached["items"]

    if not d.exists():
        logger.warning("文书模板目录不存在：%s（可在 .env 里改 DOC_TEMPLATE_DIR）", d)
        _INDEX_CACHE.update({"at": time.time(), "dir": str(d), "items": []})
        return []

    # 落盘缓存：解析 35 份 docx 有成本，重启后能直接命中
    cache_file = Path(settings.DOC_TEMPLATE_INDEX)
    files = sorted(p for p in d.glob("*.docx") if not p.name.startswith("~$"))
    stamp = _dir_stamp(files)

    if not rebuild and cache_file.exists():
        try:
            blob = json.loads(cache_file.read_text(encoding="utf-8"))
            if blob.get("stamp") == stamp and blob.get("items"):
                _INDEX_CACHE.update({"at": time.time(), "dir": str(d), "items": blob["items"]})
                return blob["items"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("模板缓存读取失败，改为重新解析：%s", exc)

    items: list[dict[str, Any]] = []
    for p in files:
        try:
            items.append(parse_docx(p))
        except Exception as exc:  # noqa: BLE001
            logger.warning("模板解析失败，跳过 %s：%s", p.name, exc)

    # 类型/地区固定的排序：先空白模板，再实例；同类型内按地区稳定排序
    items.sort(key=lambda x: (x["doc_type"], 0 if x["kind"] == "blank" else 1, x["region"], x["id"]))

    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps({"stamp": stamp, "items": items}, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("模板缓存写入失败（不影响使用）：%s", exc)

    _INDEX_CACHE.update({"at": time.time(), "dir": str(d), "items": items})
    logger.info("文书模板已索引：%d 份（%s）", len(items), d)
    return items


def _dir_stamp(files: list[Path]) -> str:
    return "|".join(f"{p.name}:{p.stat().st_mtime_ns}" for p in files)


def get_template(template_id: str) -> dict[str, Any] | None:
    for it in list_templates():
        if it["id"] == template_id:
            return it
    return None


def pick_template(text: str, *, template_id: str = "", prefer_doc_type: str = "") -> dict[str, Any] | None:
    """选模板：显式指定 > 类型关键词 + 地区匹配 > 该类型第一份空白模板。

    「地区」只在同类型有多份时用于分叉（劳动仲裁申请书有 31 份省级版本）。
    """
    items = list_templates()
    if not items:
        return None
    if template_id:
        found = get_template(template_id)
        if found:
            return found

    # 用户案情用「事实词」路由，见 detect_doc_type_for_case 的说明
    doc_type = prefer_doc_type or detect_doc_type_for_case(text)
    same_type = [x for x in items if x["doc_type"] == doc_type] or items
    # 空白模板优先（实例是给人看的样例，不是交付形态）
    blanks = [x for x in same_type if x["kind"] == "blank"] or same_type

    region = detect_region(text)
    if region:
        for x in blanks:
            if x["region"] == region:
                return x
    return blanks[0]


# ================================================================ 渲染
def render_markdown(tpl: dict[str, Any], fills: dict[str, str] | None = None) -> tuple[str, list[str]]:
    """按模板骨架渲染 Markdown；返回 (正文, 未填字段说明)。

    **结构完全来自模板**，fill 只用来替换值。
    """
    fills = fills or {}
    out: list[str] = []
    missing: list[str] = []
    title_no = 0
    prev_kind = ""

    def blank():
        """块之间空一行 —— 但**连续段落之间不空**：docx 里一句话常被拆成多段
        （仲裁申请书的"申请人：…，" / "身份证号：…，" / "住址：…。"），
        每段之间插空行会把这句拆成三段，读起来像断句错误。"""
        if prev_kind and prev_kind != "para":
            out.append("")

    for bi, b in enumerate(tpl.get("blocks") or []):
        t = b["t"]
        if t == "title":
            blank()
            title_no += 1
            out.append(f"# {b['text']}" if title_no == 1 else f"## {b['text']}")
            out.append("")
        elif t == "section":
            blank()
            out.append(f"### {b['text']}")
            out.append("")
        elif t == "hint":
            # 填写说明不输出到最终文书，避免把"（请在此填写…）"当正文印出来
            continue
        elif t == "para":
            fid = f"p{bi}"
            filled = (fills.get(fid) or "").strip()
            # 整段删除：**连它前面那行空行一起**跳过（`blank()` 放到删除判断之后调），
            # 否则删掉三条请求会在「请求事项」下面留下三行空白。
            if filled == DELETE_MARK:
                continue
            blank()
            raw = filled or (b.get("text") or "")
            if not filled and _PLACEHOLDER_RE.search(raw):
                missing.append(raw[:60])
            # 版式（落款右对齐）跟着模板走，**填了值也照样套用** —— 见 `_norm_align`。
            # 用 `<p align="…">` 而不是私有的 `::: right` 之类标记：单行、正则可测，
            # 而且这份 md 导出后在任何 md 阅读器里**本身就是右对齐的**，
            # 换成私有标记就只是噪音。
            align = b.get("align") or ""
            out.append(f'<p align="{align}">{raw}</p>' if align else raw)
        elif t == "row":
            blank()
            label = b.get("label") or ""
            out.append(f"- **{label}**")
            for _li, cells in _row_lines(b, bi):
                # 整行都是勾选项（`是 否` / `有 无`）→ 合成一行，勾中的打 ☑，
                # 不铺成两三条"待填"（实测一页能刷出二十多条，没法看）
                cells = _filter_redundant_date_cells(cells, fills)
                if not cells:
                    continue
                # 勾选项一律渲染成"☐ 词 / ☑ 词"序列 —— 和 Word 回填用同一套拆词
                # （_option_words）与同一套勾选判断，保证方框逐个对得上。
                if len(cells) >= 2 and all(c[3] for c in cells) and not any(c[2] for c in cells):
                    opts = []
                    for fid, key, _hint, _opt in cells:
                        val = (fills.get(fid) or "").strip()
                        # 也要拆词（"机关法人 农村集体经济组织法人"其实是两个可勾项），
                        # 否则预览比 Word 少一半方框，勾选全部错位。
                        for w in _option_words(key, ""):
                            checked = bool(val) and (val == w or val == key)
                            opts.append(f"{'☑' if checked else '☐'} {w}")
                    out.append("  - " + "　".join(opts))
                    continue
                for fid, key, hint, is_opt in cells:
                    val = (fills.get(fid) or "").strip()
                    if is_opt:
                        opts = []
                        for w in _option_words(key, hint):
                            checked = bool(val) and (val == w or val == key)
                            opts.append(f"{'☑' if checked else '☐'} {w}")
                        out.append("  - " + "　".join(opts))
                    elif val:
                        out.append(f"  - {key}：{val}")
                    elif hint and not _PLACEHOLDER_RE.search(hint):
                        out.append(f"  - {key}：{hint}")
                    else:
                        out.append(f"  - {key}：（待填）")
                        missing.append(f"{label} · {key}".strip(" ·"))
            out.append("")
        prev_kind = t

    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    body = "\n".join([text, "", "---",
                      f"> {settings.DISCLAIMER}"])
    return body, missing


def fields_for_prompt(tpl: dict[str, Any]) -> list[dict[str, Any]]:
    return _extract_fields(tpl.get("blocks") or [])


# ================================================================ 组卷
_PROMPT = """你是律师助理。下面是一份文书模板的**待填字段清单**和当事人提供的**原始文字**。

要求：
1. 只输出 JSON：{{"fills": {{"<字段id>": "<填写内容>"}}}}
2. **能算出来的，一定要算出来填上**，不要留空：工作年限（起止日期）、
   按「月工资 × 月数 × 倍数」得出的赔偿金／经济补偿数额、有固定期限合同的到期日。
   金额用阿拉伯数字并保留算式，例如「12000元／月 × 3个月 × 2倍 ＝ 36000元」。
3. **但原文没有、又推不出来的，一律不要出现在 fills 里**：身份证号、
   统一社会信用代码、公司全称、法定代表人、住址、电话、开户行都不要编。
   拿不准就留空，让人工补。
   ⚠️ 反过来一样要紧：**原文里给了的，一格都不许落下**。住址、工作单位、职务、
   联系电话这些只要材料里写了，就必须填进对应的那一格 —— 整段照抄下来却空着
   其中一两格，等于把法院立案要的信息漏了。
4. 金额、姓名、公司名、日期**照抄原文的原口径**，不要改写成别的说法。
5. 落款：`申请人：＿＿＿＿＿＿`／`具状人：＿＿＿＿＿＿` 填**己方当事人**的名字；
   答辩状的 `答辩人：＿＿＿＿＿＿` 填**用人单位**（答辩人是被告方）。
   落款日期填**今天**（{today}），律师会按实际提交日改。
6. `kind=value` 的字段只填值（不要带 key，不要带"："）；
   `kind=para` 的字段要输出**该段的完整文字** —— 把占位符换成真实内容，
   段内其余固定表述（法条、连接词、标点）**一字不改地保留**，不许缩写、不许截断。
7. 上面第 3 条说的是**当事人给的原始文字**里出现的"＿＿"不要当信息用。
   模板字段本身自带的"＿＿"是**待你填的空**，该填的要填上。
8. **算经济补偿／赔偿金时，先把工作年限折算成"月数"再乘，别把零头丢掉**：
   每满 1 年 = 1 个月；余下满 6 个月不满 1 年 = 1 个月；余下不满 6 个月 = 0.5 个月。
   例：工作 1 年零 15 天 → 1 + 0.5 = **1.5 个月**；月工资 12000 元、违法解除按 2 倍，
   则赔偿金 = 12000 × 1.5 × 2 = **36000 元**。
   写进公式的月数必须是折算后的数（`1.5`），不能写成整数年份（`1`）。
   但「工作年限…共计＿＿」那一格要写**年月说法**（`1年零半个月`），
   填 `1.5个月` 进去这句话会读成"工作年限…共计1.5个月"。
9. **材料里没有的，整段删掉。** 以下四种情况，该字段的值就填 `__DELETE__`
   （我们会据此把**整段**从文书里移除；不要留空格、不要写"无"、不要写"略"）：
   · `请求事项`／`诉讼请求`／`答辩请求` 下面某一项，材料里根本没主张
     （没提加班费、没提未休年假、没要求确认劳动关系…）—— 整条删掉；
   · `事实与理由` 里**专门论述某一项请求**的那一段，而该项请求已被上面删掉
     （例：没主张未休年休假工资，则论述未休年休假的那一整段也一起删）；
   · 当事人**没有委托诉讼代理人**时的 `委托诉讼代理人：…` 那一整行 —— 材料里
     **只提了对方（原告／申请人）的代理人**也算没有：那是**对方的律师**，
     照抄进我方的文书就成了"我方由对方的律师代理"，法院会挑出来。
     只有材料明说是**我方**的代理人（`我公司委托诉讼代理人肖明…`）才填；
     `律师事务所` 这种材料里没有的格子留成 `＿＿` 待人工补，
     **不要因为某一格填不上就整行删掉**）；
   · 答辩状里材料中没有对应主张可辩的论证段（答辩状的 `事实与理由` 是几个
     论证槽，只留**用得上**的，用不上的整段删）。
   ⚠️ **当事人身份那几行永远不许删**：`原告：`／`被告：`／`申请人：`／`被申请人：`／
   `答辩人：`／`被答辩人：`／`法定代表人：`／`委托诉讼代理人：`（材料里提了**我方**代理人时）。
   材料里只给了其中一部分（比如只给了姓名，没给住址、身份证号、民族），
   那就把**能给的那部分填上、其余留成 `＿＿`**，绝不能填 `__DELETE__` ——
   少了当事人的文书递不进法院。这一条由代码兜底，你填了 `__DELETE__` 也会被退回来重填。
10. **条目序号也要你填**（就是每条形如 `＿＿. 裁决被申请人…` 开头那一格）。
    `请求事项`／`诉讼请求`／`答辩请求` 里**存活下来的**条目从 `1.` 开始连续编号，
    中间不许跳号 —— 删掉第 1 条后，原来的第 2 条要改成 `1.`。
    最后那条诉讼费／仲裁费承担的条目也要跟着重新编号。
11. **同一笔钱，数字处处一致。** `请求事项` 里写的金额，必须与 `事实与理由`
   里算式算出来的金额**一字不差**（算式得 36000 元，请求里就写 36000 元）。
   先把数算出来，再往两处填同一个数；不许一处写 48000、另一处写 105000。
12. `出生日期` 要与 `身份证号码` 对得上：18 位身份证号的第 7~14 位就是
   出生年月日（`110101199005124512` → `1990年5月12日`）。给了身份证号就按它推，
   不要另写一个日期。性别同理（第 17 位奇数为男、偶数为女）。

【待填字段】
{fields}

【案件证据链】
{chain}
  ↑ 这是本案已归档材料自动梳理出的「主张 → 支撑证据 → 缺口」，用法：
  · 「主张」用来确认请求事项与事实经过；「支撑证据」里的**材料名**可以写进
    「证据」「证人」这类栏目 —— **照抄材料名，不要改写、不要凭空造一份材料**。
  · 「缺口」表示该项主张**目前还没有材料支撑**：事实与理由里**不要为它编造**
    证据或细节，相关处留 `＿＿` 由律师补。但**不要**因为有缺口就把这一项请求
    整条删掉 —— 缺口是"还没举证"，不是"当事人没主张"。
  · 它与下面的【原始文字】冲突时，**一律以【原始文字】为准**。
  · 若这一节写着"未关联案件证据链"，就忽略它，按【原始文字】照常填写。

【原始文字】
{text}
"""


# ⚠️ 放在这里而不是 ``backend/llm/prompts.py``：``_PROMPT``（template_fill）
# 就在上面 —— 两个 prompt 是同一件事的两半（填 / 改），分开放会让人改了一个
# 忘了另一个。跟着本文件已有的约定走。
_REVISE_PROMPT = """你是律师助理。下面是一份**已经填好**的文书模板字段、以及用户提出的修改要求。

要求：
1. 只输出 JSON：{{"fills": {{"<字段id>": "<新的内容>"}}, "reply": "<一句话说明改了什么>"}}
2. **只能使用字段清单里已有的 id**，绝不允许自己造 id。
3. **只返回你要改的字段**，没改的不要出现在 fills 里（我们会自己保留原值）。
4. 金额、日期、姓名、公司名**照抄用户说的**，不要换算、不要改写、不要补全。
5. `kind=para` 的字段要输出**该段的完整文字**（替换掉要改的部分，保留其余固定表述），
   不是只给改动的片段。
6. **如果用户的要求落不到任何字段上**（例如"在正文中间加一整段新论述"、
   "把标题改成别的案由"），就把 fills 留空，并在 reply 里**如实说明做不到**，
   不要硬塞进某个不相关的字段，也不要编造一个新的栏目。

【字段清单】
{fields}

【当前已填内容】
{fills}

【用户的修改要求】
{instruction}
"""


async def revise_fills(
    tpl: dict[str, Any],
    fills: dict[str, str],
    instruction: str,
) -> dict[str, Any]:
    """按用户指令改模板字段，返回 ``(fills, reply, changed, degraded, provider)``。

    **只改 ``fills`` 这一层，不碰自由文本。** 这是一条刻意的边界：改了字段，
    "模板严格保真"还能成立（重渲染出来的仍是原模板的版式）；一旦允许往正文里
    插自由段落，导出就没法保证还长得像那份模板了。

    代价是**只能改模板里已有的栏目**。用户已经知道并选了这条；模型若判断要求
    落不到任何字段上，会被要求如实说明，而不是硬改。
    """
    fields = fields_for_prompt(tpl)
    by_id = {f["id"]: f for f in fields}
    brief = [{"id": f["id"], "kind": f["kind"], "label": f.get("label", ""),
              "key": (f.get("key") or "")[:80]} for f in fields]

    from backend.llm.client import llm_client

    await llm_client.reprobe()      # 同上：别让一次探针抖动钉死整个进程，见 compose()

    data, res = await llm_client.chat_json(
        [{"role": "user", "content": _REVISE_PROMPT.format(
            fields=json.dumps(brief, ensure_ascii=False, indent=1),
            fills=json.dumps({k: v for k, v in (fills or {}).items() if v},
                             ensure_ascii=False, indent=1),
            instruction=(instruction or "")[:2000])}],
        task="template_revise",
        payload={"fills": fills, "instruction": instruction},
        default={},
    )

    returned = data.get("fills") if isinstance(data.get("fills"), dict) else {}
    reply = str(data.get("reply") or "").strip()
    degraded = bool(res.degraded)

    # **以本地 fills 为基准做合并**，而不是拿模型返回的当新全集：
    # 模型经常只回它改过的那几个字段，直接替换会把其他栏目**整片清空**。
    new_fills = {k: v for k, v in (fills or {}).items() if v}
    changed: list[dict[str, str]] = []
    for k, v in (returned or {}).items():
        fid = str(k)
        if fid not in by_id:
            # 模型造了一个不存在的字段 id。丢掉 —— 写进去的话，
            # 渲染时它落不到任何模板块上，用户会看到"改成功了但文书没变"。
            logger.warning("revise 返回了不存在的字段 id，已忽略：%r", fid)
            continue
        new_val = ("" if v is None else str(v)).strip()
        old_val = (new_fills.get(fid) or "").strip()
        if new_val == old_val:
            continue
        new_fills[fid] = new_val
        changed.append({"id": fid, "label": (by_id[fid].get("label") or by_id[fid].get("key") or fid)[:40],
                        "old": old_val[:80], "new": new_val[:80]})
    new_fills = {k: v for k, v in new_fills.items() if v}
    # 改完也要再对一次身份证口径：用户可能正好改的是身份证号，
    # 那出生日期得跟着走，不然两份材料一对照就露馅。
    new_fills = apply_identity(new_fills, fields, tpl.get("blocks"))

    # 回复**由服务端按实际改动生成**，不采信模型的自述 ——
    # 模型说"已修改"而实际一处没动，是最容易让用户以为改成了的情况。
    if changed:
        heads = "、".join(c["label"] for c in changed[:4])
        more = f" 等 {len(changed)} 处" if len(changed) > 4 else ""
        summary = f"已修改：{heads}{more}。"
    else:
        summary = "没有找到可以修改的栏目 —— 这份模板里没有对应用户要求的字段。"
    if reply and not changed:
        summary = f"{summary}（模型说明：{reply[:160]}）"

    return {
        "fills": new_fills,
        "reply": summary,
        "changed": changed,
        "changed_count": len(changed),
        "degraded": degraded,
        "provider": res.provider,
    }


def _today_cn() -> str:
    """今天的「2026年9月28日」写法 —— 落款日期要用。"""
    from datetime import date

    d = date.today()
    return f"{d.year}年{d.month}月{d.day}日"


async def compose(text: str, *, template_id: str = "", prefer_doc_type: str = "",
                  chain_digest: str = "") -> dict[str, Any]:
    """一段文字 → 选模板 → 抽字段 → 按模板渲染。

    ``chain_digest`` 是**本案证据链的摘要**（由 ``chat._compose_chain`` 生成）。
    它只作为起草时的**事实与证据来源**之一进 prompt，**不留空也不报错**：
    传空串时 prompt 里那一节写"未关联案件证据链"，模型按【原始文字】照常填。
    这是"证据链不足也可以生成文书"在服务层的落点 —— 这里没有"链不完整就退"的分支。
    """
    tpl = pick_template(text, template_id=template_id, prefer_doc_type=prefer_doc_type)
    if tpl is None:
        # 同上：这条会被 ``chat.py`` 原样当成 HTTP 400 的 detail 返回给客户端，
        # 所以只给目录名，不给绝对路径。
        raise ValueError(
            f"没有可用模板：目录 {template_dir().name} 下没有 .docx。"
            "请在 .env 的 DOC_TEMPLATE_DIR 里指向模板目录。")

    fields = fields_for_prompt(tpl)
    fills: dict[str, str] = {}
    degraded = False
    provider = ""
    note = ""

    if fields and (text or "").strip():
        brief = [
            {"id": f["id"], "kind": f["kind"], "label": f.get("label", ""),
             "key": f.get("key", ""),
             # ⚠️ `kind=para` 的 hint 是**整段原文**，一个字都不能截 ——
             #    模型看不到整段就只能写半句，而渲染时是整段替换，
             #    于是「…工资共计人民币＿＿＿＿元未向申请人支付。」被顶成
             #    「…工资共计人」。实测就是这么被截断的。
             #    value 字段的 hint 只是个短提示，截断无妨。
             "hint": (f.get("hint") or "") if f.get("kind") == "para"
                     else (f.get("hint") or "")[:60]}
            for f in fields
        ]
        try:
            from backend.llm.client import llm_client

            # ⚠️ 这一句不能省。provider 只在首次调用时探测一次并缓存，而后端
            # **刚启动**那几秒探针很容易失手（进程正忙着加载向量模型），一旦
            # openai/ollama 都判不可用，RuleProvider 就被缓存成整个进程的默认，
            # **再也不会自己恢复**（见 `client.py:_reprobe_if_stuck_on_rule`）。
            # 症状正是用户报的那个：「生成的文书大片空白」——因为正则兜底只填得出
            # 姓名电话日期，而接口全程 200、日志只有一句不起眼的"已选定：rule"。
            # `report_node` 早就调了 `reprobe()`，文书这条链路一直漏着。
            await llm_client.reprobe()

            messages = [{"role": "user", "content": _PROMPT.format(
                fields=json.dumps(brief, ensure_ascii=False, indent=1),
                chain=(chain_digest or "").strip() or "（未关联案件证据链）",
                text=text[:6000],
                today=_today_cn())}]

            async def _ask():
                return await llm_client.chat_json(
                    messages, task="template_fill",
                    payload={"fields": brief, "text": text}, default={})

            data, res = await _ask()
            if res.degraded and res.provider == "rule":
                # 探针失手把 rule 钉死了（上面那段注释）。这里**当场**重探重来一次：
                # 网关抖动是秒级的，用户完全不该为此拿到一份"大片空白"的文书。
                # 只重试一次 —— 真不可用时多试只是拖长必然失败的请求。
                logger.info("文书填充落在规则引擎上，重探模型后重试一次")
                await llm_client.reset()
                data, res = await _ask()
            provider = res.provider
            if isinstance(data.get("fills"), dict):
                fills = {str(k): str(v) for k, v in data["fills"].items() if v not in (None, "")}
            else:
                fills = {str(k): str(v) for k, v in (data or {}).items() if isinstance(v, str)}
            degraded = bool(res.degraded)
        except Exception as exc:  # noqa: BLE001
            logger.warning("模板填充调用模型失败，改用规则抽取：%s", exc)
            degraded = True
            note = f"模型不可用（{type(exc).__name__}），已用规则抽取兜底"

    if not fills and (text or "").strip():
        fills = rule_extract(text, fields, tpl["doc_type"])
        if fills and not note:
            degraded = True
            note = "模型不可用或未返回字段，已用规则抽取兜底"

    # 模型把整段写了一半就用模板原文接回去，别让它决定固定表述写不写
    fills = repair_truncated_paras(fills, fields)
    # 当事人身份／落款段落不许整段删 —— 模型删了就在这里救回来，见 `restore_protected_paras`
    # （`doc_type` 是文书立场：「我方」是谁全靠它 —— 答辩状里被申请人是我方，仲裁申请书里是对方）
    fills = restore_protected_paras(fills, fields, text, tpl["doc_type"])
    # 材料里有地址、模型那格空着 → 照抄进去（地址没有算式可推，只能抄）
    fills = backfill_address_slots(fills, fields, text)
    # 委托代理人那一行：有我方代理人就填上，没有（或只有对方的）就整行去掉
    fills = resolve_agent_line(fills, fields, text, tpl["doc_type"])
    # 清掉被主日期字段覆盖的冗余拆分字段，避免 Word 回填时再出现"年/月/日"
    fills = _dedupe_date_fills(fills, fields)
    # 出生日期/性别以身份证号为准 —— 模型给的那版一律盖掉，见 `apply_identity`
    fills = apply_identity(fills, fields, tpl.get("blocks"), text)
    # 请求事项的金额以正文算式为准，见 `apply_amount_consistency`
    fills = apply_amount_consistency(fills, fields, tpl.get("blocks"))

    content, missing = render_markdown(tpl, fills)
    return {
        "template": {
            "id": tpl["id"], "file": tpl["file"], "title": tpl["title"],
            "doc_type": tpl["doc_type"], "doc_type_label": DOC_TYPE_LABEL.get(tpl["doc_type"], tpl["doc_type"]),
            "region": tpl["region"], "kind": tpl["kind"],
        },
        "doc_type": tpl["doc_type"],
        "region": tpl["region"],
        "filled": [k for k, v in fills.items() if v],
        "fills": fills,
        "filled_count": len([v for v in fills.values() if v]),
        "field_count": len(fields),
        "missing": missing[:24],
        "missing_count": len(missing),
        "content": content,
        "degraded": degraded,
        "provider": provider,
        "note": note,
    }


def _dedupe_date_fills(
    fills: dict[str, str],
    fields: list[dict[str, Any]],
) -> dict[str, str]:
    """如果 fills 里已有完整"出生日期"，就把同组的"年/月/日"值清空。"""
    master_ids: set[str] = set()
    for f in fields:
        if f.get("key") in _DATE_GROUP:
            fid = f.get("id") or ""
            v = (fills.get(fid) or "").strip()
            if v and "年" in v and "月" in v and "日" in v:
                master_ids.add(fid)
    if not master_ids:
        return fills
    redundant_keys = set()
    for mid in master_ids:
        redundant_keys.update(_DATE_GROUP.get("出生日期", []))
    out = dict(fills)
    for f in fields:
        if f.get("key") in redundant_keys and f.get("id") in out:
            out[f["id"]] = ""
    return out


# ---------------------------------------------------------------- 规则兜底
_AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(万元|元)")
_DATE_RE = re.compile(r"(\d{4})\s*[-年/.]\s*(\d{1,2})\s*[-月/.]\s*(\d{1,2})")
# ⚠️ 数字类都要卡边界。不卡的话 `110101199003074512` 里的
#    `19900307451` 会被当成手机号抽出来 —— 实测就这么干过。
_TEL_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
# 统一社会信用代码：18 位，字符集按国标 GB 32100（不含 I O S V Z）
_USCC_RE = re.compile(r"(?<![0-9A-Z])[0-9A-HJ-NPQRTUWXY]{18}(?![0-9A-Z])")
# `2023年3月1日入职某某科技有限公司` 会让 _COMPANY_RE 连前面的「日入职」一起吃进去。
# 只削这一种「日期尾巴 + 入职/离职」的前缀，不碰真的以「日」开头的名字（日立…）。
# `2023年3月1日入职某某科技有限公司` 会让 _COMPANY_RE 连前面的「日入职」一起吃进去；
# `2022年6月10日我入职成都某某科技有限公司` 还会夹一个「我」。都要削掉。
# ⚠️ `日` 只在**后面跟着我/入职/离职**时才削 —— 否则「日立（中国）有限公司」
#    会被削成「立（中国）有限公司」。
_COMPANY_HEAD_RE = re.compile(
    r"^(?:[日号](?=我|本人|咱|入职|离职)|我|本人|咱们|咱|入职|离职|从|在|于)+")
# 「我们公司」「该公司」是当事人口语里的指代，不是被申请人名称 ——
# 抽出来填进「被申请人」那一栏，文书上就写着"被申请人：我们公司"。
_COMPANY_PRONOUN_RE = re.compile(r"^(?:我们|咱们|咱|我|本|该|贵|你们|你|他们|对方)+")
# 抓到的"名字"本身是栏目名时不算数：材料写成 `答辩人姓名：张伟` 时，
# `答辩人` 这个触发词会先吃掉紧跟的「姓名」两个字，填出来就是
# `被答辩人：姓名，…` —— 一份当事人栏写着"姓名"两个字的文书。宁可留白。
_NAME_STOPWORDS = frozenset({"姓名", "名称", "本人", "全称", "单位", "公司"})
# 地址：**只能照抄**，所以只认「标签：值」这种写法 —— 没有标签就不抽。
# 合同里的「工作地点」「合同签订地」也是地址形状，抓错了就是往当事人栏里填错地址。
# 字符类写全了地址里会出现的字（汉字/字母/数字/括号/连字符/间隔号/空格），
# 换行不在里面 —— 不然会顺着行尾把下一行的内容一起吃进来。
# ⚠️ 每条标签配**自己的** bag 键：`住址` 是劳动者的、`住所地` 是用人单位的，
#    共用一个键就会把原告的住址填进被告那一格。
_ADDR_RE = re.compile(
    "(住址|住所地|通讯地址|户籍地址|现住址)[：:][ 　]*"
    "([一-龥A-Za-z0-9 　（）()－·-]{4,60})")
# ---- 委托代理人（2026-09-28）
# 材料里的代理人提法（**不锚定**，用于正文）。段落标签那个锚定版本见 `_AGENT_LABEL_RE`。
_AGENT_IN_TEXT_RE = re.compile("(?:委托诉讼代理人|委托代理人|代理律师|代理人)[：:，,、 　]*")
# 「我方」的指代。材料里写「**我公司**委托诉讼代理人肖明」时，那一句才是答辩人／
# 被告方的代理人，才轮得到文书里那一格。只写「原告委托诉讼代理人王敏」时，
# 那是**对方**的律师 —— 照抄进答辩状就成了"被告由原告的律师代理"，法院会挑出来。
_SELF_WORDS = "我方|我公司|我司|本公司|本单位"
# ⚠️ 同一份材料，**文书立场不同结论就相反**：「被申请人中国XX公司委托诉讼代理人
#    赵磊」在答辩状里是我方（被诉的公司），在仲裁申请书里是对方（公司请的律师）。
_PARTY_SELF_BY_DOC = {
    "defense": "答辩人|被告|被申请人|被告人",
    "complaint": "原告|申请人|起诉人|上诉人",
    "arbitration_application": "申请人|原告|上诉人",
}
_PARTY_THEIRS_BY_DOC = {
    "defense": "原告|申请人|起诉人|上诉人|被答辩人",
    "complaint": "被告|被申请人|被上诉人|被告人",
    "arbitration_application": "被申请人|被告|被上诉人",
}
# 当事人称呼都没出现时（材料只写 `委托诉讼代理人王敏…`）按立场推：答辩状的材料
# 是拿来答辩的、对方的主张是主角，单提一个律师更可能是**对方**的；起诉状／
# 仲裁申请书是当事人自己讲的事，更可能是我方的；认不出文书类型就当我方 ——
# 宁可留一行空白的当事人栏，也不把当事人自己的律师删掉。
_THEIRS_BY_DEFAULT = frozenset({"defense"})
_AGENT_SELF_WINDOW = 16      # 往前看多少字来判断这个代理人归谁
_AGENT_SEG = 60              # 往后取多少字当作这个代理人的一句话
_AGENT_NAME_RE = re.compile("[一-龥]{2,4}(?![一-龥])")
_LAW_FIRM_RE = re.compile("[一-龥]{2,20}律师事务所")
# 电话：`139 1023 4567` / `010-8599 2233` 这种带空格、连字符的写法很常见，
# 而 `_TEL_RE` 只认连着的 11 位手机号 —— 材料里这几个号一个都抽不出来。
_TEL_LOOSE_RE = re.compile(
    "(?<![0-9-])(?:1[3-9][0-9](?:[ 　-]?[0-9]){8}"
    "|0[0-9]{2,3}(?:[ 　-]?[0-9]){7,8})(?![0-9-])")
_ID_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?![\dXx])")
_COMPANY_RE = re.compile(r"[\u4e00-\u9fa5（）()]{2,24}?(?:有限公司|有限责任公司|股份有限公司|公司)")
# 「我叫张三」是当事人最自然的说法，不认它等于白抽。
# ⚠️ `被申请人` 必须排在 `申请人` 前面：否则引擎会从「被」字往后挪一格，
#    拿 `申请人` 去匹配 `被申请人` 的后半截，把后面的公司名抓成姓名。
_NAME_RE = re.compile(r"(?:我叫|我是|本人|原告|答辩人|被申请人|被告|申请人|姓名)[：:是为\s]*([\u4e00-\u9fa5]{2,4})(?![\u4e00-\u9fa5])")


# 被汉字隔开的日期占位（`＿＿＿＿年＿＿月＿＿日`）必须**整组**替换。
# 一个空一个空地换会把 `1990-05-12` 塞进第一个空里，变成
# `1990-05-12年＿＿月＿＿日` —— 比不填还难看。
_DATE_BLANK_RE = re.compile(r"[＿_]{2,}\s*年\s*[＿_]{1,3}\s*月(?:\s*[＿_]{1,3}\s*日)?")
# 日期空周围出现这些词，才敢把抽到的那一个日期填进去（见 `_fill_para`）
_DATE_EVENT_RE = re.compile(r"入职|离职|解除|终止|辞退|开除|劝退|签订|建立劳动")
_BLANK_RE = re.compile(r"[＿_]{2,}")
# 「请求事项」条目的序号空：`＿＿. 裁决被申请人…`。它在段首，前面没有字，
# `_fill_para` 的"看前一个标签"判定天然认不出它，得单独按出现顺序编号。
_ITEM_NO_RE = re.compile(r"^[＿_]{2,}\.")
# 落款日期整行（`日期：＿＿＿＿年＿＿月＿＿日`）。**必须整行匹配**：
# 只认「日期」两个字的话，「出生日期：」也会被算进来，把今天的日期填进出生日期。
_SIGN_DATE_RE = re.compile(
    r"^日期[：:]\s*[＿_]{2,}\s*年\s*[＿_]{1,3}\s*月\s*[＿_]{1,3}\s*日\s*$")
# 空前面这几个字决定它该填什么。**顺序有讲究**：`被申请人` 必须排在 `申请人` 前面 ——
# 「被申请人」里含有「申请人」，反过来写会把劳动者的名字填到公司那一栏。
_SLOT_RULES: tuple[tuple[str, str], ...] = (
    ("统一社会信用代码", "信用代码"),
    ("被申请人", "公司"),
    ("公司全称", "公司"),
    # ⚠️ 这三条**顺序不能换**，靠 endswith 逐条卡：
    #    `身份证号码` 既不以「身份证号」结尾、也不以「身份证」结尾，
    #    少了第一条就会一路漏到最后，空着。
    ("身份证号码", "身份证"),
    ("身份证号", "身份证"),      # `身份证号` 不以「身份证」结尾，endswith 卡得住
    ("身份证", "身份证"),
    ("联系电话", "电话"),
    ("电话", "电话"),
    ("（签名）", "姓名"),        # 落款「申请人（签名）：＿＿＿」
    ("申请人", "姓名"),
    ("姓名", "姓名"),
    # ---- 起诉状 / 答辩状（2026-09-28 段落化重建后）新增的标签。
    # ⚠️ `被答辩人` **必须排在 `答辩人` 前面** —— 「被答辩人」里含有「答辩人」，
    #    反过来写会把公司的名字填到劳动者那一格。跟上面「被申请人/申请人」同一个坑。
    ("被答辩人", "姓名"),
    ("答辩人", "公司"),          # 答辩状里答辩人＝被告＝用人单位
    ("原告", "姓名"),
    ("被告", "公司"),
    ("具状人", "姓名"),
    # ---- 地址（2026-09-28）。`_ADDR_RE` 抽出来的键就是标签本身，一条标签一个键，
    #      `住址`（劳动者）与 `住所地`（用人单位）绝不能共用。
    ("通讯地址", "通讯地址"),
    ("户籍地址", "户籍地址"),
    ("现住址", "现住址"),
    ("住所地", "住所地"),
    ("住址", "住址"),
)
_SLOT_WINDOW = 14      # 往回看多少个字来判定这个空归谁


def _fill_para(orig: str, bag: dict[str, str]) -> str:
    """把抽到的值填进**原段落的空里**，其余固定表述一字不动。

    ⚠️ 这是 `kind="para"` 字段的契约：字段值是「该段的完整文字」，不是裸值。
       以前这里把 `2023-03-01` 这种碎片直接当整段塞回去，整句话就没了；
       更糟的是「6. 办理材料：…申请人身份证复印件…」被「身份证」三个字命中，
       整条注意事项被换成一串身份证号 —— 模板条款直接消失，且不报错。
    """
    out = orig
    # 正则只抽得出一个日期。要不要填进这个空，得看**空前后有没有说这是个什么日子**：
    #   · `申请人于【空】入职被申请人处` —— 后面跟着"入职"，填；
    #   · `出生日期：【空】` —— 出生日期由身份证推（见 `_apply_identity`），这里不填；
    #   · `【空】，被申请人单方面通知解除` ／ `尚有【空】的工资` —— 前后都没有事件词，
    #     分不清是解除日还是欠薪月份，**留白**。
    # 这里以前是"排除出生"的黑名单，于是把入职日期原样填进了「解除日期」那一格。
    if (m := _DATE_BLANK_RE.search(out)) and bag.get("日期"):
        near = out[max(0, m.start() - 6):m.start()] + out[m.end():m.end() + 6]
        if "出生" not in near and _DATE_EVENT_RE.search(near):
            y, mo, d = bag["日期"].split("-")
            txt = f"{int(y)}年{int(mo)}月"
            if "日" in m.group(0):     # 模板写的是 `＿＿年＿＿月入职`，就别硬加个日
                txt += f"{int(d)}日"
            out = out[:m.start()] + txt + out[m.end():]

    def sub(m) -> str:
        # 这个空归谁管，只看**紧挨着它、且以标签收尾**的那一小段。
        # 两条限制都是踩出来的：
        #   · 切到上一个标点 —— 「隔了一个逗号的词」不该再管这个空，
        #     否则 `…入职被申请人处，担任＿＿岗位` 的岗位会被填成公司名；
        #   · 必须 **endswith** 标签 —— 用 `in` 的话
        #     `请求裁决被申请人支付拖欠工资人民币＿＿元` 会命中「被申请人」，
        #     把公司名填进工资金额里。
        # 注意 `：` 不能当分隔符，它正是「标签：空」里的那个标签。
        head = re.split(r"[，。；、;,\n]", out[:m.start()])[-1][-_SLOT_WINDOW:]
        head = head.rstrip("：: 　\t")
        for word, bag_key in _SLOT_RULES:
            if head.endswith(word) and bag.get(bag_key):
                return bag[bag_key]
        return m.group(0)      # 认不出来就留着空 —— 宁可让人工补，也不猜

    return _BLANK_RE.sub(sub, out)


# ---------------------------------------------------------------- 身份证口径
# 18 位身份证：第 7~14 位＝出生年月日，第 17 位奇男偶女。
# 「出生日期」在段里可能还空着、也可能已被模型写成一版 —— 两种形状都要认得出。
_BIRTH_IN_PARA_RE = re.compile(
    r"出生日期[：:]\s*(?:[＿_]{2,}\s*年\s*[＿_]{1,3}\s*月(?:\s*[＿_]{1,3}\s*日)?"
    r"|\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)")
# 身份证号那一格的**标签**：起诉状/答辩状写「公民身份号码」，申请书写「身份证号码」。
# `身份(?:证)?号码` 一条盖住两种（注：`身份证号码` 里并不含 `身份号码` 这个子串，
# 中间夹着「证」，所以不能省掉这个可选组）。
_ID_LABEL_RE = re.compile(r"(?:公民)?身份(?:证)?号码")
# ⚠️ 用**捕获组**而不是定宽 lookbehind：`(?<=身份证号码[：:])` 的宽度是死的，
#    塞不进可选的「公民」。标签留在 group(1) 里，替换时拼回去。
_ID_SLOT_IN_PARA_RE = re.compile(
    r"((?:公民)?身份(?:证)?号码[：:])\s*(?:[＿_]{2,}|\d{17}[\dXx])")
# 起诉状/答辩状把出生日期写在「出生」**前面**：`＿＿＿＿年＿＿月＿＿日出生，公民身份号码：…`。
# `_BIRTH_IN_PARA_RE` 只认 `出生日期：＿＿…` 的申请书写法，盖不住这一种。
# `(?!效)` 挡掉「合同自 2022 年 7 月 1 日出生效」这种同形（"日出生" 连着读）。
_BIRTH_BEFORE_RE = re.compile(
    r"(?:[＿_]{2,}\s*年\s*[＿_]{1,3}\s*月\s*[＿_]{1,3}\s*日"
    r"|\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)(?=出生(?!效))")
# 「申请人：张三，【性别】，汉族」——只认夹在两个逗号之间、后面紧跟「…族」的那一格，
# 别处没有同形状的空，不会误伤。
_SEX_IN_PARA_RE = re.compile(r"(?<=，)(?:[＿_]{2,}|\d|男|女)(?=，[＿_]{1,3}族)")


def apply_identity(
    fills: dict[str, str],
    fields: list[dict[str, Any]],
    blocks: list[dict[str, Any]] | None = None,
    source_text: str = "",
) -> dict[str, str]:
    """用**身份证号**把出生日期、性别钉死，并让号本身在文书里处处一致。

    用户的要求是「身份证和出生年月日要对上」。这两项本来就都能从身份证算出来，
    交给模型去写只是多给它一次写岔的机会 —— 实测出过身份证 `…19900512…`
    配出生日期 `2023-03-01`（那是入职日期）。改成**能算的就不问**。

    找不到身份证号就原样返回：没有号就没有可对齐的口径，凭空造一个更糟。
    """
    idno = ""
    # ① 独立的「身份证」字段（表格型模板）
    for f in fields:
        if f.get("kind") == "para":
            continue
        if "身份证" in ((f.get("key") or "") + (f.get("label") or "")):
            v = (fills.get(f.get("id")) or "").strip()
            if _ID_RE.fullmatch(v):
                idno = v
                break
    # ② 段落正文里（申请书型模板把身份证号写在申请人那一段）
    if not idno:
        for f in fields:
            if f.get("kind") != "para":
                continue
            m = _ID_RE.search(fills.get(f.get("id")) or f.get("hint") or "")
            if m:
                idno = m.group(0)
                break
    # ③ 当事人给的原始文字
    if not idno and source_text:
        m = _ID_RE.search(source_text)
        idno = m.group(0) if m else ""
    if not idno:
        return fills

    y, mo, d = int(idno[6:10]), int(idno[10:12]), int(idno[12:14])
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return fills          # 号本身不像真的，别拿它去改文书
    birth = f"{y}年{mo}月{d}日"
    sex = "男" if int(idno[16]) % 2 else "女"

    out = dict(fills)
    for f in fields:
        if f.get("kind") != "para":
            continue
        fid = f.get("id") or ""
        txt = out.get(fid) or f.get("hint") or ""
        if not txt:
            continue
        new = txt
        # 两条出生日期正则互不重叠：一条要求 `出生日期：` 前缀（申请书型），
        # 一条要求 `出生` 后缀（起诉状/答辩状型），所以放宽成 "出生" 之后
        # 也不会对同一处替换两次。
        if "出生" in new:
            new = _BIRTH_IN_PARA_RE.sub(f"出生日期：{birth}", new)
            new = _BIRTH_BEFORE_RE.sub(birth, new)
        if "族" in new:
            new = _SEX_IN_PARA_RE.sub(sex, new)
        if _ID_LABEL_RE.search(new):
            # 标签在 group(1)，替换时拼回去 —— 不然「公民身份号码：」这五个字会被号吞掉
            new = _ID_SLOT_IN_PARA_RE.sub(lambda m: m.group(1) + idno, new)
        if new != txt:
            out[fid] = new
    return out


# 「请求事项」里那句 `…支付<项目>人民币<N>元；` 的 N，以及正文里等式右边的结果
_FORMULA_RESULT_RE = re.compile(r"[=＝]\s*([\d,]+(?:\.\d+)?)\s*元")
_WAGE_TOTAL_RE = re.compile(r"共计人民币\s*([\d,]+(?:\.\d+)?)\s*元")
_ITEM_AMOUNT_RE = re.compile(r"人民币\s*([\d,]+(?:\.\d+)?)\s*元")


def apply_amount_consistency(
    fills: dict[str, str],
    fields: list[dict[str, Any]],
    blocks: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """把「事实与理由」里算出来的金额，钉到「请求事项」对应的那一条上。

    实测：同一个案子，请求里写 `54000元`、正文算式里写 `9000 × 4.5 × 2 ＝ 81000元`。
    正文算得对，请求抄错了 —— 一份文书里两个数，律师拿去立案是要被退的。
    提示词已经交代过"数字处处一致"，没用：模型是先填请求、后写正文，
    两处各算各的。**能算的东西就不能交给模型自觉**，和出生日期同一个道理。

    只认两类有权威出处的数：赔偿金的算式结果、欠薪的"共计"额。
    找不到权威出处就原样放行 —— 没有依据时乱改才是更糟的。
    """
    if not blocks:
        return fills
    truth: dict[str, str] = {}
    for bi, b in enumerate(blocks):
        if b.get("t") != "para":
            continue
        # 请求条目本身是**被纠正的一方**，不能拿它当准绳
        if _ITEM_NO_RE.match(b.get("text") or ""):
            continue
        txt = (fills.get(f"p{bi}") or "").strip()
        if not txt:
            continue
        if "赔偿金" in txt and (m := _FORMULA_RESULT_RE.search(txt)):
            truth["赔偿金"] = m.group(1)
        elif (m := _WAGE_TOTAL_RE.search(txt)):
            truth["工资"] = m.group(1)
    if not truth:
        return fills

    out = dict(fills)
    for bi, b in enumerate(blocks):
        if b.get("t") != "para" or not _ITEM_NO_RE.match(b.get("text") or ""):
            continue
        fid = f"p{bi}"
        txt = (out.get(fid) or "").strip()
        if not txt or txt == DELETE_MARK:
            continue
        for topic, want in truth.items():
            if topic not in txt:
                continue
            if (m := _ITEM_AMOUNT_RE.search(txt)) and m.group(1) != want:
                logger.info("请求事项金额 %s 与正文算式 %s 不一致，已按正文订正", m.group(1), want)
                txt = txt[:m.start(1)] + want + txt[m.end(1):]
                out[fid] = txt
    return out


_BLANK_RUN_RE = re.compile(r"[＿_]{2,}")


def _hint_skeleton(hint: str) -> tuple[list[str], list[int]]:
    """把模板原文拆成 `(字面段, 各段起点)`。

    `原告：＿＿，住址：＿＿。` → `(['原告：', '，住址：', '。'], [0, 3, 12])`。
    空（`＿＿`）是模型可以改的，字面段是**一个字都不该动**的。
    """
    lits: list[str] = []
    offs: list[int] = []
    pos = 0
    for m in _BLANK_RUN_RE.finditer(hint):
        lits.append(hint[pos:m.start()])
        offs.append(pos)
        pos = m.end()
    lits.append(hint[pos:])
    offs.append(pos)
    return lits, offs


def repair_truncated_paras(fills: dict[str, str],
                           fields: list[dict[str, Any]]) -> dict[str, str]:
    """整段回填被模型**截断**时，用模板原文把尾巴接回去。

    `kind="para"` 的契约是"字段值 = 该段的完整文字"（提示词第 6 条也写了
    "不许缩写、不许截断"），但模型对长段落会中途收笔 —— 实测起诉状的
    `原告：＿＿＿＿，…，联系电话：＿＿＿＿。` 一填到身份证号就停了，
    `住址`/`工作单位`/`职务`/`联系电话` 整截没了。

    那不是"少个句号"的格式问题：**立案时法院要的正是住址和电话**，
    一份当事人信息不全的起诉状递不进去。而这条尾巴由模板固定提供，
    根本不该由模型决定要不要写。

    ## 怎么区分"截断"和"模型自己改了措辞"

    这是本函数唯一需要判断的地方，而**误判的代价比截断更大** ——
    把模型正常填的段落当成截断，等于用模板原文覆盖掉它填进去的值，还是静默的。
    两个条件同时成立才算截断：

    1. 模板的字面段能按顺序在回填文字里依次找到（说明它确实是在照着模板写，
       只是中途停了），且**最后一段字面段没找到**；
    2. 从断开处往后**没有句号** —— 文字是戛然而止的半句，不是另写了一句。

    条件 2 是关键：模型改写措辞时会写出完整的句子（`原告：张伟，住所：…。`），
    有句号，于是不会被误判；而 `…公民身份号码：110101198803124512` 戛然而止，
    没有句号，才是截断。

    接回去的尾巴里剩下的 `＿＿` 会照常被渲染成"待填"，让人工补 ——
    既不留下半句看似完整的错文字，也不让模型去编。
    """
    out = dict(fills)
    for f in fields:
        if f.get("kind") != "para":
            continue
        fid = f.get("id") or ""
        hint = f.get("hint") or ""
        filled = (out.get(fid) or "").strip()
        # `hint` 为空、或模型原样回填（没改一个字）时都不动
        if not filled or not hint or len(filled) >= len(hint):
            continue
        # ⚠️ 删除哨兵**必须最先排除**。诉请条目的模板文字以 `＿＿` 开头
        #    （`＿＿. 判令被告向原告支付＿＿…元；`），切出来的第一个字面段是**空串**，
        #    而空串在 `"__DELETE__"` 里当然找得到 —— 于是"匹配"成立、判定为截断，
        #    把模板原文接到了哨兵后面拼成 `__DELETE__. 判令被告向原告支付＿＿…元；`。
        #    后果不只是难看：`filled_template_doc` / `render_markdown` 都是
        #    `val == DELETE_MARK` 整串判等，拼接后再也认不出来，**那一条诉请就删不掉了**，
        #    而且这串哨兵会原样印进递给法院的正文里。
        if filled == DELETE_MARK:
            continue

        lits, offs = _hint_skeleton(hint)
        scan = 0
        last = -1
        matched_any = False        # 有没有**非空**字面段匹配上
        for i, lit in enumerate(lits):
            if not lit:            # 相邻两个空之间的空字面段，不必找
                last = i
                continue
            j = filled.find(lit, scan)
            if j < 0:
                break
            scan = j + len(lit)
            last = i
            matched_any = True
        # 「一个字面段都没匹配上」= 开头就对不上，无从判断它写到哪了 —— 不动。
        # 只判 `last < 0` 是不够的：hint 以 `＿＿` 开头时 `last` 会被那个空串顶到 0。
        if not matched_any:
            continue
        if last >= len(lits) - 1:
            continue               # 模板的字面段一个不少 —— 不是截断
        if "。" in filled[scan:]:
            continue               # 戛然而止处之后有句号 —— 是改写，不是截断
        if last + 1 >= len(offs):
            continue

        logger.info("整段回填被截断（模板还有 %d 字没写），已补回尾巴：%s…",
                    len(hint) - offs[last + 1], filled[:24])
        out[fid] = filled + hint[offs[last + 1]:]
    return out


# 「当事人身份 + 落款」这类栏目 —— 它们开头的段落**永远不许整段删**，
# 见 `restore_protected_paras`。收录的是 31 份仲裁申请书与起诉状/答辩状里
# 出现过的全部这类栏目名。
_PROTECTED_LABEL_RE = re.compile(
    r"^(?:原告|被告|第三人|上诉人|被上诉人|再审申请人|"
    r"申请人|被申请人|答辩人|被答辩人|"
    r"委托诉讼代理人|委托代理人|法定代理人|法定代表人|负责人|具状人|日期)")
# `委托诉讼代理人：…` 是这批段落里**唯一**可以合法删掉的：当事人没请代理人时，
# 留着一行空白的「委托诉讼代理人：＿＿」递上去更糟。
# ⚠️ 删它之前得看材料说的是**谁**的代理人 —— 判据在 `_agent_is_ours`，
#    不是"材料里出现过『代理人』三个字"（那会让对方的律师把这一行钉住）。
_AGENT_LABEL_RE = re.compile(r"^(?:委托)?(?:诉讼)?代理人")


def _words_re(words: str) -> re.Pattern[str]:
    """把 ``"原告|申请人"`` 这样的词表编译成"不被前面的『被』吃掉"的正则。

    ⚠️ 少了这道护栏，`被申请人中国XX公司委托诉讼代理人赵磊` 里会先命中
       **申请人** 三个字，于是一句"我方的代理人"被判成对方的。
       `_SLOT_RULES` 里「被申请人必须排在申请人前面」是同一个坑，
       那边靠顺序，这边用定宽 lookbehind 根治。
    """
    pats = [w if w.startswith("被") else "(?<!被)" + w
            for w in (words or "").split("|") if w]
    return re.compile("|".join(pats) or "(?!)")


def _party_res(doc_type: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """``(我方称呼正则, 对方称呼正则)`` —— 称呼按**文书立场**取，见上面那张表。"""
    dt = doc_type if doc_type in _PARTY_SELF_BY_DOC else ""
    ours = "|".join(x for x in (_SELF_WORDS, _PARTY_SELF_BY_DOC.get(dt, "")) if x)
    # 立场认不出来时，没有谁是"对方" —— `_words_re("")` 编译成永不命中
    return _words_re(ours), _words_re(_PARTY_THEIRS_BY_DOC.get(dt, ""))


def _agent_mentions(text: str, doc_type: str = ""):
    """材料里每一处代理人提法 → ``(归谁, 后面 60 字)``；归谁＝ ``"ours"``/``"theirs"``。

    判法两段：提法**前面 16 个字里**的当事人称呼 > 文书立场。
    （`我公司委托诉讼代理人肖明` → ours ／ `原告张伟委托诉讼代理人王敏` → theirs ／
    `委托诉讼代理人王敏` 谁都没写 → 看 `doc_type`。）

    ⚠️ 这个判据是用户那份案文逼出来的（2026-09-28）：材料里只有**对方**的代理人
        （`原告张伟委托诉讼代理人王敏，北京泽衡律师事务所律师…`）。旧判据是
        "材料里出现过『代理人』三个字就保留这一行"，于是答辩状里那行留成了高亮
        空白；而照抄王敏进去更糟 —— 那成了"被告由原告的律师代理"，立案庭会挑出来。
    """
    text = text or ""
    ours_re, theirs_re = _party_res(doc_type)
    default = "theirs" if doc_type in _THEIRS_BY_DEFAULT else "ours"
    for m in _AGENT_IN_TEXT_RE.finditer(text):
        win = text[max(0, m.start() - _AGENT_SELF_WINDOW):m.start()]
        if theirs_re.search(win):
            owner = "theirs"
        elif ours_re.search(win):
            owner = "ours"
        else:
            owner = default
        yield owner, text[m.end():m.end() + _AGENT_SEG]


def _agent_is_ours(text: str, doc_type: str = "") -> bool:
    """材料里提到的委托代理人，有没有**我方**的。"""
    return any(o == "ours" for o, _ in _agent_mentions(text, doc_type))


def _law_firm_name(seg: str) -> str:
    """取律所的**名**（不含「律师事务所」四个字）。

    ⚠️ 模板里那格是 `＿＿＿＿律师事务所律师` —— 后缀是模板本来就有的，
       值再带上一个就成了"北京泽衡律师事务所**律师事务所**律师"（实测踩过）。
    """
    if not (m := _LAW_FIRM_RE.search(seg)):
        return ""
    name = m.group(0)[: -len("律师事务所")]
    # 材料把地址一起写进这一句时（`北京市朝阳区北京泽衡律师事务所`）剃掉地址前缀；
    # `北京市泽衡律师事务所` 不剃 —— 那个「北京市」是律所全称的一部分。
    # 靠长度区分：真律所名很少超过 8 个汉字，超了就说明前面粘了地址。
    if (len(name) > 8 and (mm := re.match(".*[省市区县]", name))
            and len(name) - mm.end() >= 2):
        name = name[mm.end():]
    return name


def _extract_agent(text: str, *, ours: bool = True, doc_type: str = "") -> dict[str, str]:
    """抽委托代理人的姓名／律所／电话。

    `ours=True`（默认）只认**我方**名下那一句；`ours=False` 反过来 —— 专取
    **对方**的代理人，用来识破"模型把对方的律师填进了我方那一行"。

    抽出来的键只喂给 `_fill_agent_para`，**绝不并进 `bag`**：`bag["姓名"]` 在别处
    是对方当事人的名字，混在一起就会把原告写成我方的委托代理人。
    """
    want = "ours" if ours else "theirs"
    bag: dict[str, str] = {}
    for owner, seg in _agent_mentions(text, doc_type):
        if owner != want:
            continue
        if (n := _AGENT_NAME_RE.match(seg)):
            bag["代理人"] = n.group(0)
        if (f := _law_firm_name(seg)):
            bag["律所"] = f
        # 电话单独抽一个宽松口径：材料里写的是 `139 1023 4567` 这种带空格的，
        # `_TEL_RE` 只认连着的 11 位，抽不到。这里也只认这一句里的号 ——
        # 不能退回 `bag["电话"]`（那是当事人自己的手机号）。
        if (p := _TEL_LOOSE_RE.search(seg)):
            bag["代理人电话"] = p.group(0)
        break
    return bag


def restore_protected_paras(fills: dict[str, str], fields: list[dict[str, Any]],
                            source_text: str = "", doc_type: str = "") -> dict[str, str]:
    """模型删掉**当事人身份段落**时，把它救回来。

    ## 这里防的是哪种失败

    提示词第 9 条本来只让模型删「材料里没主张的请求」和「没得可辩的论证段」。
    起诉状／答辩状段落化重建后实测：材料里既给了被答辩人姓名、也说了
    「委托诉讼代理人肖明」，模型仍把 `被答辩人：＿＿＿＿，＿＿，＿＿族，…` 和
    `委托诉讼代理人：…` **整行删了** —— 那两段里它能填上的只有一格，其余
    （民族／出生／身份证号／住址／律所）材料里没有，于是它把「这一格没材料」
    读成了「整段该删」（第 9 条那句"材料里没有的整段删掉"太宽了）。

    后果不是版式问题：**当事人栏缺一块的答辩状立案庭不收**，而这份文书在页面上
    看着完全正常 —— 那两行就是没了，没有任何报错。所以这条得由代码钉死。

    ## 救成什么样

    把**能确定的**填上（姓名／公司名／电话／落款日期），其余留成 `＿＿` 待人工补 ——
    跟 `repair_truncated_paras` 同一个口径：宁可留白让人补，也不猜、更不整段删。
    自动填只走 `rule_extract` 里**口径明确**的那几栏（标签在 `_SLOT_RULES` 里查得到，
    或落款日期行）。`法定代表人` 这一栏故意不填：`bag["姓名"]` 抽到的是**对方当事人**
    的名字，拿它填法定代表人等于把原告写成我方的代表人，比留一行白严重得多。
    `委托诉讼代理人` 那一行走的是另一条路 —— 材料里是**我方**代理人就由
    `_extract_agent` 单独抽着填上（见 `_agent_is_ours`），是对方的就放行删除。
    """
    out = dict(fills)
    for f in fields:
        if f.get("kind") != "para":
            continue
        fid = f.get("id") or ""
        if (out.get(fid) or "").strip() != DELETE_MARK:
            continue
        hint = f.get("hint") or ""
        if not _PROTECTED_LABEL_RE.match(hint):
            continue
        # `委托诉讼代理人：…` 单开一条路：材料里没有**我方**的代理人（压根没提，
        # 或只提了对方原告的律师）→ 这一行的删除是模型本来就该做的，放行；
        # 材料里给了我方代理人 → 从材料里抽出来填上，别留白。
        if _AGENT_LABEL_RE.match(hint):
            if not _agent_is_ours(source_text, doc_type):
                continue
            filled = _fill_agent_para(
                hint, _extract_agent(source_text, doc_type=doc_type))
            if filled != hint:
                logger.info("模型要删当事人的代理人行，已按材料填回我方代理人：%s…",
                            filled[:28])
                out[fid] = filled
            else:
                logger.info("模型要删当事人的代理人行，材料里抽不出细节，改为留白待人工补")
                out.pop(fid)
            continue

        label = re.split(r"[：:]", hint, maxsplit=1)[0].strip()
        known = (any(label.endswith(w) for w, _ in _SLOT_RULES)
                 or bool(_SIGN_DATE_RE.match(hint)))
        filled = (rule_extract(source_text or "", [f], doc_type).get(fid, "")
                  if known else "")
        if filled:
            logger.info("模型要删当事人段落（%s），已按材料填回可确定的部分：%s…",
                        label, filled[:24])
            out[fid] = filled
        else:
            logger.info("模型要删当事人段落（%s），改为留白待人工补", label)
            out.pop(fid)   # 去掉这个键 → 渲染时照模板原文出，空处标成「待填」
    return out


def backfill_address_slots(fills: dict[str, str], fields: list[dict[str, Any]],
                           source_text: str = "") -> dict[str, str]:
    """材料里明明给了地址、模型却空着的那一格，由规则补上。

    实测（2026-09-28）：同一份案文 —— `住址：北京市朝阳区常营北路9号院B区7号楼
    1203室（现住址）` —— 在 31 份仲裁申请书里连着三次都填对了住址，换成重建后的
    起诉状却把 `住址：＿＿` 原样留着（整段里别的格子都填了）。模型对长段落里
    某一格偶发漏填，而地址是「照抄就能对」的东西，不该交给它掷骰子。

    ⚠️ **只填空着的那一格**：模型已经写进去的地址一个字都不动。材料里常常有两个
    地址（`住址` 与 `通讯地址`/`户籍地址`），它可能挑了更合适的那个口径，
    覆盖回去等于用正则去猜。

    ⚠️ 走 `_fill_para` 这个既有的填空口，但 bag 里**只有地址** ——
    没有 `日期` 键，`_fill_para` 里那条「按事件词填日期」的分支就不会被带起来。
    """
    bag: dict[str, str] = {}
    for m in _ADDR_RE.finditer(source_text or ""):
        bag.setdefault(m.group(1), m.group(2).strip())
    if not bag:
        return fills

    out = dict(fills)
    for f in fields:
        if f.get("kind") != "para":
            continue
        fid = f.get("id") or ""
        cur = out.get(fid) or ""
        if not _BLANK_RE.search(cur):
            continue          # 这一格没留空（或整段没填），不必管
        new = _fill_para(cur, bag)
        if new != cur:
            logger.info("模型漏填了地址那格，已按材料补上：%s…", new[:32])
            out[fid] = new
    return out


def _fill_agent_para(cur: str, bag: dict[str, str]) -> str:
    """填 `委托诉讼代理人：＿＿＿＿，＿＿＿＿律师事务所律师，联系电话：＿＿＿＿。` 那三格。

    不借 `_fill_para`（它就在隔壁，看着能用，其实两处都对不上）：
    · 「联系电话」的口径是**材料里第一个电话** —— 这一段里第一个电话常常是当事人
      自己的（`138 1055 6789`），填进去就成了"我方代理人用原告的手机号"；
    · 「律所」那格更无从判断：`_fill_para` 只看空**前面**的字，而它前面紧挨着的
      正是还没填的姓名空（`＿＿＿＿律师事务所律师`），永远认不出来。
    """
    if not bag.get("代理人"):
        return cur
    out = re.sub("(委托诉讼代理人[：:])[ 　]*[＿_]{2,}",
                 lambda m: m.group(1) + bag["代理人"], cur, count=1)
    if bag.get("律所"):
        out = re.sub("([＿_]{2,})([ 　]*律师事务所)",
                     lambda m: bag["律所"] + m.group(2), out, count=1)
    if bag.get("代理人电话"):
        out = re.sub("(联系电话[：:])[ 　]*[＿_]{2,}",
                     lambda m: m.group(1) + bag["代理人电话"], out, count=1)
    return out


def resolve_agent_line(fills: dict[str, str], fields: list[dict[str, Any]],
                       source_text: str = "", doc_type: str = "") -> dict[str, str]:
    """`委托诉讼代理人：…` 那一行：材料里有**我方**代理人的就填上，没有就整行去掉。

    用户 2026-09-28 定的口径：**有就填、没有就删掉**。这一行是当事人栏里唯一
    可以合法不存在的（当事人没请代理人），而"填谁"必须先看归谁 —— 材料里常见
    的只有**对方**的代理人（`原告张伟委托诉讼代理人王敏…`），填进去就是
    "被告由原告的律师代理"。

    ⚠️ 模型经常是"这一行我不确定，就空着交"（用户那份草稿 `p3` 正是如此），
       所以这一步不只补填，也负责把**一个字都没填**的那一行按材料删掉。
       删的是整段（`__DELETE__`），预览与 Word 导出两侧都认这个哨兵。
    """
    bag = _extract_agent(source_text, doc_type=doc_type)
    ours = _agent_is_ours(source_text, doc_type)
    theirs = _extract_agent(source_text, ours=False, doc_type=doc_type) if not ours else {}
    out = dict(fills)
    for f in fields:
        if f.get("kind") != "para":
            continue
        fid = f.get("id") or ""
        hint = f.get("hint") or ""
        if not _AGENT_LABEL_RE.match(hint):
            continue
        cur = out.get(fid)
        if (cur or "").strip() == DELETE_MARK:
            continue          # 模型已经删了、`restore_protected_paras` 也放行过 → 尊重
        if bag:
            new = _fill_agent_para(cur or hint, bag)
            if new != (cur or hint):
                logger.info("材料里给了我方委托代理人，填上：%s…", new[:28])
                out[fid] = new
            continue
        if ours:
            continue          # 材料里确实有我方代理人，只是抽不出细节 → 留白人工补
        # 模型**自己**把对方的律师抄进了这一行（提示词第 9 条现在明说了，但它
        # 仍可能照抄）。这是"被告由原告的律师代理"，比空一行严重得多。
        copied = bool(theirs.get("代理人")) and theirs["代理人"] in (cur or "")
        if copied:
            logger.warning("模型把对方的代理人（%s）抄进了我方的代理人行，已去掉这一行",
                           theirs["代理人"])
            out[fid] = DELETE_MARK
            continue
        if not (cur or "").strip() or (cur or "").strip() == hint.strip():
            logger.info("材料里没有我方的委托代理人，去掉这一行")
            out[fid] = DELETE_MARK
    return out


def rule_extract(text: str, fields: list[dict[str, Any]],
                 doc_type: str = "") -> dict[str, str]:
    """没有模型时的兜底：正则抽常见要素，再按字段 key 的关键词匹配。

    两类字段的填法**不一样**，别合并：kind="value"（表单格）的值就是裸值；
    kind="para"（整段）的值必须是**该段的完整文字**，只能用 `_fill_para` 往空里填。

    能力有限但**不做假** —— 匹配不上就留空，让页面把"待填"标出来。
    """
    bag: dict[str, str] = {}
    if (m := _ID_RE.search(text)):
        bag["身份证"] = m.group(0)
    # 身份证号 `110101199003074512` 完全符合 18 位 USCC 字符集，
    # `search` 取第一个就取到它 —— 必须整串排除掉身份证形状的。
    uscc = next((m.group(0) for m in _USCC_RE.finditer(text)
                 if not _ID_RE.fullmatch(m.group(0))), "")
    if uscc:
        bag["信用代码"] = uscc
    if (m := _TEL_RE.search(text)):
        bag["电话"] = m.group(0)
    if (m := _DATE_RE.search(text)):
        bag["日期"] = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    amounts = _AMOUNT_RE.findall(text)
    if amounts:
        bag["金额"] = "；".join(f"{a}{u}" for a, u in amounts[:6])
    # 用 finditer 而不是 search：第一个命中的常常是「我们公司」这种指代，
    # 排除掉要**接着往后找**真名，不能就此收工（否则整条只落得一个空）。
    for m in _COMPANY_RE.finditer(text):
        raw = m.group(0)
        # 「我们公司」「该公司」削掉指代就只剩「公司」两个字 —— 那是口语指代，
        # 不是被申请人名称。跳过，接着往后找真名。
        if _COMPANY_PRONOUN_RE.sub("", raw) == "公司":
            continue
        name = _COMPANY_HEAD_RE.sub("", raw)
        if name:
            bag["公司"] = name
            break
    # 用 finditer 而不是 search：第一个命中的"名字"可能就是栏目名本身
    # （材料写成 `答辩人姓名：张伟` 时，`答辩人` 分支会先吃掉后面两个字），
    # 跳过它接着往后找真名。抓不到就留空 —— 宁可让人工补，也不填一个"姓名"。
    for m in _NAME_RE.finditer(text):
        if m.group(1) not in _NAME_STOPWORDS:
            bag["姓名"] = m.group(1)
            break
    # 地址照抄，一格一个键。`setdefault`：同一种标签出现两次时取**先出现的那个**
    # （材料里先说当事人的住址，再补一句现住址的情况很常见，取先说的更稳）。
    for m in _ADDR_RE.finditer(text):
        bag.setdefault(m.group(1), m.group(2).strip())
    # 我方委托代理人（姓名／律所／电话）。**单独一口**，不并进上面那个 bag ——
    # `bag["姓名"]` 是对方当事人的名字，`bag["电话"]` 是材料里第一个电话
    # （常常是当事人的手机号）。见 `_fill_agent_para`。
    agent = _extract_agent(text, doc_type=doc_type)

    out: dict[str, str] = {}
    item_no = 0                # 存活的「请求事项」条数 —— 删掉的不占号
    for f in fields:
        if f.get("kind") == "para":
            # `hint` 存的就是模板里那一段的原文（见 `_extract_fields`）。
            # ⚠️ `bag["金额"]` 在这里故意不用：正文里有好几个金额空，
            #    正则分不清哪个空对应哪笔钱，硬填就是瞎猜 —— 宁可留白。
            hint = f.get("hint") or ""
            filled = _fill_para(hint, bag)
            if agent:
                filled = _fill_agent_para(filled, agent)
            if _ITEM_NO_RE.match(filled):
                item_no += 1
                filled = _ITEM_NO_RE.sub(f"{item_no}.", filled, count=1)
            if _SIGN_DATE_RE.match(filled):
                filled = f"日期：{_today_cn()}"     # 落款日期：规则路径下直接填今天
            if filled != hint:
                out[f["id"]] = filled
            continue          # 一个空都没填上就整个跳过，不占「已填」名额
        key = (f.get("key") or "") + (f.get("label") or "")
        for kw, val in bag.items():
            if kw in key:
                out[f["id"]] = val
                break
    return out


def stats() -> dict[str, Any]:
    items = list_templates()
    by_type: dict[str, int] = {}
    for x in items:
        by_type[x["doc_type"]] = by_type.get(x["doc_type"], 0) + 1
    return {
        # **只回目录名，不回绝对路径。** 这个键会经 ``GET /doc/templates`` 直接
        # 发给前端（空状态提示要用：「请把 .docx 模板放进 文书模板 目录」），
        # 而 ``str(template_dir())`` 是 ``C:\\Users\\<部署者>\\Desktop\\文书模板`` ——
        # 服务器用户名与目录结构就这么泄给了一个**免鉴权**的接口。
        # 键名保留 ``dir`` 是为了不动前端；要改的是值。
        "dir": template_dir().name,
        "count": len(items),
        "by_type": by_type,
        "by_type_label": {DOC_TYPE_LABEL.get(k, k): v for k, v in by_type.items()},
    }
