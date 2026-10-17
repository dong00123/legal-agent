"""文书类 MCP Tool：草稿生成 / 合同修改对照稿 / 格式规范化。"""

from __future__ import annotations

import logging
import re
from typing import Any

from backend.llm.client import llm_client
from backend.llm.prompts import DRAFT_PROMPT, LEGAL_SYSTEM_PROMPT, build_messages
from backend.tools.base import ToolResult

logger = logging.getLogger("legal_agent.tools.document")

# 文书骨架要求（来自知识库中的《诉讼文书起草规范》）
REQUIRED_SECTIONS = {
    "complaint": ["请求事项", "事实与理由", "法律依据", "此致"],
    "arbitration_application": ["仲裁请求", "事实与理由", "证据", "此致"],
    "defense": ["答辩意见", "事实与理由", "法律依据", "此致"],
    "legal_opinion": ["基本情况", "分析与意见", "结论"],
    "appeal": ["上诉请求", "上诉理由", "此致"],
    "revision": ["修改"],
}

DOC_TYPE_NAMES = {
    "complaint": "民事起诉状",
    "defense": "民事答辩状",
    "arbitration_application": "劳动仲裁申请书",
    "legal_opinion": "法律意见书",
    "appeal": "民事上诉状",
    "revision": "合同修改稿",
}


async def draft_generate(
    doc_type: str = "complaint",
    *,
    facts: list[dict[str, Any]] | None = None,
    issues: list[dict[str, Any]] | None = None,
    statutes: list[dict[str, Any]] | None = None,
    cases: list[dict[str, Any]] | None = None,
    parties: list[dict[str, Any]] | None = None,
    query: str = "",
    templates: list[dict[str, Any]] | None = None,
) -> ToolResult:
    """文书草稿生成：起诉状 / 答辩状 / 法律意见书 / 仲裁申请书。

    引用必须来自检索证据：Prompt 里带着参考资料块，且模板层再次执行
    「无来源不出引用」校验（见 :func:`format_normalize` 与 Validator）。
    """
    from backend.llm.prompts import (
        format_case_evidence,
        format_knowledge_evidence,
        format_statute_evidence,
    )

    context: list[tuple[str, str]] = []
    for i, s in enumerate(statutes or [], start=1):
        context.append(
            format_statute_evidence(i, s.get("doc_name") or "", s.get("article_no") or "",
                                    s.get("status") or "未知", (s.get("text") or "")[:600],
                                    s.get("publisher") or "")
        )
    for i, c in enumerate(cases or [], start=1):
        context.append(
            format_case_evidence(i, c.get("case_no") or "", c.get("court") or "",
                                 str(c.get("judge_date") or ""), c.get("cause_of_action") or "",
                                 (c.get("gist") or c.get("case_name") or "")[:500])
        )
    for i, k in enumerate(templates or [], start=1):
        context.append(
            format_knowledge_evidence(i, k.get("title") or "", k.get("doc_type") or "template",
                                      k.get("version") or "", (k.get("snippet") or "")[:800])
        )

    user_parts = [DRAFT_PROMPT.format(doc_type=DOC_TYPE_NAMES.get(doc_type, "法律文书"))]
    if query:
        user_parts.append(f"【委托需求】{query}")
    if parties:
        user_parts.append("【当事人】" + "；".join(f"{p.get('role') or ''}：{p.get('name') or ''}" for p in parties))
    if facts:
        user_parts.append("【案件事实】\n" + "\n".join(
            f"- {f.get('text') if isinstance(f, dict) else f}" for f in facts[:20]
        ))
    if issues:
        user_parts.append("【争议焦点】\n" + "\n".join(
            f"- {i.get('title') if isinstance(i, dict) else i}" for i in issues[:10]
        ))

    messages = build_messages(LEGAL_SYSTEM_PROMPT, "\n\n".join(user_parts), context)
    result = await llm_client.chat(
        messages,
        task="draft",
        payload={
            "doc_type": doc_type, "facts": facts or [], "issues": issues or [],
            "statutes": statutes or [], "cases": cases or [], "parties": parties or [],
        },
    )

    draft = result.text.strip()
    check = validate_draft_format(draft, doc_type)

    return ToolResult(
        ok=True,
        tool="draft_generate",
        data={
            "doc_type": doc_type,
            "doc_type_name": DOC_TYPE_NAMES.get(doc_type, "法律文书"),
            "draft": draft,
            "format_check": check,
            "provider": result.provider,
            "elapsed_ms": result.elapsed_ms,
        },
        degraded=result.degraded,
        fallback_note="模型服务不可用，草稿由规则引擎按检索证据组装（未做自由生成）" if result.degraded else "",
    )


