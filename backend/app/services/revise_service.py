"""合同修订版生成：把风险点**落回合同正文**，产出律师可直接编辑/交付的修订稿。

设计取舍（为什么是确定性的、不再调一次模型）：

1. **不新增大模型调用。** 逐条风险与修改建议在 ReviewNode 已经产出（``risk_points``
   里带 ``suggestion`` 与 ``basis``），这里只做"定位 + 就地插入"这种确定性工作。
   再让模型把建议改写成条文会多花 30~60s，而它并不比律师自己落笔更可靠。
2. **原文一字不改地留着。** 修订建议就地插在对应条款之后，原文用删除线标出，
   建议用插入样式标出 —— 律师要能一眼看出"改前是什么、改后是什么"，
   而不是拿到一份无法核对的新合同。
3. **定位失败必须如实报。** OCR 出来的合同常有错字与断行，条款定位不上的情况一定会有。
   这类条款宁可标成"未能定位，需人工处理"，也不能硬塞到别处 —— 修订稿改了错地方
   比没改更危险。

输出三份东西，分别给不同用途：
- ``revised_markdown``：带标记（删除线/插入）的修订稿，导出 Word/PDF 用；
- ``clean_text``：纯文本修订稿，律师复制到自己的文档里继续改；
- ``changes``：逐条变更清单（条款/等级/建议/依据/是否定位成功），给界面渲染。
"""

from __future__ import annotations

import re
from typing import Any

LEVEL_CN = {"high": "高", "medium": "中", "low": "低"}
LEVEL_ORDER = {"high": 0, "medium": 1, "low": 2}

# 插入样式：`++…++` 在导出 Word/PDF 时渲染成蓝色下划线（见 docs.py 的行内解析）
INS_OPEN, INS_CLOSE = "++", "++"
DEL_OPEN, DEL_CLOSE = "~~", "~~"

_WS = re.compile(r"[\s\u3000]+")

_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_num(text: str) -> int | None:
    """把「十五」「二十」「三十一」这类中文数字转成整数（限 100 以内，条款号够用）。"""
    t = text.strip()
    if not t:
        return None
    if t.isdigit():
        return int(t)
    if "十" not in t:
        return _CN_DIGIT.get(t) if len(t) == 1 else None
    head, _, tail = t.partition("十")
    tens = _CN_DIGIT.get(head, 1) if head else 1
    ones = _CN_DIGIT.get(tail, 0) if tail else 0
    if (head and head not in _CN_DIGIT) or (tail and tail not in _CN_DIGIT):
        return None
    return tens * 10 + ones


def _clause_key(clause_no: str) -> str:
    """条款号的规范化键：``第3条`` / ``第 三 条`` / ``第三条`` 都归一到 ``3``。

    为什么必须做：模型输出的条款号是中文数字（「第二十三条」），
    而解析层存的可能是阿拉伯数字（「第23条」）。不归一的话，定位会全量失败 ——
    表现为"修订稿里一处标注都没有"，而律师看不出来是匹配规则的问题。
    """
    m = re.search(r"第\s*([0-9一二三四五六七八九十百零两]+)\s*条", clause_no or "")
    if not m:
        return ""
    n = _cn_num(m.group(1))
    return str(n) if n is not None else ""


def _norm_index(text: str) -> tuple[str, list[int]]:
    """去掉所有空白后返回 (压缩文本, 位置映射)。

    为什么要这么做：扫描件 OCR 出来的合同常是「劳动合 同 书」「第 八 条」，
    直接 ``str.find`` 一定找不到条款原文。按"去空白"后的坐标匹配，
    再用映射表还原回原文位置，才能既匹配得上一字不改地插回去。
    """
    buf: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text):
        if _WS.match(ch):
            continue
        buf.append(ch)
        idx.append(i)
    return "".join(buf), idx


def _span_in_original(needle: str, norm: str, idx: list[int]) -> tuple[int, int] | None:
    n = _WS.sub("", needle or "")
    if len(n) < 4:                       # 太短的片段（如"第 8 条"）容易误匹配，宁可不定位
        return None
    pos = norm.find(n)
    if pos < 0:
        # 退一步：用前 40 字做前缀匹配（OCR 后半段常有噪声）
        head = n[:40]
        if len(head) >= 8:
            pos = norm.find(head)
            if pos >= 0:
                # **末尾下标是 pos+len-1**：写成 pos+len 会把原文的"下一个字"也圈进来
                # （实测表现为删除线里多出一个"第"，看着像把下一条也改了）
                return idx[pos], idx[min(pos + len(head) - 1, len(idx) - 1)] + 1
        return None
    return idx[pos], idx[min(pos + len(n) - 1, len(idx) - 1)] + 1


def _one_line(text: str) -> str:
    """压成一行：修订标记是按"行"解析的，标记里带换行会让导出层认不出它。"""
    return _WS.sub(" ", (text or "").replace("\n", " ").replace("\r", " ")).strip()


# 下一条款/下一节的起头（「二、」「第十五条」「第十六章」…），以及**落款签名区**。
# 落款那几条是 2026-09-29 补的：合同末条后面紧跟的就是落款（「甲方：…」「盖章：」
# 「2024 年 2 月 28 日」），而 `split_articles` 切末条时是"切到文件尾"——
# 于是改写区间一路跨进签名区，交付出去的合同**没有落款**（少一个签署页＝废纸）。
# 解析层 `structure._trim_clause_tail` 已经把条款正文修剪干净，正常情况下这里不会再命中；
# 留着是**第二道防线**：万一哪天定位又跑偏，宁可少改一行，也不能把签名区换掉。
_SIGN_ALT = (
    r"(?:甲方|乙方|丙方|用人单位|劳动者|出租人|承租人|买受人|出卖人|盖章|签字|"
    r"法定代表人|委托代理人|委托代表人)\s*[：:]"
    r"|\d{4}\s*年[^\n]{0,12}月[^\n]{0,12}日[ \t　]*$"
)
_CLAUSE_HEAD = re.compile(
    r"\n[ \t　]*(?:"
    r"[一二三四五六七八九十]{1,3}\s*[、.]"
    r"|第\s*[0-9一二三四五六七八九十百零两]{1,4}\s*[条章节]"
    r"|" + _SIGN_ALT +
    r")"
)


