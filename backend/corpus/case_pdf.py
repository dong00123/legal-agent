"""类案语料构建：把「法条—案例对照」型 PDF 解析成结构化类案。

针对的是这种真实材料：《中华人民共和国劳动法》第1-105条对应劳动案例整理，
每条法条对应多个案例，每个案例含
``关键词 / 裁判要点 / 相关法条 / 基本案情 / 裁判结果 / 裁判理由``。

为什么单独写一个解析器而不是走通用流水线：
- 这类材料没有案号（多为最高法/人社部发布的典型案例），引用校验必须靠**案例名称 + 来源**，
  所以解析时必须把案例名称作为一等字段提取出来；
- 裁判要旨是本类材料最有价值的部分，必须单独成 block 以便精排命中。
"""

from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from backend.rag.chunker import Chunk, chunk_case

logger = logging.getLogger("legal_agent.corpus.case")

# 页眉页脚噪声
PAGE_NOISE_RE = re.compile(r"^[ \t]*第\s*\d+\s*页[ \t]*$")
DOC_TITLE_RE = re.compile(r"^[ \t]*《中华人民共和国劳动法》第1-105条对应劳动案例整理[ \t]*$")
ARTICLE_HEAD_RE = re.compile(r"(?m)^[ \t　]*第\s*(\d+)\s*条[ \t　]*$")
CASE_HEAD_RE = re.compile(r"(?m)^[ \t　]*案例\s*(\d+)\s*[:：][ \t　]*(?P<title>[^\n]{4,120})$")

FIELD_RE = {
    "keywords": re.compile(r"(?:关键词|关键字)[：:][ \t　]*(?P<v>[^\n]+)"),
    "gist": re.compile(r"裁判要点[：:][ \t　]*(?P<v>[^\n]+)"),
    "related": re.compile(r"相关法条[：:][ \t　]*(?P<v>[^\n]+)"),
    "facts": re.compile(r"基本案情[：:][ \t　]*(?P<v>[^\n]+)"),
    "result": re.compile(r"裁判结果[：:][ \t　]*(?P<v>[^\n]+)"),
    "reasoning": re.compile(r"裁判理由[：:][ \t　]*(?P<v>[^\n]+)"),
}


@dataclass
class ParsedCase:
    case_name: str = ""
    case_no: str = ""
    court: str = ""
    judge_date: str = ""
    cause_of_action: str = ""
    keywords: list[str] = field(default_factory=list)
    gist: str = ""
    facts: str = ""
    result: str = ""
    reasoning: str = ""
    related_statutes: list[str] = field(default_factory=list)
    article_ref: str = ""      # 该案例挂靠在劳动法第X条下
    source: str = ""
    source_path: str = ""


CAUSE_RULES = [
    (r"违法解除|解除劳动合同|经济补偿|赔偿金", "劳动合同纠纷（违法解除）"),
    (r"劳动关系认定|确认劳动关系", "确认劳动关系纠纷"),
    (r"加班|工时|休息休假|年休假", "劳动报酬/休息休假纠纷"),
    (r"工伤|职业病", "工伤保险待遇纠纷"),
    (r"社会保险|社保|抚恤|养老", "社会保险纠纷"),
    (r"竞业限制|保密", "竞业限制纠纷"),
    (r"劳务派遣", "劳务派遣纠纷"),
    (r"工资|劳动报酬|拖欠", "劳动报酬纠纷"),
    (r"性骚扰|规章制度|违纪", "劳动合同纠纷（规章制度）"),
]


def guess_cause(text: str) -> str:
    for pattern, cause in CAUSE_RULES:
        if re.search(pattern, text or ""):
            return cause
    return "劳动争议"


