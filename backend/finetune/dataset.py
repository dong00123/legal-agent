"""从真实任务产出里构造 SFT 数据集。

用法：
    python -m backend.finetune.dataset --days 90 --out data/finetune/sft.jsonl
    python -m backend.finetune.dataset --days 90 --dry-run      # 只出体检报告

流水线：抽取 → 清洗 → 去重 → 脱敏 → 质量审查 → Instruction 构造 → 落盘。

**每一步都会淘汰样本，且必须把淘汰原因记下来。** 只报"产出 N 条"是不够的：
如果某天产出突然从 800 掉到 20，唯一的线索就是"是哪一步在大量淘汰"。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("legal_agent.finetune.dataset")

# --------------------------------------------------------------------------- 判据
# 报告太短的样本教不出结构（多是"没查到"之类的应答案）
MIN_REPORT_CHARS = 200
# 太长的大概率是把整篇法规贴了进来，学它只会让模型变啰嗦
MAX_REPORT_CHARS = 8000
# 引用校验没过的样本**不能**进训练集：它示范的是"引了一条对不上的法条"
BAD_CITATION_CHECK = {"failed"}
# 这些字样说明流程没跑完或走了兜底，不是可以给模型学的示范
DEGRADED_MARKS = ("未找到相关", "检索无结果", "无法回答", "暂不支持",
                  "占位符", "TODO", "{{", "联系管理员")
# 中文姓名/身份证等脱敏残留 —— 有一个就整条剔除
RESIDUE_HINT = ("疑似身份证", "疑似手机号", "疑似未脱敏姓名")

SYSTEM_PROMPT = (
    "你是一名中国执业律师的助理。回答必须：1) 只依据已检索到的法条与案例，"
    "不得臆造条文；2) 每条结论后标注依据（法规名 + 条文号）；"
    "3) 查不到依据时明确说「未检索到」，不要含糊其辞；"
    "4) 输出为专业但可交付给当事人的中文。"
)

INSTRUCTION_TPL = (
    "请根据中国现行法律回答下面的问题。要求：结论先行，随后给出依据"
    "（法规名 + 条文号），最后给出可执行建议。"
)


@dataclass
class Sample:
    """一条训练样本。**一定带着出处**（``task_id``）。"""

    instruction: str
    input: str
    output: str
    system: str = SYSTEM_PROMPT
    task_id: str = ""
    intent: str = ""
    citation_count: int = 0
    history: list[str] = field(default_factory=list)

    def to_record(self) -> dict[str, Any]:
        return {
            "instruction": self.instruction,
            "input": self.input,
            "output": self.output,
            "system": self.system,
            # 下面三个**不是** LLaMA-Factory 的字段，训练时会忽略；
            # 留着是为了审核：拿到一条样本就能回到原始任务核对。
            "_task_id": self.task_id,
            "_intent": self.intent,
            "_citation_count": self.citation_count,
        }


@dataclass
class BuildReport:
    """每一步淘汰了多少、为什么。**数字对不上时这是唯一的线索。**"""

    scanned: int = 0
    kept: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    samples: list[Sample] = field(default_factory=list)

    def drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "kept": self.kept,
            "dropped_total": sum(self.dropped.values()),
            "dropped_by_reason": dict(sorted(self.dropped.items(),
                                             key=lambda kv: -kv[1])),
            "note": "dropped 的键是淘汰原因；某一步突然变多就是数据质量出问题的信号",
        }


# --------------------------------------------------------------------------- 抽取
def load_tasks(*, days: int = 90, limit: int = 2000) -> list[dict[str, Any]]:
    """取最近 ``days`` 天**已经产出**的任务。

    ``degraded`` 也算合格样本：它表示"带着缺口声明交付"，
    正是我们希望模型学会的行为（**承认查不到**，而不是编一条）。
    反过来 ``failed`` 一律不要 —— 那里的报告往往只写了一半。
    """
    from sqlalchemy import select

    from backend.app.db.models.task import TaskRecord
    from backend.app.db.session import session_scope

    since = datetime.now() - timedelta(days=max(0, int(days)))
    with session_scope() as db:
        rows = db.execute(
            select(TaskRecord)
            .where(TaskRecord.created_at >= since)
            .where(TaskRecord.status.in_(["succeeded", "degraded"]))
            .order_by(TaskRecord.id.desc())
            .limit(max(1, int(limit)))
        ).scalars().all()
        return [
            {
                "task_id": r.task_id,
                "query": r.query or "",
                "intent": r.intent or "",
                "status": r.status,
                "result": r.result or {},
            }
            for r in rows
        ]


def _is_production_task(task_id: str) -> bool:
    """排除测试夹具造的任务。**与 evals 的判据保持一致。**

    测试任务的 query 往往是"测试""冒烟"这类，教给模型的是废话；
    而且它们占了库里一半以上，不排除的话统计出来的"淘汰率"根本没法看。
    """
    from backend.evals.runner import is_production_task

    return is_production_task(task_id)


# --------------------------------------------------------------------------- 各步
def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _hash(text: str) -> str:
    return hashlib.sha1(_norm(text).encode("utf-8")).hexdigest()


def clean(row: dict[str, Any], report: BuildReport) -> tuple[str, str] | None:
    """清洗：拿 query 与 report，并做最基本的合格性判断。"""
    query = _norm(str(row.get("query") or ""))
    res = row.get("result") or {}
    body = _norm(str(res.get("report") or ""))
    if len(query) < 4:
        report.drop("问题太短")
        return None
    if len(body) < MIN_REPORT_CHARS:
        report.drop(f"报告短于 {MIN_REPORT_CHARS} 字")
        return None
    if len(body) > MAX_REPORT_CHARS:
        report.drop(f"报告长于 {MAX_REPORT_CHARS} 字（多半是整篇法规，学了会啰嗦）")
        return None
    if any(m in body for m in DEGRADED_MARKS):
        report.drop("报告含兜底/未完成字样")
        return None
    return query, body


def check_citations(row: dict[str, Any], report: BuildReport) -> list[dict[str, Any]]:
    """**引用是这个系统的一切。** 没有引用或引用没过校验的一律剔除。"""
    res = row.get("result") or {}
    cites = list(res.get("citations") or [])
    if not cites:
        report.drop("没有引用")
        return []
    if str(res.get("citation_check") or "") in BAD_CITATION_CHECK:
        report.drop("引用校验未通过")
        return []
    return cites


def sanitize_pair(query: str, body: str, report: BuildReport) -> tuple[str, str] | None:
    """脱敏。**有残留就整条剔除** —— 不做"部分脱敏"。

    一个漏掉的真名会被写进模型参数，之后没法删除；
    而少一条样本只是少一点数据。这个取舍没有第二种答案。
    """
    from backend.memory.sanitizer import find_residue, sanitize

    q = sanitize(query, label="query")
    b = sanitize(body, label="report")
    residue = find_residue(q) + find_residue(b)
    if residue:
        report.drop(f"脱敏残留（{residue[0]}）")
        return None
    return q, b


def build(*, days: int = 90, limit: int = 2000,
          dedupe: bool = True) -> tuple[list[Sample], BuildReport]:
    """跑完整条流水线。返回 ``(样本, 报告)``。"""
    from backend.memory.sanitizer import sanitize

    report = BuildReport()
    rows = load_tasks(days=days, limit=limit)
    report.scanned = len(rows)
    seen: set[str] = set()
    out: list[Sample] = []

    for row in rows:
        if not _is_production_task(str(row.get("task_id") or "")):
            report.drop("测试任务")
            continue
        cleaned = clean(row, report)
        if cleaned is None:
            continue
        query, body = cleaned
        cites = check_citations(row, report)
        if not cites:
            continue
        if dedupe:
            # 按「问题 + 报告」一起判重：只看问题的会把同一问题在不同日期
            # 的优质回答当成重复丢掉；只看报告会漏掉批量导入的相同文本。
            key = _hash(query) + _hash(body)
            if key in seen:
                report.drop("重复样本")
                continue
            seen.add(key)
        pair = sanitize_pair(query, body, report)
        if pair is None:
            continue
        q, b = pair
        out.append(Sample(
            instruction=INSTRUCTION_TPL,
            input=q,
            output=_attach_citations(b, cites),
            task_id=str(row.get("task_id") or ""),
            intent=str(row.get("intent") or ""),
            citation_count=len(cites),
        ))

    report.kept = len(out)
    report.samples = out
    return out, report


def _attach_citations(body: str, cites: list[dict[str, Any]]) -> str:
    """把引用清单附在正文之后。

    **为什么要附。** 模型要学的不是"给个结论"，而是"结论 + 依据"这个整体形态。
    只给正文的话，它学会的是像律师那样说话，但不一定会给依据 ——
    而依据恰恰是这个系统对律师唯一不可替代的部分。
    """
    lines = [body, "", "【引用依据】"]
    for c in cites[:12]:
        if isinstance(c, dict):
            name = str(c.get("doc_name") or c.get("name") or "")
            art = str(c.get("article_no") or "")
            status = str(c.get("status") or "")
            lines.append(f"- {name}{(' ' + art) if art else ''}"
                         f"{('（' + status + '）') if status else ''}")
        else:
            lines.append(f"- {c}")
    return "\n".join(lines)


def write_jsonl(samples: Iterable[Sample], path: str | Path) -> Path:
    """写 JSONL。**一个样本一行**（LLaMA-Factory 的要求）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with p.open("w", encoding="utf-8") as fh:
        for s in samples:
            fh.write(json.dumps(s.to_record(), ensure_ascii=False) + "\n")
            n += 1
    logger.info("已写 %d 条样本 -> %s", n, p)
    return p


