"""表格结构化解析。

法律文档里的表格承载关键要素：费用清单、还款计划、股权结构、租赁物清单、工资表……
必须单独结构化而不是当正文文本流，否则「金额/期限」这类要素会丢失或串行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MONEY_RE = re.compile(r"[¥￥]?\s*([0-9][0-9,，]*(?:\.[0-9]{1,2})?)\s*(?:元|万元|万)?")
DATE_CELL_RE = re.compile(r"(?:(?:19|20)\d{2})\s*[年\-/.]\s*\d{1,2}\s*[月\-/.]\s*\d{1,2}\s*日?")

# 表格类型识别关键词
TABLE_KINDS: list[tuple[str, tuple[str, ...]]] = [
    ("repayment_plan", ("还款", "期次", "本金", "利息", "余额", "贷款")),
    ("fee_schedule", ("费用", "金额", "单价", "数量", "合计", "税费")),
    ("equity_structure", ("股东", "出资", "持股", "股权比例", "认缴")),
    ("lease_object", ("租赁物", "面积", "用途", "坐落", "房号")),
    ("payroll", ("工资", "基本工资", "岗位", "补贴", "应发", "实发")),
    ("work_experience", ("起止时间", "工作单位", "职务", "入职", "离职")),
    ("evidence_list", ("证据名称", "证明目的", "页码", "来源")),
]


@dataclass
class TableParseResult:
    kind: str = "generic"
    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)
    semantic: str = ""
    issues: list[str] = field(default_factory=list)


def _clean_cell(cell: str) -> str:
    return re.sub(r"\s+", " ", (cell or "").replace("\u3000", " ")).strip()


def _is_header_like(row: list[str]) -> bool:
    non_empty = [c for c in row if c]
    if len(non_empty) < 2:
        return False
    numeric = sum(1 for c in non_empty if MONEY_RE.fullmatch(c) or c.replace(".", "").isdigit())
    texty = sum(1 for c in non_empty if 2 <= len(c) <= 12 and not c.isdigit())
    return numeric == 0 and texty >= max(2, len(non_empty) // 2)


def parse_table(rows: list[list[str]], placeholder: str = "") -> TableParseResult:
    """把原始单元格矩阵解析成结构化记录 + 语义描述。"""
    result = TableParseResult()
    if not rows:
        result.issues.append("空表格")
        return result

    cleaned = [[_clean_cell(c) for c in row] for row in rows]
    cleaned = [r for r in cleaned if any(r)]
    if not cleaned:
        result.issues.append("表格无有效单元格")
        return result

    # 表头：首行像表头就用首行，否则用「列1/列2...」占位
    if _is_header_like(cleaned[0]):
        headers = [c or f"列{i + 1}" for i, c in enumerate(cleaned[0])]
        body = cleaned[1:]
    else:
        width = max(len(r) for r in cleaned)
        headers = [f"列{i + 1}" for i in range(width)]
        body = cleaned

    result.headers = headers
    result.rows = body

    # 类型识别
    joined_head = " ".join(headers)
    for kind, keys in TABLE_KINDS:
        if any(k in joined_head for k in keys):
            result.kind = kind
            break

    records: list[dict[str, Any]] = []
    for row in body:
        rec: dict[str, Any] = {}
        for i, h in enumerate(headers):
            val = row[i] if i < len(row) else ""
            if not val:
                continue
            num = _to_number(val)
            rec[h] = num if num is not None else val
        if rec:
            records.append(rec)
    result.records = records

    result.semantic = describe_table(result, placeholder)
    return result


def _to_number(text: str) -> float | None:
    m = MONEY_RE.fullmatch(text.strip())
    if not m:
        return None
    raw = m.group(1).replace(",", "").replace("，", "")
    try:
        val = float(raw)
    except ValueError:
        return None
    if "万" in text:
        val *= 10000
    return val


KIND_LABEL = {
    "repayment_plan": "还款计划",
    "fee_schedule": "费用清单",
    "equity_structure": "股权结构",
    "lease_object": "租赁物清单",
    "payroll": "工资明细",
    "work_experience": "工作经历",
    "evidence_list": "证据清单",
    "generic": "表格",
}


def describe_table(result: TableParseResult, placeholder: str = "") -> str:
    """生成用于回插的语义描述（人读与检索都能用）。"""
    label = KIND_LABEL.get(result.kind, "表格")
    parts = [f"{label}，共 {len(result.records)} 行"]
    if result.headers:
        parts.append("字段：" + "、".join(result.headers[:10]))
    for rec in result.records[:3]:
        kv = "，".join(f"{k}={v}" for k, v in list(rec.items())[:6])
        parts.append(kv)
    text = "；".join(parts)
    return text[:600]


def summarize_for_contract(result: TableParseResult) -> str:
    """给合同审查用的表格摘要（只保留关键数值）。"""
    if result.kind == "fee_schedule":
        total_keys = [h for h in result.headers if "合计" in h or "总额" in h or "金额" in h]
        if total_keys:
            vals = [rec.get(total_keys[0]) for rec in result.records if rec.get(total_keys[0])]
            if vals:
                return f"费用合计约 {max(vals):,.2f} 元"
    if result.kind == "lease_object":
        return f"租赁物共 {len(result.records)} 项"
    return ""