def parse_case_pdf(
    path: str | Path,
    *,
    law_name: str = "《中华人民共和国劳动法》",
    source: str = "最高人民法院/人社部典型案例整理",
) -> list[ParsedCase]:
    """解析类案 PDF。"""
    import pymupdf

    path = Path(path)
    doc = pymupdf.open(str(path))
    try:
        raw = "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()

    lines = []
    for line in raw.splitlines():
        if PAGE_NOISE_RE.match(line) or DOC_TITLE_RE.match(line):
            continue
        lines.append(line)
    text = "\n".join(lines)

    cases: list[ParsedCase] = []
    case_marks = list(CASE_HEAD_RE.finditer(text))

    for i, m in enumerate(case_marks):
        start = m.start()
        end = case_marks[i + 1].start() if i + 1 < len(case_marks) else len(text)
        block = text[start:end]
        # 该案例挂靠的法条：往上找最近的「第N条」
        prefix = text[:start]
        art_matches = list(ARTICLE_HEAD_RE.finditer(prefix))
        article_ref = f"第{art_matches[-1].group(1)}条" if art_matches else ""

        item = ParsedCase(
            case_name=m.group("title").strip(),
            article_ref=article_ref,
            source=source,
            source_path=str(path),
        )
        for key, pat in FIELD_RE.items():
            fm = pat.search(block)
            if not fm:
                continue
            value = _clean_field(fm.group("v"))
            if key == "keywords":
                item.keywords = [k.strip() for k in re.split(r"[；;、,，]", value) if k.strip()]
            elif key == "related":
                item.related_statutes = [s.strip() for s in re.split(r"[；;]", value) if s.strip()]
            else:
                setattr(item, key, value)
        # 多行字段（基本案情/裁判理由可能跨行）
        item.facts = _multi_line(block, "基本案情", item.facts)
        item.reasoning = _multi_line(block, "裁判理由", item.reasoning)
        item.result = _multi_line(block, "裁判结果", item.result)
        item.gist = _multi_line(block, "裁判要点", item.gist)

        item.cause_of_action = guess_cause(item.gist + item.facts + item.case_name)
        if not item.result and not item.gist:
            logger.debug("案例块字段不全，跳过：%s", item.case_name)
            continue
        cases.append(item)

    logger.info("类案解析完成：%s，共 %d 个案例", path.name, len(cases))
    return cases


def _clean_field(value: str) -> str:
    return re.sub(r"[ \t　]+", " ", (value or "").strip())


def _multi_line(block: str, label: str, current: str) -> str:
    """抓取「标签：」之后直到下一个标签之前的多行内容。

    边界要同时覆盖三种情况：下一个案例、下一个字段、下一条法条。
    其中「第N条法条原文」常常与前文挤在同一行（PDF 抽取没有换行），
    所以必须单独加一条不要求行首的边界，否则上一案例的裁判理由会串到下一条法条去。
    """
    pat = re.compile(
        rf"{label}[：:][ \t　]*(?P<v>.*?)(?="
        rf"\n[ \t　]*(?:案例\s*\d+\s*[:：]|关键词|裁判要点|相关法条|基本案情|裁判结果|裁判理由)"
        rf"|\n[ \t　]*第\s*\d+\s*条"
        rf"|第\s*\d+\s*条\s*法条原文"
        rf"|\Z)",
        re.S,
    )
    m = pat.search(block)
    if not m:
        return current
    value = re.sub(r"\s*\n\s*", "", m.group("v")).strip()
    return value[:2000] or current


def clean_related_statutes(statutes: list[str]) -> list[str]:
    """清洗「相关法条」字段。

    这个字段是正则从 PDF 里抓的，会带入两类噪声：
    1. 被换行截断的半句话（如「并可结合劳动合同、工资支付、社会保险、劳动争议处」）；
    2. 没有书名号的裸文本。
    这些噪声会随 chunk 进入检索结果，让"相关法条"看起来像乱码。
    只保留**像法条引用**的条目（含《》或 "第X条" 或法规体裁词），其余丢弃。
    """
    out: list[str] = []
    for s in statutes or []:
        t = (s or "").strip()
        if not t:
            continue
        looks_citable = (
            ("《" in t and "》" in t)
            or re.search(r"第\s*[一二三四五六七八九十百零两\d]+\s*条", t) is not None
            or any(k in t for k in ("法", "条例", "司法解释", "规定", "办法", "解释"))
        )
        # 截断句特征：以「并可」「以及」「结合」等连接词开头，或长度超出正常引用范围
        broken = t.startswith(("并可", "以及", "结合", "还可", "参见", "见")) or len(t) > 80
        if looks_citable and not broken:
            out.append(t)
    return list(dict.fromkeys(out))