def _tighten(segment: str) -> str:
    """把删除区间收紧到"这一条"为止。

    真机上遇到的：IntakeNode 拆出的 clause_text 有时会连带上下一节的标题
    （扫描件里没有规整的条款边界），照原样标删除线就会把"二、工作内容和工作地点"
    一起划掉 —— 看着像把下一条也改了。这里只在**后面还有内容**时，
    从下一个条款/小节标题处截断；截不到就原样返回，绝不猜。

    `_SIGN_HEAD` 补上之后，落款区也成了一道收口（见那里的注释）。
    """
    m = _CLAUSE_HEAD.search(segment)
    if m and m.start() > 0:
        return segment[:m.start()].rstrip()
    return segment


def _clause_text_of(clauses: list[dict[str, Any]], clause_no: str) -> str:
    """按条款号从解析结果里取原文（比用模型回抄的片段可靠）。

    条款号先归一再比 —— 模型写「第三条」、解析层存「第3条」是常态。
    """
    key = _clause_key(clause_no)
    if not key:
        return ""
    for c in clauses or []:
        if _clause_key(str(c.get("clause_no") or "")) == key:
            return str(c.get("clause_text") or "")
    return ""


# 条款/小节的起头（「第十五条」「二、」「第三章」），用于在下一条处收口
_NEXT_CLAUSE = re.compile(r"第\s*[0-9一二三四五六七八九十百零两]{1,4}\s*[条章节]"
                          r"|[一二三四五六七八九十]{1,3}\s*、")

# 条款号（用于把「第二条、第八条」这种复合条款号拆开）
_CLAUSE_NO_RE = re.compile(r"第\s*[0-9一二三四五六七八九十百零两]{1,4}\s*条")


def split_clause_nos(clause_no: str) -> list[str]:
    """「第二条、第八条」→ ['第二条', '第八条']。

    为什么必须拆：模型经常把一处风险挂在多条条款号上（同一条违法表述在合同里出现两遍）。
    只改第一条的话，另一条的违法内容会**原样留在改后合同里** —— 交付物看起来"已修订"，
    实际还踩在同一个坑上，这是最不能接受的一种漏改。
    """
    found = _CLAUSE_NO_RE.findall(clause_no or "")
    out: list[str] = []
    seen: set[str] = set()
    for f in found:
        key = _clause_key(f)
        if key and key not in seen:
            seen.add(key)
            out.append(f)
    return out or [clause_no or ""]


def _anchor_by_marker(contract_text: str, norm: str, idx: list[int],
                      clause_no: str) -> tuple[int, int] | None:
    """兜底定位：找原文里的「第十条」这种条款号，锚到**下一条款之前**。

    什么时候用得上：解析层没拆出条款文本（扫描件纯文本路径失败），或模型给的条款号
    与合同里的写法不一致。这时的锚点不如"条款原文精确匹配"可靠，但它至少把修订
    放在正确的位置附近，而不是整份修订稿一处标注都没有。调用方会把这类锚点标出来。

    ⚠ 结束点必须收到"下一个条款号"为止。早先用固定 400 字兜底，真机上一份 10 条风险的
    合同里有 6 处因为区间互相压叠被判成"已合并"，等于白标。
    """
    for token in (clause_no, _arabic_form(clause_no)):
        if not token:
            continue
        n = _WS.sub("", token)
        pos = norm.find(n)
        if pos < 0:
            continue
        start = idx[pos]
        nxt = _NEXT_CLAUSE.search(norm, pos + len(n))
        end = idx[nxt.start()] if nxt else -1
        if end <= start:
            # 后面没有下一条款号可依靠（末条，或扫描件拆行混乱）：退到行尾
            end = contract_text.find("\n", start)
            if end < 0 or end - start < 8:
                end = min(len(contract_text), start + 400)
        return start, end
    return None


def _arabic_form(clause_no: str) -> str:
    """「第三条」→「第3条」，用于在原文里找阿拉伯数字写法的条款号。"""
    key = _clause_key(clause_no)
    if not key:
        return ""
    m = re.search(r"第\s*[0-9一二三四五六七八九十百零两]+\s*条", clause_no or "")
    return f"第{key}条" if m else ""


def _basis_text(basis: Any) -> str:
    if isinstance(basis, list):
        items = [str(b).strip() for b in basis if str(b).strip()]
        return "；".join(items)
    return str(basis or "").strip()


# 合同类型枚举 → 中文。state 里的 contract_type 常是 "contract" 这种原始值，
# 直接拿去当标题会得到「contract·修订版」这种半截中英混排的文件名。
_TYPE_CN = {
    "contract": "合同", "labor": "劳动合同", "employment": "劳动合同",
    "lease": "租赁合同", "sales": "买卖合同", "loan": "借款合同",
    "service": "服务合同", "construction": "建设工程合同",
    "consult": "合同", "unknown": "合同",
}
_TYPE_HEAD = re.compile(r"^(?:第[0-9一二三四五六七八九十]+[条章节]"
                        r"|[一二三四五六七八九十]+[、.]"
                        r"|[（(][一二三四五六七八九十]+[)）])")

# 材料解析留下的「【1.1.jpg】」「【合同（演示样例）.pdf】」（与 :func:`_strip_file_markers` 同源）
_FILE_MARKER_RE = re.compile(
    r"\s*[【\[][^】\]]{1,40}[.．](?:jpg|jpeg|png|bmp|tiff|webp|pdf|docx?|txt|md)[】\]]\s*",
    flags=re.I)

# 交付版式用的分类（消费方是 :func:`_contract_markdown`，常量放顶上是因为
# :func:`_guess_title` 也要用 `_DOC_RE_PAGE` 把页标行从抬头候选里剔掉）。
# **比前端 ``ctDocBlocks`` 窄一层**：那份还要管水印、图片占位符、以及"把页标与页脚
# 原样收回去"（左栏是 contenteditable，丢一个字字数就对不上）；这份只面对
# **已经拆条清洗过的交付正文**。两边目标不同，别拿一份去替另一份。
_DOC_RE_PAGE = re.compile(r"^\s*(?:<!--\s*第\s*\d+\s*页\s*-->|\d{1,3})\s*$")
_DOC_RE_CHAPTER = re.compile(r"^[一二三四五六七八九十]{1,3}\s*[、.]")
_DOC_RE_SIGN = re.compile(
    r"^(甲方|乙方|丙方|用人单位|劳动者|出租人|承租人|买受人|出卖人|地址|住所|"
    r"联系电话|电话|盖章|签名|日期)[\s　]*[：:]")
