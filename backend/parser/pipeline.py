"""文档解析总流水线。

    PDF / Word / 扫描卷宗
      ↓ 判断文档类型（合同/裁判文书/证据材料/法律意见书）
      ↓ 文本抽取（PyMuPDF）
      ↓ 表格/印章/签字页/图片 → 占位符标记
      ├─ 表格 → 单独结构化解析
      └─ 扫描件/手写批注/示意图 → VLM 多模态识别 → 语义描述
      ↓ 按 Placeholder 回插
      ↓ 结构化文档（按文书类型定制结构）
      ↓ （交由 rag.chunker 按条款/段落语义切分）
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.app.config import settings
from backend.parser import text_extractor as tx
from backend.parser.doc_type import DocTypeResult, guess_cause_of_action, judge_doc_type
from backend.parser.structure import (
    Clause,
    Party,
    extract_parties,
    split_articles,
    split_clauses,
    split_litigation_sections,
    strip_toc,
)
from backend.parser.table_parser import parse_table

logger = logging.getLogger("legal_agent.parser.pipeline")

# text_extractor 按页抽取时插入的页码占位符（如 ``<!-- 第 38 页 -->``）。
# 它是页级占位符，不属于任何一条法条的正文，必须在切条前整行/行内一并剥离。
# ⚠ 不锚行首行尾：实际残留经常嵌在句中（“诉讼时效中断，<!-- 第 38 页 -->从中断…”），
# 整行锚定的 _TAIL_PAGE_RE（structure.py，合同路径专用）清不掉这种。
_PAGE_MARK_RE = re.compile(r"<!--\s*第\s*\d+\s*页\s*-->")


@dataclass
class ParsedDocument:
    source_path: str = ""
    filename: str = ""
    doc_type: str = "unknown"
    doc_type_confidence: float = 0.0
    raw_text: str = ""
    text: str = ""                      # 回插后的对齐文本
    pages: int = 0
    clauses: list[Clause] = field(default_factory=list)
    parties: list[Party] = field(default_factory=list)
    facts: list[dict] = field(default_factory=list)
    tables: list[dict] = field(default_factory=list)
    blocks: list[dict] = field(default_factory=list)
    litigation_sections: dict[str, str] = field(default_factory=dict)
    cause_of_action: str = ""
    sha256: str = ""
    warnings: list[str] = field(default_factory=list)
    evidence_gaps: list[dict] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    # 解析产物（``*.parsed.json``）落盘后的绝对路径，由 ``save_parsed`` 回写。
    # ⚠️ **加在字段列表末尾**：这个 dataclass 有按位置构造的调用方，插在中间会
    # 让它们静默错位（字段类型都差不多，错位了不会报错，只会读出莫名其妙的值）。
    parsed_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "filename": self.filename,
            "doc_type": self.doc_type,
            "doc_type_confidence": self.doc_type_confidence,
            "pages": self.pages,
            "text": self.text,
            "clauses": [c.__dict__ for c in self.clauses],
            "parties": [p.__dict__ for p in self.parties],
            "facts": self.facts,
            "tables": self.tables,
            "blocks": self.blocks,
            "litigation_sections": self.litigation_sections,
            "cause_of_action": self.cause_of_action,
            "sha256": self.sha256,
            "warnings": self.warnings,
            "evidence_gaps": self.evidence_gaps,
            "stats": self.stats,
        }

    def clause_text(self) -> str:
        """按条款拼回的合同正文（供条款定位与逐条高亮）。"""
        if not self.clauses:
            return self.text
        return "\n\n".join(c.clause_text for c in self.clauses)


def save_parsed(doc: ParsedDocument, *, dir_path: str | Path | None = None) -> str:
    """把解析产物落盘，**并把路径记回 ``doc.parsed_path``**；返回该路径（失败回空串）。

    为什么必须记回 ``doc``，而不是让调用方自己拼文件名：
    ``case_material.parsed_path`` 是「查看材料正文」与证据链唯一的正文来源
    （见 ``case_service._material_text``）。此前只有 Agent 的 ``material_parse``
    tool 落盘，而且**只往自己的返回 payload 里塞路径，没回填到 doc** ——
    于是 ``POST /materials/{id}/parse``（Web 上传、微信上传走的都是这条）
    取到的 ``getattr(doc, "parsed_path", "")`` **永远是空串**。后果有两个，
    都在界面上表现为"点了也没用"：

      1. 图片 / PDF / Word 材料的「查看正文」永远显示"还没有可读的正文"，
         因为解析产物这一路取不到、原文那一路是二进制读不出文字 ——
         而界面还在叫律师"点解析"，点多少次都一样。
      2. 证据链从材料里读正文时，同样只认得 ``parsed_path``；PDF 与扫描件
         的正文全部读成空，链上凭空多出一堆"缺口"。

    落盘放在管线里（而不是各个调用方各写一遍）是因为：三条调用路径
    （解析路由 / 微信服务 / Agent tool）共享这一个出口，写两处必然分叉。
    """
    try:
        out_dir = Path(dir_path or settings.PROCESSED_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        # 文件名用**上传后的存储名**做 stem。它已经带了内容哈希前缀
        # （``e303d26c0bd33d86_合同.txt``），所以不同案件的同名材料不会互相覆盖。
        stem = Path(doc.source_path or doc.filename or "doc").stem
        out_file = out_dir / f"{stem}.parsed.json"
        payload = doc.to_dict()
        payload["parsed_path"] = str(out_file)
        out_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        doc.parsed_path = str(out_file)
        return doc.parsed_path
    except Exception as exc:  # noqa: BLE001
        # 落盘失败**不该让解析本身失败**：正文还在内存里，调用方拿得到 text。
        # 但没有产物 = 界面与证据链读不到正文，所以这条日志是排查的入口。
        logger.warning("解析产物落盘失败 %s：%s", doc.filename, exc)
        return ""


async def parse_document(
    path: str | Path,
    *,
    do_vision: bool | None = None,
    hint: str = "",
    max_pages: int | None = None,
) -> ParsedDocument:
    """解析单个文档。"""
    started = time.perf_counter()
    path = Path(path)
    doc = ParsedDocument(source_path=str(path), filename=path.name)

    # 1) 文本抽取（含占位符标记）
    try:
        extraction = await asyncio.to_thread(
            tx.extract, path, image_dir=str(Path(settings.PROCESSED_DIR) / "images"), max_pages=max_pages
        )
    except Exception as exc:  # noqa: BLE001
        doc.warnings.append(f"抽取失败：{exc}")
        doc.evidence_gaps.append({"kind": "parser", "reason": str(exc), "impact": "该材料未能进入检索库"})
        # **失败也要落盘**（2026-10-12）。从前这里直接 return，于是这次失败只活在
        # 内存里：律师在界面上看到的是"没有可读的正文 —— 请先点解析"，而**原因**
        # （"不支持的文件类型：.xlsx"之类）随函数返回一起没了，他就只能一直点。
        # 产物在这里的价值不是正文，是那句原因 —— 界面靠 ``_parsed_gap_reason`` 读它。
        save_parsed(doc)
        return doc

    doc.raw_text = extraction.text
    doc.pages = extraction.pages
    doc.sha256 = extraction.sha256
    doc.warnings.extend(extraction.warnings)

    # 2) 文档类型判定
    dt: DocTypeResult = judge_doc_type(extraction.text, path.name)
    doc.doc_type = dt.doc_type
    doc.doc_type_confidence = dt.confidence
    doc.cause_of_action = guess_cause_of_action(extraction.text)

    # 3) 表格结构化
    table_blocks = [b for b in extraction.blocks if b.kind == "table"]
    for b in table_blocks:
        parsed = parse_table(b.rows, b.placeholder)
        b.semantic = parsed.semantic
        b.parsed = {
            "kind": parsed.kind,
            "headers": parsed.headers,
            "records": parsed.records,
            "issues": parsed.issues,
        }
        doc.tables.append(
            {
                "placeholder": b.placeholder,
                "page": b.page,
                "kind": parsed.kind,
                "headers": parsed.headers,
                "records": parsed.records,
                "semantic": parsed.semantic,
                "row_count": len(parsed.records),
            }
        )

    # 4) 多模态识别（图片 / 扫描件 / 手写批注 / 结构图）
    image_blocks = [b for b in extraction.blocks if b.kind == "image"]
    use_vision = settings.VLM_ENABLED if do_vision is None else do_vision
    if image_blocks:
        if use_vision:
            from backend.parser.vision.client import vision_client

            ok = await vision_client.probe()
            if ok:
                results = await asyncio.gather(
                    *[vision_client.describe_image(b.image_path or "", hint=hint or path.name) for b in image_blocks],
                    return_exceptions=True,
                )
                degraded = 0
                for b, r in zip(image_blocks, results):
                    if isinstance(r, Exception):
                        degraded += 1
                        b.semantic = "图像内容未识别（识别异常）"
                        continue
                    b.semantic = r.semantic
                    if r.degraded:
                        degraded += 1
                if degraded:
                    doc.evidence_gaps.append(
                        {
                            "kind": "vlm",
                            "reason": f"{degraded}/{len(image_blocks)} 张图片未能识别",
                            "impact": "图像中的要素信息缺失，其余文本分析不受影响",
                        }
                    )
            else:
                for b in image_blocks:
                    b.semantic = "图像内容未识别（多模态服务不可用，仅保留占位）"
                doc.evidence_gaps.append(
                    {
                        "kind": "vlm",
                        "reason": "多模态识别服务不可用",
                        "impact": "扫描件/手写批注/结构图的语义信息未纳入分析",
                    }
                )
        else:
            # 视觉**被显式关掉**（``do_vision=False`` 或 ``VLM_ENABLED=false``）：
            # 图里的内容不是"识别失败"，是根本没试。这一条不记，``evidence_gaps``
            # 就是空的 —— 界面只能含糊地说"没有抽出文字"，而律师无从知道该去开视觉，
            # 只能反复点「解析」（2026-10-12）。
            for b in image_blocks:
                b.semantic = "图像内容未识别（多模态识别已关闭）"
            doc.evidence_gaps.append(
                {
                    "kind": "vlm",
                    "reason": "多模态识别已关闭（VLM_ENABLED=false 或 do_vision=false）",
                    "impact": "扫描件与图片的语义信息未纳入分析",
                }
            )

    # 5) 占位符回插
    doc.text = tx.splice_placeholders(extraction.text, extraction.blocks)
    doc.blocks = [
        {
            "placeholder": b.placeholder,
            "kind": b.kind,
            "page": b.page,
            "semantic": b.semantic,
            "parsed": b.parsed,
        }
        for b in extraction.blocks
    ]

    # 6) 按文书类型定制结构
    if doc.doc_type == "statute":
        # 法规：剥掉目录后按「第X条」切分，条文号即最小检索单位
        doc.text = strip_toc(doc.text)
        # 剥离 text_extractor 插入的页码占位符（嵌在句中的也删），
        # 否则页标会随条文正文进入 chunk、污染 embedding 与引用展示。
        doc.text = _PAGE_MARK_RE.sub("", doc.text)
        doc.clauses = [
            Clause(clause_no=a.article_no, clause_title=a.heading, clause_text=a.text, index=a.index)
            for a in split_articles(doc.text)
        ]
    elif doc.doc_type == "contract":
        doc.clauses = split_clauses(doc.text)
    elif doc.doc_type == "litigation":
        doc.litigation_sections = split_litigation_sections(doc.text)
        doc.clauses = split_clauses(doc.litigation_sections.get("reasoning", "") or doc.text)
    else:
        doc.clauses = split_clauses(doc.text)

    doc.parties = extract_parties(doc.text)

    doc.stats = {
        "chars": len(doc.text),
        "clauses": len(doc.clauses),
        "tables": len(doc.tables),
        "images": len(image_blocks),
        "parties": len(doc.parties),
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }
    logger.info(
        "解析完成 %s | 类型=%s(%.2f) 字数=%d 条款=%d 表格=%d 图片=%d 耗时=%dms",
        path.name, doc.doc_type, doc.doc_type_confidence, len(doc.text),
        len(doc.clauses), len(doc.tables), len(image_blocks), doc.stats["elapsed_ms"],
    )
    # 7) 落盘产物并记回 ``parsed_path``。**放在 return 之前**：调用方拿到 doc 时
    #    ``doc.parsed_path`` 就必须已经可用了 —— 那两个调用方（解析路由、
    #    微信服务）写的都是 ``getattr(doc, "parsed_path", "")``，靠这一行才有值。
    save_parsed(doc)
    return doc


async def parse_many(paths: list[str | Path], *, do_vision: bool | None = None) -> list[ParsedDocument]:
    """批量解析（对应 FR1 批量上传 / RabbitMQ 异步任务）。"""
    out: list[ParsedDocument] = []
    for p in paths:
        try:
            out.append(await parse_document(p, do_vision=do_vision))
        except Exception as exc:  # noqa: BLE001
            logger.warning("解析失败 %s：%s", p, exc)
    return out