def render_report(report: BuildReport) -> str:
    """把体检报告渲染成中文。**淘汰原因必须按数量排序列出来。**"""
    lines = [
        "================ 微调数据集体检 ================",
        f"扫描任务 {report.scanned} 条，产出样本 {report.kept} 条，"
        f"淘汰 {sum(report.dropped.values())} 条",
        "",
        "淘汰原因（按数量排序）：",
    ]
    for reason, n in sorted(report.dropped.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {n:>5}  {reason}")
    if report.kept == 0:
        lines += [
            "",
            "**一条都没产出。** 按上面的淘汰原因从多往少查：",
            "  · 「没有引用」最多 —— 说明这个库的回答普遍没带依据，"
            "那现在还不适合微调，先修引用链路；",
            "  · 「脱敏残留」最多 —— 说明脱敏规则没覆盖这类文本，"
            "去 backend/memory/sanitizer.py 补规则，不要放宽这里的判据。",
        ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构造 LoRA 微调数据集")
    parser.add_argument("--days", type=int, default=90, help="取最近多少天的任务")
    parser.add_argument("--limit", type=int, default=2000)
    parser.add_argument("--out", default="data/finetune/sft.jsonl")
    parser.add_argument("--report-out", default="", help="体检报告落盘路径（可选）")
    parser.add_argument("--dry-run", action="store_true", help="只出报告，不写 JSONL")
    parser.add_argument("--no-dedupe", action="store_true", help="不去重（排查重复率时用）")
    args = parser.parse_args(argv)

    from backend.app.logging_conf import setup_logging

    setup_logging("INFO")

    samples, report = build(days=args.days, limit=args.limit,
                            dedupe=not args.no_dedupe)
    text = render_report(report)
    print(text)
    if args.report_out:
        Path(args.report_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report_out).write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    if args.dry_run:
        print("\n--dry-run：未写 JSONL")
        return 0
    p = write_jsonl(samples, args.out)
    print(f"\n样本已写入：{p}")
    print("**下一步是律师审核**，不是直接开训：见 backend/finetune/REVIEW_CHECKLIST.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