_DOC_RE_DATE = re.compile(
    r"^[0-9〇零一二三四五六七八九十]{2,4}\s*年\s*[0-9〇零一二三四五六七八九十]{1,3}\s*月"
    r"\s*[0-9〇零一二三四五六七八九十]{1,3}\s*日。?$")
# 一句话说完了没有 —— 决定下一行是"折行"还是"新段落"（与前端 CT_RE_END 同源）
_DOC_RE_END = re.compile(r"[。；：！？”』」）)\]]$")
# :func:`to_plain_text` 用的"脱标记"（`# `/`### ` 行首、`<p align="…">…</p>` 整行包裹）
_DOC_MARK_STRIP = re.compile(r"^\s*#{1,4}\s+")
_DOC_P_TAG = re.compile(r'<p\s+align="(?:left|right|center)">([\s\S]*?)</p>')


def _guess_title(contract_text: str, contract_type: str) -> str:
    """给修订稿起个像样的名字：优先用合同正文自己的抬头（「劳动合同书」），
    拿不到再退回类型中文名。文件名是律师第一眼看到的东西，不该是半截英文枚举。

    2026-09-29：**跳过文件标记行**。材料的第 1 行恰好是「【合同（演示样例）.pdf】」，
    它长度合法、没有数字、也不含下面那些排除词，于是被当成了抬头 ——
    导出的文件名就成了「【合同（演示样例）.pdf】（改后版）.pdf」，用户直接看到。
    """
    for line in (contract_text or "").splitlines()[:6]:
        if _FILE_MARKER_RE.fullmatch(line or ""):
            continue
        t = _WS.sub("", line).strip()
        if not (2 <= len(t) <= 30):
            continue
        if _DOC_RE_PAGE.match(t):                  # 「<!-- 第 1 页 -->」「1」也不是抬头
            continue
        if _TYPE_HEAD.match(t):                    # 「第一条 …」「一、…」不是抬头
            continue
        if sum(ch.isdigit() for ch in t) > 1:      # 日期行、编号行排除
            continue
        if any(k in t for k in ("甲方", "乙方", "合同编号", "签订", "根据")):
            continue
        return t
    return _TYPE_CN.get((contract_type or "").strip().lower(), contract_type or "合同")