def validate_draft_format(draft: str, doc_type: str) -> dict[str, Any]:
    """文书格式规范化校验：必备段落 / 引用格式 / 编造嫌疑。"""
    required = REQUIRED_SECTIONS.get(doc_type, [])
    missing = [s for s in required if s not in (draft or "")]

    citations = re.findall(r"《[^》]{2,40}》\s*第\s*[一二三四五六七八九十百零两\d]+\s*条", draft or "")
    bad_citations = re.findall(r"《[^》]{2,40}》\s*第\s*[零一二三四五六七八九十百千两\d]{2,}\s*条", draft or "")
    placeholders = re.findall(r"[（(]此处需补充[^）)]*[）)]", draft or "")

    return {
        "required_sections": required,
        "missing_sections": missing,
        "citation_count": len(citations),
        "citations": sorted(set(citations))[:20],
        "unverified_citation_count": len(bad_citations),
        "placeholder_count": len(placeholders),
        "placeholders": placeholders[:10],
        "passed": not missing,
        "note": "缺失段落需补充；「此处需补充法律依据」占位符表示检索未命中，不得用编造条文填充。"
                if missing or placeholders else "格式检查通过",
    }


async def revision_draft_generate(
    contract_text: str,
    risk_points: list[dict[str, Any]],
    *,
    contract_type: str = "",
) -> ToolResult:
    """合同修改对照稿生成（原文 / 修改后 / 理由）。"""
    lines = [f"# {contract_type or '合同'}修改对照稿", ""]
    lines.append("| # | 条款 | 风险等级 | 原文 | 修改后 | 依据 |")
    lines.append("|---|---|---|---|---|---|")
    level_cn = {"high": "高", "medium": "中", "low": "低"}
    for i, rp in enumerate(risk_points or [], start=1):
        clause = f"{rp.get('clause_no') or ''} {rp.get('clause_title') or ''}".strip() or "全文"
        original = (rp.get("clause_text") or "（缺失该条款）").replace("\n", " ").replace("|", "／")[:160]
        suggest = (rp.get("suggestion") or "").replace("\n", " ").replace("|", "／")[:200]
        basis = "；".join(rp.get("basis") or []) or "（需补充法律依据）"
        lines.append(f"| {i} | {clause} | {level_cn.get(rp.get('level'), rp.get('level'))} | {original} | {suggest} | {basis} |")
    lines.append("")
    lines.append("> 说明：修改建议需结合交易背景与委托人商业诉求最终确认；依据缺失处不得凭猜测补条文。")

    return ToolResult(
        ok=True,
        tool="revision_draft_generate",
        data={
            "contract_type": contract_type,
            "revision_draft": "\n".join(lines),
            "risk_count": len(risk_points or []),
            "unbacked_count": sum(1 for rp in (risk_points or []) if not rp.get("basis")),
        },
    )


async def format_normalize(text: str, *, doc_type: str = "complaint") -> ToolResult:
    """文书格式规范化：按文书骨架校验 + 统一引用格式 + 补免责声明。"""
    from backend.app.config import settings

    check = validate_draft_format(text, doc_type)
    normalized = text or ""

    # 统一法条引用空格：动「第 87 条」→「第八十七条」这类问题不动，只规整空格
    normalized = re.sub(r"《([^》]{2,40})》\s+第", r"《\1》第", normalized)
    normalized = re.sub(r"第\s+([一二三四五六七八九十百零两\d]+)\s+条", r"第\1条", normalized)

    if "仅供律师工作参考" not in normalized:
        normalized = normalized.rstrip() + f"\n\n---\n> {settings.DISCLAIMER}\n"

    return ToolResult(
        ok=True,
        tool="format_normalize",
        data={"text": normalized, "format_check": check},
        degraded=not check["passed"],
        fallback_note="文书缺少必备段落" if not check["passed"] else "",
    )
