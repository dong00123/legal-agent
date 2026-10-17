"""Chunk 切分。

法律场景的检索质量极度依赖切分粒度：
- 检索式往往是「《劳动合同法》第八十七条」「违约金过高怎么认定」，
  命中单元必须是**一条法条 / 一个合同条款**，而不是 512 字的滑动窗口；
- 因此切分优先级：条文 → 条款 → 章节 → 段落窗口（兜底）。

每个 chunk 都携带 ``出处元数据``（法规名 + 条文号 + 效力状态 / 案号 + 法院），
这是「引用可溯源」的物理基础。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

MAX_CHUNK_CHARS = 900
MIN_CHUNK_CHARS = 30
OVERLAP_CHARS = 80

# 页码占位符（text_extractor 每页插入，如 ``<!-- 第 38 页 -->``）。
# chunk 入口兜底：pipeline 已源头剥离，但直接调 chunk_statute 的路径也要保证干净。
_PAGE_MARK_RE = re.compile(r"<!--\s*第\s*\d+\s*页\s*-->")


@dataclass
class Chunk:
    chunk_id: str
    text: str
    corpus: str                  # statute / case / knowledge / memory
    metadata: dict[str, Any] = field(default_factory=dict)
    index: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"chunk_id": self.chunk_id, "text": self.text, "corpus": self.corpus,
                "metadata": self.metadata, "index": self.index}


def make_chunk_id(corpus: str, source: str, key: str) -> str:
    raw = f"{corpus}|{source}|{key}".encode("utf-8")
    return f"{corpus[:3]}_{hashlib.blake2b(raw, digest_size=8).hexdigest()}"


def _window_split(text: str, max_chars: int = MAX_CHUNK_CHARS, overlap: int = OVERLAP_CHARS) -> list[str]:
    """按段落/句子边界做带重叠的窗口切分（兜底策略）。"""
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if len(text) >= MIN_CHUNK_CHARS else ([text] if text else [])

    pieces: list[str] = []
    buf = ""
    for para in re.split(r"(?<=[。；;！？\n])", text):
        if not para:
            continue
        if len(buf) + len(para) <= max_chars:
            buf += para
            continue
        if buf.strip():
            pieces.append(buf.strip())
        buf = (buf[-overlap:] if overlap and len(buf) > overlap else "") + para
    if buf.strip():
        pieces.append(buf.strip())

    # 仍然超长的单句硬切
    out: list[str] = []
    for p in pieces:
        if len(p) <= max_chars * 1.5:
            out.append(p)
        else:
            for i in range(0, len(p), max_chars):
                out.append(p[i : i + max_chars])
    return [p for p in out if p]


# --------------------------------------------------------------------------- 法规
def chunk_statute(
    text: str,
    *,
    doc_name: str,
    source_path: str,
    category: str = "",
    level: str = "",
    publisher: str = "",
    publish_date: str = "",
    effective_date: str = "",
    status: str = "现行有效",
    version: str = "",
    clauses: list[dict[str, Any]] | None = None,
) -> list[Chunk]:
    """法规按「第X条」切分，一条一个 chunk。

    每条 chunk 都带 ``article_no`` 与 ``status``，检索结果可直接落到
    「《法规名》第X条（现行有效）」这一最小可核验单元。
    """
    chunks: list[Chunk] = []
    items = clauses or []
    for i, c in enumerate(items):
        body = (c.get("clause_text") or "").strip()
        if not body:
            continue
        # 兜底剥离页码占位符（pipeline 已源头剥离，这里防直接调用路径残留）
        body = _PAGE_MARK_RE.sub("", body).strip()
        article_no = (c.get("clause_no") or "").strip()
        heading = (c.get("clause_title") or "").strip()
        # 把条款标题拼进正文，提升检索命中（标题常含争议焦点关键词）
        text_for_index = f"{doc_name}{article_no} {heading}\n{body}" if heading else f"{doc_name}{article_no}\n{body}"

        pieces = _window_split(text_for_index) if len(text_for_index) > MAX_CHUNK_CHARS else [text_for_index]
        for j, piece in enumerate(pieces):
            # key 必须包含**位置下标 i**，不能只用 article_no。
            # 踩过的坑：相当一部分法规（通知、规定、办法）的正文用「一、二、三」或
            # 表格编号，`split_articles` 拿不到「第X条」，article_no 全是空串；
            # 此时 key 退化成 `#0`，同一文档内所有条款的 chunk_id 完全相同，
            # Milvus 批量 upsert 会整体报
            # "duplicate primary keys are not allowed in the same batch"，
            # **整批被拒**（不是跳过重复项），BM25 却写进去了 ——
            # 结果就是"BM25 有、向量库空"，检索静默退化成单路。
            key = f"{article_no or 'p'}#{i}#{j}"
            chunks.append(
                Chunk(
                    chunk_id=make_chunk_id("statute", source_path, key),
                    text=piece,
                    corpus="statute",
                    index=len(chunks),
                    metadata={
                        "doc_name": doc_name,
                        "article_no": article_no,
                        "heading": heading,
                        "status": status,
                        "category": category,
                        "level": level,
                        "publisher": publisher,
                        "publish_date": publish_date,
                        "effective_date": effective_date,
                        "version": version,
                        "source_path": source_path,
                        "part": j,
                    },
                )
            )
    return chunks


# --------------------------------------------------------------------------- 合同
def chunk_contract(
    text: str,
    *,
    title: str,
    source_path: str,
    case_id: str = "",
    doc_type: str = "contract",
    parties: list[dict] | None = None,
) -> list[Chunk]:
    """合同/文书按条款切分；每个 chunk 记录条款下标，供 EXE 端「逐条高亮」定位。"""
    from backend.parser.structure import split_clauses

    clauses = split_clauses(text)
    chunks: list[Chunk] = []
    if clauses:
        for i, c in enumerate(clauses):
            body = (c.clause_text or "").strip()
            if not body:
                continue
            piece = f"{title} {c.clause_no} {c.clause_title}\n{body}"
            pieces = _window_split(piece) if len(piece) > MAX_CHUNK_CHARS else [piece]
            for j, p in enumerate(pieces):
                chunks.append(
                    Chunk(
                        chunk_id=make_chunk_id("material", source_path, f"{c.clause_no}#{j}"),
                        text=p,
                        corpus="material",
                        index=len(chunks),
                        metadata={
                            "title": title,
                            "clause_no": c.clause_no,
                            "clause_title": c.clause_title,
                            "clause_index": c.index,
                            "part": j,
                            "case_id": case_id,
                            "doc_type": doc_type,
                            "source_path": source_path,
                        },
                    )
                )
    else:
        for i, p in enumerate(_window_split(text)):
            chunks.append(
                Chunk(
                    chunk_id=make_chunk_id("material", source_path, f"p{i}"),
                    text=p,
                    corpus="material",
                    index=i,
                    metadata={"title": title, "case_id": case_id, "doc_type": doc_type, "source_path": source_path},
                )
            )
    return chunks


# --------------------------------------------------------------------------- 判例
def chunk_case(
    text: str,
    *,
    case_no: str = "",
    case_name: str = "",
    court: str = "",
    judge_date: str = "",
    cause_of_action: str = "",
    source_path: str,
    gist: str = "",
    result: str = "",
    reasoning: str = "",
    keywords: list[str] | None = None,
    related_statutes: list[str] | None = None,
    source: str = "",
    issues: list[str] | None = None,
    ordinal: int = -1,
) -> list[Chunk]:
    """判例按「关键词 / 裁判要旨 / 裁判理由 / 裁判结果」切分，案号或案例名称必带其一。

    ``ordinal`` 是该判例在语料中的序号，会进入 chunk_id。
    为什么必须带它：很多公开典型案例**没有案号**（本项目的 93 个案例全部无案号），
    且同一判例会被按条文分册重复收录（同一案例名出现在「第18条」和「第22条」两处）。
    此时 ``id_key`` 退化为案例名，两处生成的 chunk_id 完全相同，
    批量 upsert 会被 Milvus 整体拒绝。
    """
    chunks: list[Chunk] = []
    blocks: list[tuple[str, str]] = []
    if keywords:
        blocks.append(("关键词", "；".join(keywords)))
    if gist:
        blocks.append(("裁判要旨", gist))
    if reasoning:
        blocks.append(("裁判理由", reasoning))
    if result:
        blocks.append(("裁判结果", result))
    if related_statutes:
        blocks.append(("相关法条", "；".join(related_statutes)))
    if text:
        for piece in _window_split(text):
            blocks.append(("基本案情", piece))

    id_key = case_no or case_name or source_path
    if ordinal >= 0:
        id_key = f"{id_key}#{ordinal}"
    for i, (label, body) in enumerate(blocks):
        head_bits = []
        if case_no:
            head_bits.append(f"案号：{case_no}")
        if case_name:
            head_bits.append(f"案例名称：{case_name}")
        if court:
            head_bits.append(f"法院：{court}")
        if judge_date:
            head_bits.append(f"裁判日期：{judge_date}")
        if cause_of_action:
            head_bits.append(f"案由：{cause_of_action}")
        head = "｜".join(head_bits)
        chunks.append(
            Chunk(
                chunk_id=make_chunk_id("case", source_path, f"{id_key}|{label}{i}"),
                text=f"{head}\n{label}：{body}",
                corpus="case",
                index=i,
                metadata={
                    "case_no": case_no,
                    "case_name": case_name,
                    "court": court,
                    "judge_date": judge_date,
                    "cause_of_action": cause_of_action,
                    "keywords": keywords or [],
                    "gist": gist,
                    "result": result,
                    "reasoning": reasoning,
                    "related_statutes": related_statutes or [],
                    "source": source,
                    "section": label,
                    "issues": issues or [],
                    "source_path": source_path,
                },
            )
        )
    return chunks


# --------------------------------------------------------------------------- 知识库
def chunk_knowledge(
    text: str,
    *,
    title: str,
    doc_type: str,
    version: str,
    source_path: str,
    category: str = "",
    effective_date: str = "",
) -> list[Chunk]:
    """律所知识库：按标题层级/段落切分，必须带指引版本。"""
    chunks: list[Chunk] = []
    sections = re.split(r"(?m)^(?=#{1,4}\s|[一二三四五六七八九十]+[、.]\s*\S|【[^】]{2,20}】)", text or "")
    for i, sec in enumerate(s for s in sections if s and s.strip()):
        for j, piece in enumerate(_window_split(sec)):
            chunks.append(
                Chunk(
                    chunk_id=make_chunk_id("knowledge", source_path, f"{i}-{j}"),
                    text=f"{title}（{doc_type}，版本：{version}）\n{piece}",
                    corpus="knowledge",
                    index=len(chunks),
                    metadata={
                        "title": title,
                        "doc_type": doc_type,
                        "version": version,
                        "category": category,
                        "effective_date": effective_date,
                        "section_index": i,
                        "part": j,
                        "source_path": source_path,
                    },
                )
            )
    return chunks


def chunk_memory(text: str, *, title: str, source_path: str = "", tags: list[str] | None = None) -> list[Chunk]:
    """脱敏后的办案经验进入语义记忆。"""
    out: list[Chunk] = []
    for i, piece in enumerate(_window_split(text)):
        out.append(
            Chunk(
                chunk_id=make_chunk_id("memory", source_path or title, f"m{i}"),
                text=f"{title}\n{piece}",
                corpus="memory",
                index=i,
                metadata={"title": title, "tags": tags or [], "source_path": source_path},
            )
        )
    return out


def chunk_document(parsed: Any) -> list[Chunk]:
    """把 :class:`ParsedDocument` 转成材料 chunk（供材料检索与条款定位）。"""
    return chunk_contract(
        parsed.text,
        title=parsed.filename,
        source_path=parsed.source_path,
        doc_type=parsed.doc_type,
    )