def locate_changes(
    contract_text: str,
    risk_points: list[dict[str, Any]],
    *,
    clauses: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """把风险点定位到原文位置，产出变更清单（annotate 与 rewrite 两条路共用）。

    共用是刻意的：定位是**同一件容易出错的事**（OCR 空格、条款号中英混写、区间压叠），
    两条路各写一份必然分叉 —— 到时候"标注稿标了、改后合同没改"这种不一致最难查。
    """
    text = contract_text or ""
    clauses = clauses or []
    norm, idx = _norm_index(text)

    changes: list[dict[str, Any]] = []
    seq = 0
    for rp in risk_points or []:
        if not isinstance(rp, dict):
            continue
        raw_no = str(rp.get("clause_no") or "").strip()
        # 复合条款号（「第二条、第八条」）拆成多条，各自定位、各自改写 —— 见 split_clause_nos
        nos = split_clause_nos(raw_no)
        for clause_no in nos:
            seq += 1
            # 定位优先用 IntakeNode 拆出的条款原文；模型回抄的片段（clause_text）次之。
            # ⚠ 复合条款号时**不能**用 rp 里那段片段：它只对应其中一条，套到另一条头上会改错地方。
            source = _clause_text_of(clauses, clause_no) or (
                str(rp.get("clause_text") or "") if len(nos) == 1 else "")
            anchor = "clause_text"
            span = _span_in_original(source, norm, idx) if source else None
            if span is None:
                # 条款原文匹配不上时退到条款号锚点（可靠性低一档，界面会标出来）
                span = _anchor_by_marker(text, norm, idx, clause_no)
                if span:
                    anchor = "clause_no"
            tightened = False
            if span:
                # 收口：扫描件拆出的 clause_text 常连带上下一节的标题
                # （「…。二、工作内容和工作地点」）。不收口的话，改写会把下一条的标题
                # 一起圈进来，替换后正文里就会出现两条编号叠在一起。
                seg = text[span[0]:span[1]]
                tight = _tighten(seg)
                if tight and tight != seg:
                    span = (span[0], span[0] + len(tight))
                    tightened = True
            changes.append({
                "seq": seq,
                "clause_no": clause_no,
                "clause_group": raw_no if len(nos) > 1 else "",
                "clause_title": str(rp.get("clause_title") or "").strip(),
                "level": rp.get("level") if rp.get("level") in LEVEL_CN else "medium",
                "risk": str(rp.get("risk") or "").strip(),
                "suggestion": str(rp.get("suggestion") or "").strip(),
                "basis": _basis_text(rp.get("basis")),
                "anchor": anchor if span else "",
                "tightened": tightened,
                "start": span[0] if span else -1,
                "end": span[1] if span else -1,
                "located": bool(span),
                # 原文片段：改写要用它当"改前"，界面也要展示它当对照
                "original": _one_line(text[span[0]:span[1]]) if span else _one_line(source),
            })

    # 同一位置被多条风险命中时只保留第一条（重复标注会让修订稿难以阅读）。
    # ⚠ 被合并掉的**不算"未定位"**：它们定位到了，只是并进了上一条。
    # 早先版本把这两件事混成一个 located=False，结果真机上一份 10 条风险的合同
    # 报"6 处未能定位"，律师一核对发现其中 3 处明明标出来了 —— 数字自相矛盾，
    # 整份清单的可信度就没了。
    located = [c for c in changes if c["located"]]
    located.sort(key=lambda c: (c["start"], c["seq"]))
    picked: list[dict[str, Any]] = []
    merged_count = 0
    last_end = -1
    for c in located:
        if c["start"] < last_end:
            c["merged"] = True
            c["skip_reason"] = "与上一条修订位置重叠，已合并到上一处"
            merged_count += 1
            continue
        picked.append(c)
        last_end = c["end"]

    stats = {
        "total": len(changes),
        "applied": len(picked),
        "merged": merged_count,
        "unlocated": sum(1 for c in changes if not c["located"] and not c.get("merged")),
        "high": sum(1 for c in changes if c["level"] == "high"),
        "medium": sum(1 for c in changes if c["level"] == "medium"),
        "low": sum(1 for c in changes if c["level"] == "low"),
    }
    stats["high_applied"] = sum(1 for c in picked if c["level"] == "high")
    stats["anchored_by_no"] = sum(1 for c in picked if c.get("anchor") == "clause_no")
    return {"changes": changes, "picked": picked, "stats": stats, "text": text}


def build_revision(
    contract_text: str,
    risk_points: list[dict[str, Any]],
    *,
    clauses: list[dict[str, Any]] | None = None,
    contract_type: str = "",
    title: str = "",
) -> dict[str, Any]:
    """把风险点落到原文位置上，返回**带修订标记的对照稿**（原文删除线 + 建议插入）。

    给律师核对用，不是最终交付物 —— 要"改完的合同本身"走 :func:`build_rewritten`。
    """
    loc = locate_changes(contract_text, risk_points, clauses=clauses)
    text, picked, changes, stats = loc["text"], loc["picked"], loc["changes"], loc["stats"]

    head = title or (f"{_guess_title(text, contract_type)}·修订版")
    out: list[str] = [f"# {head}", "",
                      f"> 本修订版由 AI 依据逐条审查结果就地标注：原文以删除线标出，"
                      f"修订建议以插入样式紧随其后。共 {stats['total']} 处风险，"
                      f"已就地标注 {stats['applied']} 处"
                      + (f"，{stats['merged']} 处与相邻修订重叠已合并"
                         if stats["merged"] else "")
                      + (f"，另有 {stats['unlocated']} 处未能定位（见文末清单，需人工处理）"
                         if stats["unlocated"] else "") + "。", ""]

    cursor = 0
    for c in picked:
        out.append(text[cursor:c["start"]].strip("\n"))
        # 原文：保留 + 删除线（不真删：律师要能核对）。
        # 删除线里的换行必须压掉 —— 导出层是按行认标记的，标记跨行就认不出来，
        # Word 里会直接显示 "~~…" 这种原文。
        out.append("")
        out.append(DEL_OPEN + _one_line(_tighten(text[c["start"]:c["end"]])) + DEL_CLOSE)
        out.append("")
        out.append(f"{INS_OPEN}【修订 · 等级：{LEVEL_CN[c['level']]}】"
                   f"{_one_line(c['suggestion']) or '（模型未给出具体改法，需人工拟订）'}{INS_CLOSE}")
        if c["basis"]:
            out.append(f"> 依据：{c['basis']}")
        out.append("")
        cursor = c["end"]
    out.append(text[cursor:].strip("\n"))

    # 文末：未能定位的条款 + 分级统计。**必须列出**，否则律师以为全文都改到了。
    tail: list[str] = ["", "---", "", "## 修订说明", ""]
    tail.append(f"- **风险合计**：{stats['total']} 处（高 {stats['high']} / 中 {stats['medium']} / 低 {stats['low']}）")
    tail.append(f"- **已就地标注**：{stats['applied']} 处（其中按条款号锚定 {stats['anchored_by_no']} 处，位置需人工核对）")
    if stats["merged"]:
        tail.append(f"- **与相邻修订重叠、已合并**：{stats['merged']} 处（同一处修改被多条风险命中）")
    tail.append(f"- **未能定位**：{stats['unlocated']} 处")
    if stats["unlocated"]:
        tail.append("")
        tail.append("以下条款未能在正文中可靠定位（原文可能是扫描件、条款号有误或文字被拆散），"
                    "**需人工打开原件处理**：")
        for c in changes:
            if not c["located"] and not c.get("merged"):
                tail.append(f"- {c['clause_no'] or '（未标条款号）'} {c['clause_title']}"
                            f"｜等级：{LEVEL_CN[c['level']]}｜风险：{c['risk'][:120]}")
    out.extend(tail)
    revised_markdown = "\n".join(out)

    # 界面用：逐条清单（含定位情况），按严重度排序
    changes.sort(key=lambda c: (LEVEL_ORDER.get(c["level"], 1), c["seq"]))
    return {
        "title": head,
        "revised_markdown": revised_markdown,
        "clean_text": _strip_marks(revised_markdown),
        "changes": changes,
        "stats": stats,
    }


def _strip_marks(md: str) -> str:
    """去掉修订标记，得到可直接复制的纯文本。"""
    t = md.replace(DEL_OPEN, "").replace(DEL_CLOSE, "")
    t = t.replace(INS_OPEN, "").replace(INS_CLOSE, "")
    t = re.sub(r"^#+\s*", "", t, flags=re.M)
    t = re.sub(r"^>\s?", "", t, flags=re.M)
    return t.strip() + "\n"


# --------------------------------------------------------------------------- 按风险重写
# 与"就地标注"的区别：标注稿给律师看"哪里要改、怎么改"，这里要的是**改完的合同本身** ——
# 条款正文必须是能直接放进合同的条文，不能残留"建议""应当修改"这类元话语。
REWRITE_SYSTEM = """你是执业律师，负责把合同条款按已确认的风险改写成可直接使用的正式条文。

硬要求：
1. **只输出条文正文**。不要出现"建议""应当修改""风险提示""依据"等元话语，不要加小标题、
   不要写说明段落；写成一个能直接放进合同的自然段。
2. **保留原条款的编号与格式**：原文以「第十七条」开头就照写，「一、」「1.」这类编号也照写；
   原文里的下划线填空位（如 ____）保留原样，不要替换成具体数字。
   **不要自己加「条款号 + 标题」这种行首标签** —— 编号在原文里已经有，重复写会变成
   「第八条 第八条 …」或者把标题塞进正文。
3. **不得超出给定依据**：修改方向必须与给定的「风险」「修改建议」「法律依据」一致；
   依据里没出现的法条编号一律不要写进条文。需要填金额/期限/比例时，优先沿用原文已有数值，
   或采用法定上限（如竞业限制不超过二年、试用期最长六个月），**不要臆造商业条款**。
4. **只改必要之处**：合法内容原样保留，称谓（甲方/乙方）与体例与原文一致。

如果该条款的正确处理是**整条删除**（约定了法律不允许约定的内容），
把 revised_text 留空、deleted 置 true，并在 note 里写"删除该条款"。"""

REWRITE_USER = """合同类型：{ctype}
条款号：{no}（标题仅供参考定位，不要写进正文）

【原条款正文】
{original}

【已识别的风险】
{risk}

【修改建议】
{suggestion}

【可用法律依据（只能引用这些，不得新增编号）】
{basis}

【本次同时修订的其它条款（它们的问题由它们自己改）】
{siblings}

⚠ 两条纪律要同时满足，别为了其中一条牺牲另一条：
(1) **属于本条范围的要求必须写进条文**（尤其「修改建议」里带百分比、年限、期限、
    "经济补偿"这类硬要求的，不能因为怕重复就不写）；
(2) 属于别的条款范围的内容不要搬过来，可在 note 里注明"该要求属第 X 条"。
两边都不写，等于把这条风险漏掉了 —— 那比重复严重得多。

请以 JSON 输出，格式严格如下（不要加 markdown 围栏以外的任何解释文字）：
{{"revised_text": "改写后的条款正文；若整条删除则留空", "note": "一句话说明这条改了什么", "deleted": false}}

（注意：这里必须是合法 JSON —— 网关在 response_format=json_object 时要求提示里出现 json 字样，
缺了会直接 400。）"""


async def rewrite_clauses(picked: list[dict[str, Any]], *, contract_type: str = "",
                          concurrency: int = 4) -> dict[int, dict[str, Any]]:
    """逐条把风险条款改写成正式条文（并发）。

    为什么并发：10 条风险串行改写要 1~2 分钟，而条款之间互不依赖，
    并发 4 路后墙钟≈最慢的那一条。每条的 prompt 都很小（只有一条条款），
    比把 10 条塞进一次调用更快，也更容易约束"只改这一条"。
    """
    import asyncio

    from backend.llm.client import llm_client

    sem = asyncio.Semaphore(max(1, concurrency))
    total = len(picked)
    # 姐妹条款清单：告诉每一条"别的条款正在改什么"，防止同一个问题被两条各写一遍
    # （真机上出现过：试用期规则同时写进第一条和第二条，合同里出现重复条款）
    sibling_all = [
        f"{c.get('clause_no') or '?'}（{(c.get('clause_title') or '无标题').strip()}："
        f"{(c.get('risk') or '')[:40]}）"
        for c in picked
    ]

    async def one(idx: int, c: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        async with sem:
            siblings = "\n".join(s for i, s in enumerate(sibling_all) if picked[i]["seq"] != c["seq"])
            user = REWRITE_USER.format(
                ctype=contract_type or "合同",
                no=c.get("clause_no") or f"第{idx + 1}条",
                title=c.get("clause_title") or "",
                original=c.get("original") or "",
                risk=c.get("risk") or "（未给出具体风险描述）",
                suggestion=c.get("suggestion") or "（未给出具体改法，按法律规定补齐必要内容）",
                basis=c.get("basis") or "（本次检索未命中具体条文，改写时不要出现法条编号）",
                siblings=siblings or "（本次只改这一条）",
            )
            data, res = await llm_client.chat_json(
                [{"role": "system", "content": REWRITE_SYSTEM},
                 {"role": "user", "content": user}],
                task="revise_clause",
                payload={"clause_no": c.get("clause_no") or "", "original": c.get("original") or "",
                         "risk": c.get("risk") or "", "suggestion": c.get("suggestion") or ""},
                default={"revised_text": "", "note": "", "deleted": False},
            )
            text = str(data.get("revised_text") or "").strip()
            deleted = bool(data.get("deleted")) and not text
            return c["seq"], {
                "seq": c["seq"],
                "clause_no": c.get("clause_no") or "",
                "revised_text": text,
                "deleted": deleted,
                "note": str(data.get("note") or "").strip(),
                # 没拿到正文且不是"删除"= 这条没改成，调用方必须保持原文不动
                "ok": bool(text) or deleted,
                "degraded": bool(getattr(res, "degraded", False)),
            }

    pairs = await asyncio.gather(*(one(i, c) for i, c in enumerate(picked)))
    return dict(pairs)


# --------------------------------------------------------------------------- 规则改写兜底
# 模型不可用（额度耗尽/网关故障）时，rewrite 模式不能直接 502 把用户晾在原地 ——
# 那样"生成改后合同"这个功能在降级期间等于不存在。兜底思路与对照稿同源：
# 审查阶段每条风险都带「修改建议」，对照稿里插入的文本就是它；
# 这里把同一份建议**条款化**（去掉元话语、补条款号、收句号），作为确定性的改写结果。
_DELETE_PAT = re.compile(r"整条删除|删除该条|删去该条|不得约定|认定为?无效|无效条款")
_META_PREFIX = re.compile(r"^(?:建议|应当|需要?|必须?|须|宜|最好|同时|另外|此外|否则)[:：、,，\s]*")
_MODIFY_LEAD = re.compile(r"^(?:修改为|改为|改写为|替换为)[:：\s]*")
_ADD_PAT = re.compile(r"缺少|缺失|未约定|没有约定|补充|增加|添加|增设")


def rule_rewrite_one(c: dict[str, Any]) -> dict[str, Any]:
    """把一条风险的「修改建议」条款化成正式条文（不调模型，确定性输出）。

    三种处理：
    1. 建议/风险指向**整条删除**（约定了法律不允许约定的内容）→ deleted=True；
    2. 风险是**缺少约定**类（没写补偿、没写期限）→ 原条款保留 + 建议条款化后追加；
    3. 其余（内容违法/显失公平）→ 用条款化后的建议**替换**原条款。
    输出格式与 ``rewrite_clauses`` 的单条结果对齐，调用方可以直接塞进 rewrites。
    """
    sug = _one_line(c.get("suggestion") or "")
    orig = _one_line(c.get("original") or "")
    risk = _one_line(c.get("risk") or "")
    no = (c.get("clause_no") or "").strip()
    base = {"clause_no": no, "ok": True, "rule_fallback": True}

    if _DELETE_PAT.search(sug + risk):
        return {**base, "revised_text": "", "deleted": True,
                "note": "该条款与法律强制性规定冲突，整条删除（规则改写）"}

    # 去掉元话语开头，得到"能放进合同的句子"
    t = sug
    prev = None
    while prev != t:
        prev = t
        t = _META_PREFIX.sub("", t).strip()
    t = _MODIFY_LEAD.sub("", t).strip()
    if not t:
        return {**base, "ok": False, "revised_text": "", "deleted": False,
                "note": "规则改写失败：修改建议为空"}

    def numbered(body: str) -> str:
        # 建议自带条款号就不重复加；替换区间在条款号之后时，build_rewritten 会剥重
        if re.match(r"^第\s*[0-9一二三四五六七八九十百零两]{1,4}\s*条", body):
            return body
        if no and re.match(r"^第", no):
            return f"{no} {body}"
        return body

    if _ADD_PAT.search(sug + risk) and orig:
        # 缺少约定类：原文合法，只是少了内容 → 保留原文并追加
        body = numbered(orig.rstrip("。；") + "；" + t)
    else:
        body = numbered(t)
    if body and body[-1] not in "。；！？":
        body += "。"
    return {**base, "revised_text": body, "deleted": False,
            "note": "模型不可用，按审查建议条款化（规则改写），请律师逐条核对"}


def _covers_clause_no(text: str, start: int) -> int:
    """若条款号标记紧贴在 start 之前，返回包含该标记的起点（用于整条删除时一起删掉编号）。"""
    head = text[:start]
    m = re.search(r"第\s*[0-9一二三四五六七八九十百零两]{1,4}\s*条\s*$", head)
    if m and "\n" not in head[m.start():]:
        return m.start()
    return start


def _strip_dup_no(head: str, new_text: str) -> str:
    """去掉改写文开头重复的条款号。

    为什么会有重复：prompt 要求"保留原条款编号"，而按条款正文定位时**编号本来就在
    替换区间之外**（区间从正文开始），模型再写一遍就变成「第二条 第二条 试用期自…」。
    只有区间的原文尾部已经带着同一个编号时才剥掉，避免把改写文里合法引用的其它条款号删掉。
    """
    m = re.search(r"第\s*([0-9一二三四五六七八九十百零两]{1,4})\s*条[\s\u3000]*$", head)
    if not m:
        return new_text
    key = _clause_key(f"第{m.group(1)}条")
    t = new_text.lstrip()
    m2 = re.match(r"第\s*([0-9一二三四五六七八九十百零两]{1,4})\s*条[\s\u3000]*", t)
    if m2 and _clause_key(f"第{m2.group(1)}条") == key:
        return t[m2.end():].lstrip()
    return new_text


def _sentences(text: str) -> list[str]:
    """按中英文句读切句，太短的丢掉（短句重复多半是巧合，不值得报）。"""
    parts = re.split(r"[。；！？;\n]+", text or "")
    return [p.strip() for p in parts if len(p.strip()) >= 15]


def find_duplicate_sentences(applied: list[dict[str, Any]],
                             *, threshold: float = 0.75, limit: int = 8) -> list[dict[str, Any]]:
    """找出**跨条款**的重复表述（安全网，不是主防线）。

    为什么需要：每条条款是独立改写的，同一个法律问题可能被两条都写一遍。
    真机上出现过：「试用期自…止，试用期工资不低于…百分之八十」同时出现在
    第一条（合同期限）和第二条（试用期）里 —— 合同交出去会显得很不专业。
    prompt 里已经加了"不要重复规定其它条款的内容"，但模型不保证听话，
    所以这里做一道确定性检查：相似度 ≥ threshold 的句子对，报出来让人删掉一处。
    只报不改 —— 自动删错地方（把该留的那条删了）比重复更糟。
    """
    from difflib import SequenceMatcher

    pool: list[tuple[str, str]] = []
    for a in applied or []:
        for s in _sentences(a.get("rewritten") or ""):
            pool.append((a.get("clause_no") or "", s))

    out: list[dict[str, Any]] = []
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            ca, sa = pool[i]
            cb, sb = pool[j]
            if ca == cb:                      # 同一条内部重复不算（多为并列项）
                continue
            ratio = SequenceMatcher(None, _WS.sub("", sa), _WS.sub("", sb)).ratio()
            if ratio >= threshold:
                out.append({"a_clause": ca, "b_clause": cb,
                            "a_text": sa[:80], "b_text": sb[:80],
                            "similarity": round(ratio, 2)})
                if len(out) >= limit:
                    return out
    return out


def find_uncovered_requirements(picked: list[dict[str, Any]],
                                applied: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """检查「修改建议」里的**硬要求**是否真的落到了改后条文里。

    为什么需要：防重复的指令（"别人要写的内容别搬过来"）会带来反向风险 ——
    两条都把某项要求让给对方，结果**两边都没写**。真机上就丢过
    "试用期工资不低于约定工资的百分之八十"这条法定底线：合同看起来改完了，
    实质保护却没了，这比重复严重得多。

    只检查能机械比对的那几类（百分比 / 年限 / 期限天数 / 经济补偿），不猜语义 ——
    宁可漏报，也不报一堆需要律师逐条排除的噪声。
    """
    import re as _re

    # 复合条款号（「第二条、第八条」）算一组：要求只要落在组里任意一条即视为已落实
    group_text: dict[str, str] = {}
    clause_group: dict[str, str] = {}
    for c in picked or []:
        key = c.get("clause_group") or c.get("clause_no") or ""
        clause_group[c.get("clause_no") or ""] = key
    for a in applied or []:
        key = clause_group.get(a.get("clause_no") or "", a.get("clause_no") or "")
        group_text[key] = group_text.get(key, "") + (a.get("rewritten") or "")

    token_re = _re.compile(
        r"不超过([一二三四五六七八九十百零\d]+(?:年|个月|日))"     # 「不超过二年」→ 核对「二年」
        r"|提前([一二三四五六七八九十百零\d]+日)"                  # 「提前三十日」→ 核对「三十日」
        r"|([一二三四五六七八九十百零\d]+日内)"
        r"|(百分之[一二三四五六七八九十百零\d]+)"                   # 比例表述比较稳定，整段核对
        r"|(经济补偿)"
    )

    def tokens_of(text: str) -> list[str]:
        out: list[str] = []
        for m in token_re.finditer(text or ""):
            tok = next((g for g in m.groups() if g), "")
            if tok:
                out.append(tok)
        return out

    out: list[dict[str, Any]] = []
    for c in picked or []:
        text = c.get("suggestion") or ""
        key = c.get("clause_group") or c.get("clause_no") or ""
        body = group_text.get(key, "")
        # 「删除/取消」类建议里出现的数字不是要求，是"要去掉的东西" ——
        # 不滤掉就会误报（建议写"删除六十日内的表述"，改后条文当然没有"六十日内"）
        kept = [
            seg for seg in _re.split(r"[；。，,\n]", text)
            if not _re.search(r"删除|删去|去掉|取消|不再|不得约定", seg)
        ]
        missing = [t for t in set(tokens_of("；".join(kept))) if t not in body]
        if missing:
            out.append({"clause_no": c.get("clause_no") or "", "clause_group": c.get("clause_group") or "",
                        "missing": sorted(missing),
                        "suggestion": text[:80]})
    return out


def _drop_sentence(text: str, sent: str) -> str:
    """从条款正文里删掉一整句（连同紧随其后的句读），并收拾残留的标点。"""
    idx = text.find(sent)
    if idx < 0:
        return text
    end = idx + len(sent)
    while end < len(text) and text[end] in "。；！？;，,、":
        end += 1
    out = text[:idx] + text[end:]
    out = re.sub(r"[。；]{2,}", "。", out)
    out = re.sub(r"^[，、；。\s]+", "", out).strip()
    return re.sub(r"[，、；。\s]+$", "", out)


def dedupe_across_clauses(applied: list[dict[str, Any]],
                          rewrites: dict[int, dict[str, Any]] | None = None,
                          *, threshold: float = 0.75) -> list[dict[str, Any]]:
    """跨条款去重：同一句表述在两条条款里各写一遍时，只保留**先出现**的那条。

    为什么必须自动处理而不是只报警：条款是各改各的，同一个法律问题（比如试用期工资）
    很容易被两条都写一遍。真机上第一条/第二条、第二条/第八条都出现过整句重复 ——
    一条合同里同一句话出现两遍，交出去显得很不专业，而让律师手动删、
    他还得先判断删哪一处。规则是确定的：**后面那条让前面那条**。

    两条安全线：
    1. 只有删完之后本条还剩 ≥15 字才删 —— 否则会把整条条款删空（那比重复严重得多）；
    2. 删了什么全部记进返回值，界面照实说明，不悄悄改。

    ``rewrites`` 传进来时会同步改掉里面的 ``revised_text`` —— 调用方随后要用
    :func:`build_rewritten` 重新拼合同，拼的是这份改过的文本。
    """
    from difflib import SequenceMatcher

    rows = [(a.get("clause_no") or "", a.get("seq"), a.get("rewritten") or "") for a in applied or []]
    removed: list[dict[str, Any]] = []
    for i in range(len(rows)):
        ci, _, ti = rows[i]
        if not ti:
            continue
        for j in range(i + 1, len(rows)):
            cj, seqj, tj = rows[j]
            if not tj:
                continue
            for sent in _sentences(ti):
                if sent in tj:
                    target = sent
                else:
                    # 换个说法也算重复：相似度够了就认
                    cands = [s for s in _sentences(tj)
                             if SequenceMatcher(None, _WS.sub("", sent), _WS.sub("", s)).ratio() >= threshold]
                    if not cands:
                        continue
                    target = cands[0]
                trial = _drop_sentence(tj, target)
                if len(_WS.sub("", trial)) < 15:
                    continue                     # 删掉就把这条删空了 → 不动，留给人工
                rows[j] = (cj, seqj, trial)
                tj = trial
                removed.append({"clause_no": cj, "kept_in": ci,
                                "sentence": target[:60], "reason": f"与 {ci} 表述重复，已去重"})
    if removed:
        for a in applied:
            for no, seq, text in rows:
                if a.get("seq") == seq:
                    a["rewritten"] = text
                    a["deduped"] = True
        if rewrites is not None:
            for _no, seq, text in rows:
                if seq in rewrites:
                    rewrites[seq]["revised_text"] = text
    return removed


def _strip_file_markers(text: str) -> str:
    """去掉解析器留下的「【1.1.jpg】」这类分页/文件标记。

    合同正文里混着文件名，交付出去很难看；这些标记是材料解析阶段给
    "这段来自哪一页/哪个文件"用的，对合同本身没有意义。
    """
    out = []
    for line in (text or "").splitlines():
        if _FILE_MARKER_RE.fullmatch(line or ""):
            continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _contract_markdown(body: str) -> str:
    """把「改完的合同」整理成**带结构的 Markdown**，供 Word/PDF 导出层排版。

    为什么要在后端做这一步：导出层（``docs._write_docx`` / ``_write_pdf``）是**按行
    认标记**的 —— ``#`` 标题、``###`` 小节、``<p align="right|center">`` 靠右/居中。
    而 :func:`build_rewritten` 拼出来的是**PDF 抽出来的纯文本**：一行一段、标题与正文
    一样大、落款混在正文里。交付出去的合同因此"格式什么都没了"（用户 2026-09-29）。

    三件事：

    1. **丢掉页标与页脚页码**（``<!-- 第 N 页 -->``、整行只有一个数字）。页数是页脚的
       事，导出层会画真页脚 —— 用户原话「页数是页脚，不是里面的文字」。
    2. **标出结构**：大标题 → ``#``（导出层渲染成居中加粗）、章节 → ``###``（顶格加粗）、
       落款日期 → ``<p align="right">``、落款其它行 → ``<p align="left">``（免掉首行缩进）。
    3. **把 PDF 的硬换行并回自然段**。原件一句话被 PDF 折成两行，不并的话导出后每个
       折行都成了独立段落（"…经甲乙双方平等自愿协商一致，同意签订本／合同，共同遵守…"）。

    不猜：认不出来的行原样当普通段落，绝不丢字（改写失败的条款已经由调用方如实保留）。
    """
    lines = (body or "").splitlines()
    blocks: list[tuple[str, str]] = []      # [(kind, text)]，kind ∈ title/chapter/para/sign/date/left
    title_left = True                        # 只有**第一个**内容块够格当大标题

    def prev():
        return blocks[-1] if blocks else None

    def mergeable(p) -> bool:
        return bool(p) and p[0] in ("para", "clause", "sign") and not _DOC_RE_END.search(p[1])

    for raw in lines:
        ln = raw.replace("　", " ").strip()
        if not ln:
            continue                          # 空行不消耗"第一个内容块"这个名额
        if _DOC_RE_PAGE.match(ln):
            continue                          # 页标/页码不进交付正文
        if _FILE_MARKER_RE.fullmatch(ln):
            continue                          # 文件标记（调用方一般已经清过，这里兜一手）
        if _DOC_RE_CHAPTER.match(ln):
            blocks.append(("chapter", ln))
            title_left = False
            continue
        if _DOC_RE_SIGN.match(ln):
            blocks.append(("sign", ln))
            title_left = False
            continue
        if _DOC_RE_DATE.match(ln):
            blocks.append(("date", ln))
            title_left = False
            continue
        # 大标题：**第一个内容块**、够短、没有句号，而且**不是条款/小节的开头**。
        # 少了最后那个排除，没有封面抬头的合同会把「第一条 本合同为固定期限…」
        # 渲染成居中加粗的大标题（与 `_guess_title` 用的是同一套判据 `_TYPE_HEAD`）。
        if (title_left and len(ln) <= 20 and "。" not in ln and not _TYPE_HEAD.match(ln)):
            blocks.append(("title", ln))
            title_left = False
            continue
        title_left = False
        # 折行：接得上去就接（不加空格 —— 断行处的空格本来就在上一行行尾，已 strip）
        if mergeable(prev()):
            blocks[-1] = (blocks[-1][0], blocks[-1][1] + ln)
            continue
        blocks.append(("para", ln))

    out: list[str] = []
    for kind, text in blocks:
        if kind == "title":
            out.append("# " + text)
        elif kind == "chapter":
            out.append("### " + text)
        elif kind == "date":
            out.append('<p align="right">' + text + "</p>")
        elif kind == "sign":
            out.append('<p align="left">' + text + "</p>")
        else:
            out.append(text)
    # 用单换行连接（不空行）：原件是**密排**的（`_pdfgeom.txt` 里正文行距均匀、段间无空行），
    # 空行会让 Word/PDF 每段之间多一个空段，交付稿比原件松散一截。
    # 预览侧 `mdLite` 对非列表块是**逐行**渲染的（`web/app.js:671`），单换行照样分行。
    return "\n".join(out).strip() + "\n"


def to_plain_text(marked: str) -> str:
    """把 :func:`_contract_markdown` 标出来的**版式标记**还原成纯文本。

    「复制改后合同」按钮走的是纯文本剪贴板（`web/app.js:1197` copyText），
    带着 `# ` / `<p align="left">` 复制出去，粘到微信或邮件里就是一堆标记。
    只脱标记，**不还原页码、不再动分段** —— 交付正文该怎么分还怎么分。
    """
    out = []
    for line in (marked or "").splitlines():
        s = _DOC_MARK_STRIP.sub("", line)
        m = _DOC_P_TAG.fullmatch(s.strip())
        out.append(m.group(1).strip() if m else s)
    return "\n".join(out).strip()


def build_rewritten(contract_text: str, picked: list[dict[str, Any]],
                    rewrites: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """把改写结果拼回合同，产出**改完的合同全文**（不含任何标记）。

    一条铁律：**没改成的不动**。某条改写失败（模型降级、返回空）时保留原文，
    并在 `skipped` 里如实列出 —— 悄悄留一条有风险的条款比报错危险得多。
    """
    text = contract_text or ""
    out: list[str] = []
    cursor = 0
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for c in picked:
        rw = rewrites.get(c["seq"]) or {}
        new_text = (rw.get("revised_text") or "").strip()
        deleted = bool(rw.get("deleted"))
        if not new_text and not deleted:
            skipped.append({"clause_no": c.get("clause_no") or "", "reason": "改写未返回有效条文，已保留原文"})
            continue
        start = c["start"]
        if deleted:
            start = _covers_clause_no(text, start)      # 整条删除时把条款号一起删掉
        out.append(text[cursor:start])
        if new_text:
            # 条款号若本来就在替换区间之外（按条款正文定位时就是这样），
            # 改写文开头再写一遍会变成「第二条 第二条 …」→ 先剥掉
            new_text = _strip_dup_no(text[:start], new_text)
            # 与前一行的衔接：原文被替换处若是行首，补一个换行，避免和上一条粘在一行
            prev = text[:start]
            lead = "" if (not prev or prev.endswith(("\n", " "))) else " "
            out.append(lead + _one_line(new_text))
        cursor = c["end"]
        applied.append({
            "seq": c["seq"], "clause_no": c.get("clause_no") or "",
            "level": c.get("level"), "deleted": deleted,
            "original": c.get("original") or "", "rewritten": new_text,
            "note": rw.get("note") or "",
            "basis": c.get("basis") or "",
        })
    out.append(text[cursor:])
    body = "".join(out)
    # 清理：去掉解析器的文件标记 + 连续 3 个以上换行压成 2 个（替换处常留下多余空行）
    body = _strip_file_markers(body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip() + "\n"
    # 交付版式：标出标题/章节/落款、丢掉页标页码、把 PDF 硬换行并回自然段。
    # ⚠ 只加工最终正文 —— `applied[].rewritten` 是逐条对照用的**单条改写文**，
    # 前端按它渲染「原文 → 改后」两栏，套上 `#`/`<p>` 标记会原样显示出来。
    body = _contract_markdown(body)
    return {"rewritten_text": body, "applied": applied, "skipped": skipped}


__all__ = ["build_revision", "build_rewritten", "to_plain_text", "rewrite_clauses", "locate_changes",
           "find_duplicate_sentences", "split_clause_nos", "rule_rewrite_one",
           "LEVEL_CN", "LEVEL_ORDER"]