def merge_duplicate_cases(cases: list[ParsedCase]) -> list[ParsedCase]:
    """按案例名归并重复收录的判例。

    为什么需要：本项目这份 PDF 是**按法条分册**编排的，同一个判例会出现在多个条文下
    （例如「马筱楠诉北京搜狐新动力信息技术有限公司竞业限制纠纷案」同时收录在
    「第18条」与「第22条」两处）。如果不去重：

    1. 类案检索的 top-k 会被同一个判例占掉多个名额，实际召回的有效判例变少；
    2. 同一个判例以不同 ``related_statutes`` 出现，引用核验时会被当成两个不同来源。

    归并策略：同案例名的取**信息最全**的一条作为主体，把各条目的 ``related_statutes``
    与 ``article_ref`` 合并保留（条文↔判例的对应关系不能丢，它正是"按条文找类案"的依据）。
    无案例名的条目不参与归并（无法判断是否同一案件，宁可重复也不误合）。
    """
    if not cases:
        return []

    merged: dict[str, ParsedCase] = {}
    unordered: list[ParsedCase] = []
    order: list[str] = []

    for c in cases:
        name = (c.case_name or "").strip()
        if not name:
            unordered.append(c)
            continue
        if name not in merged:
            # ParsedCase 是 dataclass（不是 pydantic 模型），用 deepcopy 而不是 model_copy
            merged[name] = copy.deepcopy(c)
            order.append(name)
            continue

        base = merged[name]
        # 合并相关法条（去重且保序）
        combined = list(dict.fromkeys([*base.related_statutes, *c.related_statutes]))
        base.related_statutes = combined
        # 合并关键词
        base.keywords = list(dict.fromkeys([*base.keywords, *c.keywords]))
        # 字段补齐：谁有内容用谁的（不覆盖已有的非空值）
        for field_name in ("gist", "result", "reasoning", "facts", "court",
                           "judge_date", "cause_of_action", "case_no", "source"):
            if not getattr(base, field_name) and getattr(c, field_name):
                setattr(base, field_name, getattr(c, field_name))
        # 取更长的一条正文，避免丢失信息
        if len(c.facts or "") > len(base.facts or ""):
            base.facts = c.facts
        if len(c.reasoning or "") > len(base.reasoning or ""):
            base.reasoning = c.reasoning

    out = [merged[n] for n in order] + unordered
    # 顺手清洗相关法条，让 case_summarize / case_no_verify 的输出也干净
    for c in out:
        c.related_statutes = clean_related_statutes(c.related_statutes)
    if len(out) != len(cases):
        logger.info("类案归并：解析 %d 条 → 归并为 %d 个唯一判例（重复收录已合并相关法条）",
                    len(cases), len(out))
    return out


def cases_to_chunks(cases: list[ParsedCase], law_name: str = "《中华人民共和国劳动法》") -> list[Chunk]:
    """结构化案例 → 检索 chunk。"""
    chunks: list[Chunk] = []
    for ordinal, c in enumerate(cases):
        related = clean_related_statutes(list(c.related_statutes))
        if c.article_ref:
            related.append(f"{law_name}{c.article_ref}")
        related = list(dict.fromkeys(related))
        chunks.extend(
            chunk_case(
                text=c.facts,
                case_no=c.case_no,
                case_name=c.case_name,
                court=c.court,
                judge_date=c.judge_date,
                cause_of_action=c.cause_of_action,
                source_path=c.source_path,
                gist=c.gist,
                result=c.result,
                reasoning=c.reasoning,
                keywords=c.keywords,
                related_statutes=related,
                source=c.source,
                # ordinal 保证 chunk_id 唯一：这些案例全部没有案号，
                # 同一案例名还会重复出现（见 merge_duplicate_cases 的说明）
                ordinal=ordinal,
            )
        )
    return chunks
