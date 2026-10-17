"""业务类 MCP Tool：案件进度记录 / 审查意见清单导出。

方案第五章业务类工具，对应 FR：与案件管理系统对接、审查意见可导出交付客户。

设计边界（重要）：
- 这两个工具**不做推理**，只做持久化与格式转换；
- 写入类操作 ``idempotent=False``，由 ToolExecutor 保证不重试，避免重复执行；
- 导出走"服务端生成文件 + 返回路径/下载 token"的方式，不把大段 Word 二进制塞进
  Agent 状态里（否则 State 会被撑爆，LangGraph 每步都要序列化整个 State）。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.app.config import settings
from backend.app.db.models import AuditLog, Case, TaskRecord
from backend.app.db.session import session_scope
from backend.app.services.case_service import STAGES as _KNOWN_STAGES
from backend.tools.base import ToolResult

logger = logging.getLogger("legal_agent.tools.business")

# ---------------------------------------------------------------- 案件进度记录

# 定义在 ``case_service.STAGES``，这里只是别名。``la_case.stage`` 存的是**英文枚举**，
# 而下面 ``PROGRESS_STAGES`` 是中文的办理环节清单 —— 两者不是一套词汇，
# 混用就是脏数据与"人工推进被冲掉"的来源，详见 ``case_progress_record``。
PROGRESS_STAGES = (
    "咨询",
    "材料接收",
    "法律检索",
    "事实与争点梳理",
    "策略形成",
    "文书起草",
    "提交法院",
    "庭审",
    "结案",
)


async def case_progress_record(
    case_id: str,
    *,
    stage: str = "",
    note: str = "",
    operator: str = "",
    task_id: str = "",
    result: str = "",
) -> ToolResult:
    """记录案件办理进度（与案件管理系统对接）。

    Args:
        case_id: 案件编号。
        stage: 办理阶段，需为 PROGRESS_STAGES 之一（不匹配时按"其他"记录，不丢数据）。
        note: 进度说明。
        operator: 承办律师（用户标识）。
        task_id: 关联任务 ID，用于把 Agent 任务与案件进度串起来。
        result: 阶段性结果（如"已提交立案"、"调解结案"）。
    """
    if not case_id:
        return ToolResult(ok=False, tool="case_progress_record", error="缺少 case_id")

    normalized_stage = stage if stage in PROGRESS_STAGES else (stage or "其他")
    stage_warning = ""
    if stage and stage not in PROGRESS_STAGES:
        stage_warning = f"阶段「{stage}」不在标准阶段清单中，已按原值记录，请确认是否需要规范化"

    payload = {
        "stage": normalized_stage,
        "note": note,
        "operator": operator,
        "task_id": task_id,
        "result": result,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
    }

    with session_scope() as db:
        case = db.query(Case).filter(Case.case_id == case_id).one_or_none()
        if case is None:
            return ToolResult(
                ok=False,
                tool="case_progress_record",
                error=f"案件 {case_id} 不存在",
                degraded=True,
                fallback_note="案件不存在，进度未记录",
            )

        # 进度写进 case.progress（JSON 列表），并把状态同步到 case.status
        history = list(case.progress or [])
        history.append(payload)
        case.progress = history
        # ⚠ **只有合法阶段才准写 case.stage**。
        #
        # ``PROGRESS_STAGES`` 是**中文**的办理环节清单（咨询/材料接收/…/庭审/结案），
        # 而 ``la_case.stage`` 用的是英文枚举（见 ``case_service.STAGES``）。
        # 这里原先是无条件 ``case.stage = normalized_stage``，于是：
        #   1. 中文值直接落进 stage 列 —— 库里那两行「分析完成」就是这么来的，
        #      前端 ``STAGE_LABEL[c.stage] || c.stage`` 认不出，列表里原样吐中文；
        #   2. 更要命的是它会把**人工推进的阶段冲掉** —— 律师在详情页点了
        #      「推进到开庭审理」（stage=hearing），下一次 AI 分析跑这个工具，
        #      阶段又被冲成一个中文环节名，「开庭审理阶段」那张卡跟着掉回去。
        #
        # 进度流水照记不误（``case.progress`` 是给人看的，中文没有错），
        # 只是不再拿它去覆写枚举列。
        if normalized_stage in _KNOWN_STAGES:
            case.stage = normalized_stage
        else:
            logger.warning(
                "案件 %s 的进度阶段「%s」不是合法阶段枚举，只记流水、不改案件阶段",
                case_id, normalized_stage,
            )
        case.status = _status_of_stage(normalized_stage)
        if result:
            case.outcome = result
        case.updated_at = datetime.now()

        if task_id:
            task = db.query(TaskRecord).filter(TaskRecord.task_id == task_id).one_or_none()
            if task is not None and not task.case_id:
                task.case_id = case_id

        db.add(
            AuditLog(
                trace_id=task_id,
                task_id=task_id,
                case_id=case_id,
                username=operator or "system",
                action="case_progress_record",
                resource="case",
                resource_id=case_id,
                detail=f"{normalized_stage}｜{note[:200]}",
            )
        )

    return ToolResult(
        ok=True,
        tool="case_progress_record",
        data={
            "case_id": case_id,
            "stage": normalized_stage,
            "status": _status_of_stage(normalized_stage),
            "recorded": True,
            "history_length": len(history),
            "standard_stages": list(PROGRESS_STAGES),
        },
        degraded=bool(stage_warning),
        fallback_note=stage_warning,
    )


def _status_of_stage(stage: str) -> str:
    """阶段 → 案件状态。"""
    if stage == "结案":
        return "closed"
    if stage in ("咨询",):
        return "intake"
    return "in_progress"


# ---------------------------------------------------------------- 审查意见清单导出

EXPORT_TEMPLATES = {
    "review": "合同审查意见清单",
    "analysis": "案件分析意见",
    "statute": "法规检索报告",
}


async def review_checklist_export(
    content: str = "",
    *,
    title: str = "审查意见清单",
    export_type: str = "review",
    case_id: str = "",
    risk_points: list[dict[str, Any]] | None = None,
    missing_clauses: list[Any] | None = None,
    citations: list[dict[str, Any]] | None = None,
    evidence_gap: list[Any] | None = None,
    confidence: float | None = None,
    format: str = "md",
) -> ToolResult:
    """审查意见清单导出（Markdown / HTML，可直接另存为 Word、打印为 PDF）。

    为什么用 md/html 而不是直接生成 .docx：
    服务端不引入 python-docx 之外的重依赖；Markdown 便于 EXE/Web 双端复用渲染，
    律师需要 Word 时用 Word 打开 .md/.html 另存即可，或走 ``format_normalize`` 套模板。
    """
    if not content and not risk_points:
        return ToolResult(
            ok=False, tool="review_checklist_export",
            error="无内容可导出（content 与 risk_points 均为空）",
        )

    export_type = export_type if export_type in EXPORT_TEMPLATES else "review"
    body = content or _render_sections(risk_points or [], missing_clauses, citations, evidence_gap, confidence)

    header = _header(title, export_type, case_id, confidence)
    footer = (
        "\n\n---\n\n"
        "> **声明**：本文件由 AI 辅助生成，仅供律师工作参考，不构成正式法律意见，"
        "不得直接作为对客户的正式交付文件，须经承办律师审阅确认。\n"
    )

    out_dir = Path(settings.EXPORT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_title = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", title)[:40] or "exports"

    warnings: list[str] = []
    written: list[str] = []

    text_md = header + body + footer
    md_path = out_dir / f"{safe_title}_{stamp}.md"
    md_path.write_text(text_md, encoding="utf-8")
    written.append(str(md_path))

    html_path = ""
    if format in ("html", "both", "docx"):
        html_path = str(out_dir / f"{safe_title}_{stamp}.html")
        Path(html_path).write_text(_to_html(title, text_md), encoding="utf-8")
        written.append(html_path)
        if format == "docx":
            warnings.append("未直接生成 .docx：请用 Word 打开导出的 .html 另存为 .docx，或改用 format_normalize 套用模板")

    if not citations:
        warnings.append("导出内容未包含引用清单，交付前请确认是否遗漏法规/类案引用")

    logger.info("审查意见清单已导出：%s", written)

    return ToolResult(
        ok=True,
        tool="review_checklist_export",
        data={
            "title": title,
            "export_type": export_type,
            "case_id": case_id,
            "format": format,
            "files": written,
            "markdown_path": str(md_path),
            "html_path": html_path,
            "bytes": md_path.stat().st_size,
            "sections": {
                "risk_points": len(risk_points or []),
                "missing_clauses": len(missing_clauses or []),
                "citations": len(citations or []),
                "evidence_gap": len(evidence_gap or []),
            },
        },
        degraded=bool(warnings),
        fallback_note="；".join(warnings),
    )


def _header(title: str, export_type: str, case_id: str, confidence: float | None) -> str:
    lines = [
        f"# {title}",
        "",
        f"- 文件类型：{EXPORT_TEMPLATES[export_type]}",
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
    ]
    if case_id:
        lines.append(f"- 案件编号：{case_id}")
    if confidence is not None:
        lines.append(f"- 置信度：{confidence:.2f}")
    lines.append("")
    return "\n".join(lines)


LEVEL_LABEL = {"high": "高", "medium": "中", "low": "低"}


def _render_sections(
    risk_points: list[dict[str, Any]],
    missing_clauses: list[Any] | None,
    citations: list[dict[str, Any]] | None,
    evidence_gap: list[Any] | None,
    confidence: float | None,
) -> str:
    parts: list[str] = []

    if risk_points:
        parts.append("## 一、风险点清单\n")
        parts.append("| 序号 | 条款定位 | 风险等级 | 风险描述 | 修改建议 | 法律依据 |")
        parts.append("|---|---|---|---|---|---|")
        for idx, rp in enumerate(risk_points, 1):
            basis = rp.get("basis") or rp.get("citations") or []
            basis_txt = "；".join(_fmt_basis(b) for b in basis) or "—"
            parts.append(
                "| {i} | {clause} | {level} | {risk} | {sug} | {basis} |".format(
                    i=idx,
                    clause=_cell(rp.get("clause") or rp.get("location") or ""),
                    level=LEVEL_LABEL.get(str(rp.get("level") or "").lower(), _cell(rp.get("level")) or "中"),
                    risk=_cell(rp.get("risk") or rp.get("description")),
                    sug=_cell(rp.get("suggestion") or rp.get("advice")),
                    basis=_cell(basis_txt),
                )
            )
        parts.append("")

    if missing_clauses:
        parts.append("## 二、缺失条款\n")
        for mc in missing_clauses:
            if isinstance(mc, dict):
                name = mc.get("clause") or mc.get("name") or ""
                why = mc.get("reason") or mc.get("suggestion") or ""
                parts.append(f"- **{_cell(name)}**：{_cell(why)}")
            else:
                parts.append(f"- {mc}")
        parts.append("")

    if citations:
        parts.append("## 三、引用清单\n")
        parts.append("| 类型 | 名称 | 条文/案号 | 效力状态 |")
        parts.append("|---|---|---|---|")
        for c in citations:
            parts.append(
                "| {t} | {n} | {a} | {s} |".format(
                    t=_cell(c.get("type")),
                    n=_cell(c.get("name") or c.get("case_name")),
                    a=_cell(c.get("article") or c.get("case_no")),
                    s=_cell(c.get("status")) or "—",
                )
            )
        parts.append("")

    if evidence_gap:
        parts.append("## 四、证据缺口与不确定项\n")
        for g in evidence_gap:
            parts.append(f"- {_cell(g if isinstance(g, str) else g.get('reason') or g)}")
        parts.append("")

    if confidence is not None:
        parts.append(f"## 五、置信度说明\n\n本次结论置信度 **{confidence:.2f}**。"
                     "置信度低于 0.60 时建议律师补充材料后重新分析。\n")

    return "\n".join(parts) if parts else "（无结构化内容）"


def _fmt_basis(b: Any) -> str:
    if isinstance(b, str):
        return b
    if isinstance(b, dict):
        name = b.get("name") or b.get("doc_name") or ""
        art = b.get("article") or b.get("article_no") or ""
        status = b.get("status") or ""
        txt = f"《{name.strip('《》')}》{art}" if name else art
        return f"{txt}（{status}）" if status else txt
    return str(b)


def _cell(value: Any) -> str:
    """Markdown 表格单元格转义：竖线会破坏表格结构，换行会破坏行结构。"""
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\n", "<br>").strip()


def _to_html(title: str, md: str) -> str:
    """极简 Markdown → HTML（只支持本工具生成的结构，不追求通用 Markdown 解析）。"""
    import html as html_mod

    lines = md.split("\n")
    out: list[str] = []
    in_table = False
    for line in lines:
        s = line.rstrip()
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue  # 分隔行
            if not in_table:
                out.append("<table>")
                in_table = True
                out.append("<tr>" + "".join(f"<th>{html_mod.escape(c)}</th>" for c in cells) + "</tr>")
            else:
                out.append("<tr>" + "".join(f"<td>{html_mod.escape(c)}</td>" for c in cells) + "</tr>")
            continue
        if in_table:
            out.append("</table>")
            in_table = False
        if s.startswith("### "):
            out.append(f"<h3>{html_mod.escape(s[4:])}</h3>")
        elif s.startswith("## "):
            out.append(f"<h2>{html_mod.escape(s[3:])}</h2>")
        elif s.startswith("# "):
            out.append(f"<h1>{html_mod.escape(s[2:])}</h1>")
        elif s.startswith("> "):
            out.append(f"<blockquote>{html_mod.escape(s[2:])}</blockquote>")
        elif s.startswith("- "):
            out.append(f"<li>{html_mod.escape(s[2:])}</li>")
        elif s in ("---", "***"):
            out.append("<hr/>")
        elif not s:
            out.append("")
        else:
            out.append(f"<p>{html_mod.escape(s)}</p>")
    if in_table:
        out.append("</table>")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>{html_mod.escape(title)}</title>
<style>
  body {{ font-family: "Microsoft YaHei", "PingFang SC", serif; max-width: 900px;
         margin: 40px auto; padding: 0 24px; line-height: 1.8; color: #1a1a1a; }}
  h1 {{ font-size: 24px; border-bottom: 2px solid #b91c1c; padding-bottom: 8px; }}
  h2 {{ font-size: 18px; margin-top: 28px; color: #b91c1c; }}
  table {{ border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 14px; }}
  th, td {{ border: 1px solid #d4d4d4; padding: 8px 10px; text-align: left; vertical-align: top; }}
  th {{ background: #fafafa; font-weight: 600; }}
  blockquote {{ background: #fef6f6; border-left: 4px solid #b91c1c;
                margin: 20px 0; padding: 10px 16px; color: #7f1d1d; font-size: 13px; }}
  hr {{ border: none; border-top: 1px dashed #d4d4d4; margin: 24px 0; }}
</style>
</head>
<body>
{"".join(out)}
</body>
</html>"""
