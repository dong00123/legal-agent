"""材料类 MCP Tool：结构化解析 / OCR 与要素提取 / 多文档交叉比对。"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from backend.parser.pipeline import parse_document
from backend.tools.base import ToolResult

logger = logging.getLogger("legal_agent.tools.material")

MONEY_RE = re.compile(r"[¥￥]?\s*([0-9][0-9,，]*(?:\.[0-9]{1,2})?)\s*(万元|元)")
DATE_RE = re.compile(r"((?:19|20)\d{2})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*日?")
TERM_RE = re.compile(r"(?:期限|租期|合同期)[^。\n]{0,10}?(\d+)\s*(年|个月|月)")


async def material_parse(path: str, *, do_vision: bool | None = None) -> ToolResult:
    """合同/文书结构化解析：条款拆分 + 要素提取。

    输出同时落盘到 ``data/processed/``，供后续审查与条款定位复用。
    """
    p = Path(path)
    if not p.exists():
        return ToolResult(ok=False, tool="material_parse", error=f"文件不存在：{path}",
                          degraded=True, fallback_note="材料解析不可用")

    parsed = await parse_document(p, do_vision=do_vision)
    # 落盘**已经在管线里做掉了**（``pipeline.save_parsed``，2026-10-12 收拢到这里）：
    # 解析路由与微信上传同样要读这份产物，各写一遍必然分叉。
    # 这里只把结果取出来 —— 顺带把路径带进 tool 的 payload（Agent 的观察里要它）。
    payload = parsed.to_dict()
    payload["parsed_path"] = parsed.parsed_path

    degraded = bool(parsed.evidence_gaps)
    return ToolResult(
        ok=True,
        tool="material_parse",
        data=payload,
        degraded=degraded,
        fallback_note="；".join(g.get("reason", "") for g in parsed.evidence_gaps) if degraded else "",
    )


async def material_ocr(path: str, *, hint: str = "") -> ToolResult:
    """扫描件 OCR 与要素提取。

    文本图层可直接抽取时走普通解析；无文本图层（纯扫描）时把页面渲染为图片后
    交给 VLM 识别，并把识别结论回插到原位置。
    """
    p = Path(path)
    if not p.exists():
        return ToolResult(ok=False, tool="material_ocr", error=f"文件不存在：{path}",
                          degraded=True, fallback_note="OCR 不可用")

    parsed = await parse_document(p, do_vision=True, hint=hint or p.name)
    gaps = [g for g in parsed.evidence_gaps if g.get("kind") == "vlm"]
    return ToolResult(
        ok=True,
        tool="material_ocr",
        data={
            "doc_type": parsed.doc_type,
            "pages": parsed.pages,
            "text_length": len(parsed.text),
            "placeholders": parsed.blocks,
            "parties": [pp.__dict__ for pp in parsed.parties],
            "vlm_available": not bool(gaps),
            "text": parsed.text[:8000],
        },
        degraded=bool(gaps),
        fallback_note=gaps[0]["reason"] if gaps else "",
    )


async def material_cross_compare(paths: list[str]) -> ToolResult:
    """多文档交叉比对（合同 vs 补充协议 vs 函件）。

    比对维度：主体名称、金额、期限、关键日期。任何维度出现不一致都单列出来，
    避免律师在数十页材料里靠肉眼找矛盾。
    """
    if not paths:
        return ToolResult(ok=False, tool="material_cross_compare", error="未提供待比对文件")

    docs: list[dict[str, Any]] = []
    for path in paths:
        p = Path(path)
        if not p.exists():
            continue
        parsed = await parse_document(p, do_vision=False)
        docs.append(
            {
                "file": p.name,
                "path": str(p),
                "text": parsed.text,
                "parties": [pp.__dict__ for pp in parsed.parties],
                "clauses": [c.__dict__ for c in parsed.clauses],
                "tables": parsed.tables,
            }
        )
    if len(docs) < 2:
        return ToolResult(
            ok=False, tool="material_cross_compare",
            error="有效文件不足 2 份，无法交叉比对",
            degraded=True, fallback_note="比对材料不足",
        )

    dims = {
        "主体": _extract_dim(docs, lambda t: [f"{p['role']}={p['name']}" for p in _parties(t)]),
        "金额": _extract_dim(docs, lambda t: sorted({f"{m[0]}{m[1]}" for m in MONEY_RE.findall(t)})),
        "日期": _extract_dim(docs, lambda t: sorted({f"{y}-{m:0>2}-{d:0>2}" for y, m, d in DATE_RE.findall(t)})),
        "期限": _extract_dim(docs, lambda t: sorted({f"{n}{u}" for n, u in TERM_RE.findall(t)})),
    }

    conflicts: list[dict[str, Any]] = []
    for dim, values in dims.items():
        non_empty = {k: v for k, v in values.items() if v}
        if len(non_empty) < 2:
            continue
        sets = {k: set(v) for k, v in non_empty.items()}
        all_vals = set().union(*sets.values())
        common = set.intersection(*sets.values()) if sets else set()
        diff = sorted(all_vals - common)
        if diff:
            conflicts.append({"dimension": dim, "differences": diff, "by_file": non_empty})

    return ToolResult(
        ok=True,
        tool="material_cross_compare",
        data={
            "files": [d["file"] for d in docs],
            "dimensions": dims,
            "conflicts": conflicts,
            "conclusion": (
                f"共比对 {len(docs)} 份文件，发现 {len(conflicts)} 个维度存在不一致，"
                "需律师核实是否存在补充/变更约定或文本笔误。"
                if conflicts
                else f"共比对 {len(docs)} 份文件，主体/金额/期限/日期维度未发现明显冲突。"
            ),
        },
        degraded=False,
    )


def _parties(text: str) -> list[dict[str, str]]:
    from backend.parser.structure import extract_parties

    return [p.__dict__ for p in extract_parties(text)]


def _extract_dim(docs: list[dict[str, Any]], extractor) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for d in docs:
        try:
            out[d["file"]] = list(extractor(d["text"]))
        except Exception:  # noqa: BLE001
            out[d["file"]] = []
    return out
