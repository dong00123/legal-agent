"""文书模板语料：把律所的各类文书模板（Word）纳入知识库。

模板的作用有两个：
1. **Knowledge RAG**：文书生成 Agent 检索"本所同类文书模板"，据其结构与表述起草；
2. **格式规范化**：DocumentGenerator 用模板的段落骨架校验生成草稿的完整性
   （是否缺"请求事项"、是否缺"此致"等）。

因此除了文本本身，还要抽取模板的**段落骨架**。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from backend.rag.chunker import Chunk, chunk_knowledge

logger = logging.getLogger("legal_agent.corpus.templates")

# 文书骨架关键段落
SKELETON_PATTERNS: list[tuple[str, str]] = [
    ("title", r"(?:起诉状|答辩状|申请书|意见书|代理意见|上诉状|和解协议|调解协议)"),
    ("parties", r"^(?:申请人|被申请人|原告|被告|上诉人|被上诉人|当事人)[：:]"),
    ("claims", r"^(?:仲裁请求|诉讼请求|请求事项|上诉请求)[：:]?"),
    ("facts", r"^(?:事实(?:与理由)?|事实和理由|基本事实)[：:]?"),
    ("reasons", r"^(?:理由|法律依据)[：:]?"),
    ("evidence", r"^(?:证据清单|附件|证据目录)[：:]?"),
    ("closing", r"^(?:此致|特此申请|特此起诉|具状人|申请人|起诉人|答辩人)"),
    ("date", r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日"),
]

DOC_TYPE_MAP = [
    (r"仲裁申请书", "arbitration_application"),
    (r"起诉状", "complaint"),
    (r"答辩状", "defense"),
    (r"上诉状", "appeal"),
    (r"意见书|代理意见", "legal_opinion"),
    (r"合同|协议", "contract_template"),
]


@dataclass
class TemplateDoc:
    title: str
    doc_type: str
    region: str = ""
    text: str = ""
    skeleton: list[str] = field(default_factory=list)
    source_path: str = ""
    version: str = "v1.0"


def _guess_doc_type(name: str) -> str:
    for pattern, dtype in DOC_TYPE_MAP:
        if re.search(pattern, name):
            return dtype
    return "other"


def _guess_region(name: str) -> str:
    provinces = (
        "北京", "上海", "天津", "重庆", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江",
        "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东",
        "广西", "海南", "四川", "贵州", "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆",
    )
    for p in provinces:
        if name.startswith(p):
            return p
    return ""


def parse_template(path: str | Path) -> TemplateDoc | None:
    """解析单个 Word 模板。"""
    path = Path(path)
    try:
        import docx

        document = docx.Document(str(path))
        paragraphs = [p.text.strip() for p in document.paragraphs if p.text and p.text.strip()]
    except Exception as exc:  # noqa: BLE001
        logger.warning("模板解析失败 %s：%s", path.name, exc)
        return None

    if not paragraphs:
        return None

    text = "\n".join(paragraphs)
    skeleton: list[str] = []
    for section, pattern in SKELETON_PATTERNS:
        if re.search(pattern, text, re.M):
            skeleton.append(section)

    stem = path.stem
    return TemplateDoc(
        title=stem,
        doc_type=_guess_doc_type(stem),
        region=_guess_region(stem),
        text=text,
        skeleton=skeleton,
        source_path=str(path),
    )


def load_templates(template_dir: str | Path) -> list[TemplateDoc]:
    root = Path(template_dir)
    if not root.exists():
        return []
    out: list[TemplateDoc] = []
    for p in sorted(root.rglob("*")):
        if p.suffix.lower() not in (".docx", ".doc"):
            continue
        doc = parse_template(p)
        if doc:
            out.append(doc)
    logger.info("文书模板解析完成：%d 个", len(out))
    return out


def templates_to_chunks(templates: list[TemplateDoc]) -> list[Chunk]:
    chunks: list[Chunk] = []
    for t in templates:
        header = f"模板类型：{t.doc_type}｜地域：{t.region or '通用'}｜骨架：{','.join(t.skeleton) or '未识别'}\n"
        chunks.extend(
            chunk_knowledge(
                header + t.text,
                title=t.title,
                doc_type="template",
                version=t.version,
                category=f"文书模板-{t.region or '通用'}",
                source_path=t.source_path,
            )
        )
    return chunks


def skeleton_by_type(templates: list[TemplateDoc], doc_type: str) -> list[str]:
    """取某类文书的典型骨架，供格式规范化校验使用。"""
    for t in templates:
        if t.doc_type == doc_type and t.skeleton:
            return t.skeleton
    return []
