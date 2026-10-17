"""语料入库编排：把 ``data/raw`` 下的真实材料灌进 Milvus + BM25 + MySQL。

用法：
    python -m backend.corpus.ingest --all
    python -m backend.corpus.ingest --statutes --cases
    python -m backend.corpus.ingest --all --force

三类语料：
- **法规库**（statute）：法规 PDF/Word → 按条切分 → 连带效力状态一起入库；
- **类案库**（case）：法条—案例对照型材料 → 结构化案例 → 入库；
- **知识库**（knowledge）：律所审查清单/办案指引 + 文书模板 → 入库。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.app.config import PROJECT_ROOT, settings
from backend.app.db.models import KnowledgeDoc, StatuteDoc
from backend.app.db.session import init_db, session_scope
from backend.parser.pipeline import parse_document
from backend.parser.structure import check_statute_status, parse_statute_meta
from backend.rag.chunker import chunk_statute
from backend.rag.pipeline import index_chunks, remove_source

logger = logging.getLogger("legal_agent.corpus.ingest")

RAW_DIR = PROJECT_ROOT / "data" / "raw"
CASE_PDF_HINT = "案例"


@dataclass
class IngestReport:
    statutes: int = 0
    statute_articles: int = 0
    cases: int = 0
    knowledge: int = 0
    chunks: int = 0
    vector_written: int = 0
    bm25_written: int = 0
    skipped: int = 0
    failed: list[str] = field(default_factory=list)
    details: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "statutes": self.statutes,
            "statute_articles": self.statute_articles,
            "cases": self.cases,
            "knowledge": self.knowledge,
            "chunks": self.chunks,
            "vector_written": self.vector_written,
            "bm25_written": self.bm25_written,
            "skipped": self.skipped,
            "failed": self.failed,
            "details": self.details[:50],
        }

    def summary(self) -> str:
        return (
            f"法规 {self.statutes} 部/{self.statute_articles} 条｜类案 {self.cases} 个｜"
            f"知识文档 {self.knowledge} 个｜chunks {self.chunks}"
            f"（向量 {self.vector_written} / BM25 {self.bm25_written}）｜跳过 {self.skipped}｜失败 {len(self.failed)}"
        )


def _is_case_corpus(path: Path) -> bool:
    return CASE_PDF_HINT in path.name and path.suffix.lower() == ".pdf"


def discover_statute_files() -> list[Path]:
    out: list[Path] = []
    template_dir = Path(settings.RAW_DIR) / "劳动法" / "文书模板"
    for p in sorted(Path(settings.RAW_DIR).rglob("*")):
        if p.suffix.lower() not in (".pdf", ".docx", ".doc"):
            continue
        if template_dir in p.parents:
            continue
        if _is_case_corpus(p):
            continue
        out.append(p)
    return out


async def ingest_statutes(force: bool = False) -> IngestReport:
    """法规入库。"""
    report = IngestReport()
    files = discover_statute_files()
    logger.info("发现法规文件 %d 个", len(files))
    if not files:
        return report

    with session_scope() as db:
        indexed_names = {
            row.doc_name
            for row in db.query(StatuteDoc).filter(StatuteDoc.indexed.is_(True)).all()
        }

    for path in files:
        try:
            parsed = await parse_document(path, do_vision=False)
            if parsed.doc_type != "statute":
                # 非法规内容（如律所自编材料）跳过，交由其它通道处理
                logger.info("跳过非法规文件：%s（判定为 %s）", path.name, parsed.doc_type)
                report.skipped += 1
                continue

            meta = parse_statute_meta(parsed.text, path.name)
            status = check_statute_status(meta)

            if meta.doc_name in indexed_names and not force:
                report.skipped += 1
                logger.info("已入库，跳过：%s", meta.doc_name)
                continue

            if force:
                await remove_source("statute", str(path))

            chunks = chunk_statute(
                parsed.text,
                doc_name=meta.doc_name,
                source_path=str(path),
                category=meta.category,
                level=meta.level,
                publisher=meta.publisher,
                publish_date=meta.publish_date.isoformat() if meta.publish_date else "",
                effective_date=meta.effective_date.isoformat() if meta.effective_date else "",
                status=status,
                clauses=[c.__dict__ for c in parsed.clauses],
            )
            res = await index_chunks(chunks, "statute")
            report.chunks += res.chunks
            report.vector_written += res.vector_written
            report.bm25_written += res.bm25_written
            report.statutes += 1
            report.statute_articles += len(parsed.clauses)
            report.details.append(
                {
                    "corpus": "statute",
                    "file": path.name,
                    "doc_name": meta.doc_name,
                    "level": meta.level,
                    "category": meta.category,
                    "status": status,
                    "articles": len(parsed.clauses),
                    "chunks": res.chunks,
                    "degraded": res.degraded,
                }
            )

            _upsert_statute_record(meta, status, path, len(parsed.clauses))
        except Exception as exc:  # noqa: BLE001
            report.failed.append(f"{path.name}: {exc}")
            logger.warning("法规入库失败 %s：%s", path.name, exc)
    return report


def _upsert_statute_record(meta, status: str, path: Path, article_count: int) -> None:
    with session_scope() as db:
        row = db.query(StatuteDoc).filter(StatuteDoc.doc_name == meta.doc_name).one_or_none()
        payload = dict(
            doc_no=meta.doc_no,
            category=meta.category,
            level=meta.level,
            publisher=meta.publisher,
            publish_date=meta.publish_date.isoformat() if meta.publish_date else "",
            effective_date=meta.effective_date.isoformat() if meta.effective_date else "",
            status=status,
            stored_path=str(path),
            article_count=article_count,
            indexed=True,
        )
        if row is None:
            db.add(StatuteDoc(doc_name=meta.doc_name, **payload))
        else:
            for k, v in payload.items():
                setattr(row, k, v)


async def ingest_cases(force: bool = False) -> IngestReport:
    """类案入库。"""
    from backend.corpus.case_pdf import (
        cases_to_chunks,
        merge_duplicate_cases,
        parse_case_pdf,
    )

    report = IngestReport()
    files = [p for p in sorted(Path(settings.RAW_DIR).rglob("*.pdf")) if _is_case_corpus(p)]
    logger.info("发现类案文件 %d 个", len(files))

    for path in files:
        try:
            if force:
                await remove_source("case", str(path))
            parsed = parse_case_pdf(path)
            # 按法条分册的语料里同一判例会重复收录，先归并再切块，
            # 否则同一个判例会占掉类案检索 top-k 的多个名额（详见 merge_duplicate_cases）
            cases = merge_duplicate_cases(parsed)
            chunks = cases_to_chunks(cases)
            res = await index_chunks(chunks, "case")
            report.cases += len(cases)
            report.chunks += res.chunks
            report.vector_written += res.vector_written
            report.bm25_written += res.bm25_written
            report.details.append(
                {
                    "corpus": "case",
                    "file": path.name,
                    "cases": len(cases),
                    "parsed_cases": len(parsed),
                    "chunks": res.chunks,
                    "degraded": res.degraded,
                }
            )
        except Exception as exc:  # noqa: BLE001
            report.failed.append(f"{path.name}: {exc}")
            logger.warning("类案入库失败 %s：%s", path.name, exc)
    return report


def _template_dir() -> Path:
    d = Path(settings.RAW_DIR) / "劳动法" / "文书模板"
    return d


async def ingest_knowledge(force: bool = False) -> IngestReport:
    """知识库入库：内置审查清单/办案指引 + 文书模板。"""
    from backend.corpus import guidelines
    from backend.corpus.templates import load_templates, templates_to_chunks

    report = IngestReport()
    guidelines.materialize()

    # 1) 内置指引
    builtin_chunks = guidelines.to_chunks()
    if builtin_chunks:
        if force:
            for src in guidelines.SOURCES:
                await remove_source("knowledge", f"builtin://{src.title}")
        res = await index_chunks(builtin_chunks, "knowledge")
        report.chunks += res.chunks
        report.vector_written += res.vector_written
        report.bm25_written += res.bm25_written
        report.knowledge += len(guidelines.SOURCES)
        report.details.append({"corpus": "knowledge", "source": "builtin", "docs": len(guidelines.SOURCES), "chunks": res.chunks})
        _upsert_knowledge_records(guidelines.SOURCES)

    # 2) 律所自己的知识库目录（data/knowledge/*.md）
    extra_dir = Path(settings.KNOWLEDGE_DIR)
    for md in sorted(extra_dir.glob("*.md")):
        if any(md.stem == s.title for s in guidelines.SOURCES):
            continue
        text = md.read_text(encoding="utf-8")
        from backend.rag.chunker import chunk_knowledge

        chunks = chunk_knowledge(
            text, title=md.stem, doc_type="guideline", version="v1.0",
            category="律所知识库", source_path=str(md),
        )
        res = await index_chunks(chunks, "knowledge")
        report.chunks += res.chunks
        report.vector_written += res.vector_written
        report.bm25_written += res.bm25_written
        report.knowledge += 1

    # 3) 文书模板
    templates = load_templates(_template_dir())
    if templates:
        if force:
            for t in templates:
                await remove_source("knowledge", t.source_path)
        t_chunks = templates_to_chunks(templates)
        res = await index_chunks(t_chunks, "knowledge")
        report.chunks += res.chunks
        report.vector_written += res.vector_written
        report.bm25_written += res.bm25_written
        report.knowledge += len(templates)
        report.details.append(
            {"corpus": "knowledge", "source": "templates", "docs": len(templates), "chunks": res.chunks}
        )
        _upsert_template_records(templates)
    return report


def _upsert_knowledge_records(sources) -> None:
    with session_scope() as db:
        for src in sources:
            row = db.query(KnowledgeDoc).filter(KnowledgeDoc.title == src.title).one_or_none()
            payload = dict(
                doc_type=src.doc_type,
                category=src.category,
                version=src.version,
                effective_date=src.effective_date,
                status="active",
                content=src.content,
                indexed=True,
            )
            if row is None:
                db.add(KnowledgeDoc(title=src.title, **payload))
            else:
                for k, v in payload.items():
                    setattr(row, k, v)


def _upsert_template_records(templates) -> None:
    with session_scope() as db:
        for t in templates:
            row = db.query(KnowledgeDoc).filter(KnowledgeDoc.title == t.title).one_or_none()
            payload = dict(
                doc_type="template",
                category=f"文书模板-{t.region or '通用'}",
                version=t.version,
                stored_path=t.source_path,
                content=t.text[:20000],
                indexed=True,
            )
            if row is None:
                db.add(KnowledgeDoc(title=t.title, **payload))
            else:
                for k, v in payload.items():
                    setattr(row, k, v)


async def ingest_all(force: bool = False, statutes: bool = True, cases: bool = True, knowledge: bool = True) -> IngestReport:
    init_db()
    total = IngestReport()
    parts: list[tuple[str, IngestReport]] = []
    if statutes:
        parts.append(("statute", await ingest_statutes(force)))
    if cases:
        parts.append(("case", await ingest_cases(force)))
    if knowledge:
        parts.append(("knowledge", await ingest_knowledge(force)))

    for name, r in parts:
        total.statutes += r.statutes
        total.statute_articles += r.statute_articles
        total.cases += r.cases
        total.knowledge += r.knowledge
        total.chunks += r.chunks
        total.vector_written += r.vector_written
        total.bm25_written += r.bm25_written
        total.skipped += r.skipped
        total.failed.extend(r.failed)
        total.details.extend([{**d, "stage": name} for d in r.details])
    await _invalidate_query_cache(total)
    return total


async def _invalidate_query_cache(report: IngestReport) -> None:
    """语料库发生变化后，作废相似问题缓存（qcache）。

    语义缓存按"问题向量相似度"复用旧结论；语料更新（新增/重灌法条、类案、
    知识库）后若不失效，同样的问法会直接命中**旧数据时代**的结论，
    新入库内容永远检索不到 —— 表现为"数据加了，回答还是老样子"。
    缓存只是优化，可重建（结论本体仍在 la_task），清空是安全的。
    """
    if report.chunks <= 0:
        return
    try:
        from backend.app.services.query_cache import purge as purge_query_cache

        result = await purge_query_cache()
        if result.get("purged"):
            logger.info("语料已更新，清空相似问题缓存（qcache），旧结论不再复用")
        else:
            logger.warning("清空相似问题缓存失败：%s", result.get("error"))
    except Exception as exc:  # noqa: BLE001
        # 缓存失效失败不能中断入库：它只影响"下次是否复用旧结论"
        logger.warning("清空相似问题缓存异常（不影响本次入库）：%s", exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="语料入库：法规库 / 类案库 / 知识库")
    parser.add_argument("--all", action="store_true", help="全量入库")
    parser.add_argument("--statutes", action="store_true", help="仅法规库")
    parser.add_argument("--cases", action="store_true", help="仅类案库")
    parser.add_argument("--knowledge", action="store_true", help="仅知识库")
    parser.add_argument("--force", action="store_true", help="强制重建（先删后写）")
    args = parser.parse_args(argv)

    from backend.app.logging_conf import setup_logging

    setup_logging("INFO")

    if not any([args.all, args.statutes, args.cases, args.knowledge]):
        args.all = True

    report = asyncio.run(
        ingest_all(
            force=args.force,
            statutes=args.all or args.statutes,
            cases=args.all or args.cases,
            knowledge=args.all or args.knowledge,
        )
    )
    print("\n===== 入库结果 =====")
    print(report.summary())
    if report.failed:
        print("失败明细：")
        for f in report.failed[:20]:
            print("  -", f)
    return 0 if not report.failed else 1


if __name__ == "__main__":
    sys.exit(main())
