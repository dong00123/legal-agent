"""端到端全链路冒烟：**用一份真实的劳动法 PDF** 跑通
解析 → 检索 → Agent 编排 → 文书起草 → 引用校验。

和另外两个脚本的分工：

- ``smoke_services.py`` —— 直接连数据库**在进程内**跑服务层，验代码逻辑；
- ``healthcheck.py``   —— 走 HTTP 验"部署出来的进程活着"；
- **本脚本**           —— 走**完整业务链路**，验的是"这五步串起来能出东西"。

前两个都绿、这个红是完全可能的：它们都不关心"检索出来的法条和报告里
引用的法条是不是同一批"。

用法（项目根目录）：
    python scripts/e2e_chain.py
    python scripts/e2e_chain.py --pdf data/raw/劳动法/中华人民共和国劳动法.pdf
    python scripts/e2e_chain.py --query "未签书面劳动合同的二倍工资怎么算"

跑完会给出**前端展示页的直达链接**（任务详情 / 文书草稿），
因为链路跑通的判据不是"脚本没报错"，而是"律师能在界面上看到它"。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.config import settings  # noqa: E402
from backend.app.db.session import init_db  # noqa: E402

DEFAULT_PDF = "data/raw/劳动法/中华人民共和国劳动法.pdf"
DEFAULT_QUERY = "用人单位未与劳动者签订书面劳动合同，劳动者可以主张什么？"
# 前端地址。**默认 8001** 而不是读 settings.PORT（那个是 8000，
# 与实际启动端口不一致，拼出来的链接打不开）。
DEFAULT_BASE = "http://127.0.0.1:8001"

FAILED: list[str] = []


def step(no: str, title: str) -> None:
    print(f"\n{'=' * 66}\n[{no}] {title}\n{'=' * 66}")


def check(label: str, cond: bool, detail: str = "", fail_hint: str = "") -> bool:
    """打一行断言结果。

    **``detail`` 与 ``fail_hint`` 必须分开。** 前者是描述现状的中性信息
    （"8737 字"），成功失败都该看；后者是失败原因，只在失败时打印。

    曾经只有一个 ``extra`` 参数，措辞用的是"前 5 条里没出现劳动/合同/工资/用工"
    —— 于是成功时打出来的也长这样：

        OK   命中内容与问题相关  [前 5 条里没出现劳动/合同/工资/用工]

    通过的项带着一句"没出现"，比不打还糟：看日志的人会以为断言写反了。
    """
    suffix = f"  [{detail}]" if detail else ""
    if not cond and fail_hint:
        suffix = f"  ← {fail_hint}"
    print(f"  {'OK  ' if cond else 'FAIL'} {label}{suffix}")
    if not cond:
        FAILED.append(label)
    return cond


# --------------------------------------------------------------------------- 各步
async def stage_parse(pdf: Path) -> tuple[str, int]:
    """1) 解析：PDF → 文本 + 条款。"""
    from backend.parser.pipeline import parse_document

    doc = await parse_document(pdf)
    text = str(getattr(doc, "text", "") or "")
    clauses = getattr(doc, "clauses", None) or []
    check("PDF 解析出正文", len(text) > 500, f"{len(text)} 字")
    check("切出条款", len(clauses) > 0, f"{len(clauses)} 条")
    print(f"  文件：{pdf.name}")
    print(f"  正文前 80 字：{text[:80].replace(chr(10), ' ')}")
    return text, len(clauses)


async def stage_retrieve(query: str) -> list[dict[str, Any]]:
    """2) 检索：语义 + BM25 混合。"""
    from backend.tools.registry import call_tool

    res = await call_tool("statute_search", query=query, top_k=5, task_id="e2e_chain")
    statutes = list((res.data or {}).get("statutes") or [])
    check("法规检索有命中", len(statutes) > 0, f"{len(statutes)} 条")
    for i, s in enumerate(statutes[:3], 1):
        print(f"    {i}. {s.get('doc_name')} {s.get('article_no')} "
              f"（{s.get('status') or '效力未知'}）")
    # **命中不能只是"有"，还得对得上问题。** 只查条数的话，
    # 检索退化为随便返回几条也能全绿。
    joined = " ".join(str(s.get("text") or "") for s in statutes[:5])
    check("命中内容与问题相关", any(k in joined for k in ("劳动", "合同", "工资", "用工")),
          fail_hint="前 5 条里没出现劳动/合同/工资/用工")
    return statutes


async def stage_agent(query: str) -> dict[str, Any]:
    """3) 编排：走完整 Agent 图。"""
    from backend.app.services.orchestrator import RunRequest, orchestrator

    req = RunRequest(query=query, doc_type="consult", client="e2e",
                     username="e2e_chain", intent="consult")
    outcome = await orchestrator.execute(req)
    body = outcome.to_dict(with_trace=False)
    # ``ok`` 的定义本来就是 ``not error``（见 orchestrator.RunOutcome 的构造），
    # 所以不用再 ``or not error`` 一次 —— 那样写看着更宽容，其实完全等价。
    check("流程跑完", outcome.ok, f"ok={outcome.ok} error={str(outcome.error or '')[:60]}")
    check("产出报告", len(body.get("report") or "") > 100,
          f"{len(body.get('report') or '')} 字")
    check("走完了若干节点", int(body.get("steps") or 0) > 0, f"{body.get('steps')} 步")
    print(f"  意图：{body.get('intent')}　路线：{' → '.join(body.get('route') or [])}")
    counts = body.get("counts") or {}
    print(f"  计数：{json.dumps(counts, ensure_ascii=False)}")
    # **``counts.clauses`` 与第 1 步的"切出条款"不是一回事。** 前者统计的是
    # 本次**案件材料**里的条款数，后者是那份法规 PDF 自己的条款数。
    # 纯咨询（没上传材料）时它就是 0 —— 两个数字摆在一起，
    # 不解释的话很容易被读成"解析白做了"。
    if not counts.get("clauses"):
        print("        （clauses=0：本次是纯咨询，没有案件材料可切条款；"
              "第 1 步的条款数来自法规 PDF 本身）")
    return body


def stage_citations(body: dict[str, Any]) -> None:
    """4) 校验：引用能不能回溯到检索证据。**这是全链路里最关键的一步。**"""
    cites = body.get("citation_summary") or {}
    check_state = str(body.get("citation_check") or "pending")
    check("终核验引用", check_state != "pending", f"citation_check={check_state}")
    check("引用校验通过", check_state in ("passed", "warn"), check_state)
    print(f"  引用统计：{json.dumps(cites, ensure_ascii=False)[:200]}")
    gaps = body.get("evidence_gap") or []
    if gaps:
        print(f"  证据缺口 {len(gaps)} 项（**有缺口是诚实的表现，不是失败**）：")
        for g in gaps[:3]:
            print(f"    · {str(g)[:90]}")


def stage_draft(body: dict[str, Any]) -> None:
    """5) 文书：链路终点要能落地成一份可编辑的草稿。"""
    from backend.app.services import draft_service

    rows = draft_service.list_drafts(limit=5)
    items = rows.get("items") if isinstance(rows, dict) else rows
    check("草稿库可查", items is not None, f"{len(items or [])} 份")
    if items:
        top = items[0]
        print(f"  最新草稿：{top.get('title')}　v{top.get('version')}　"
              f"{top.get('content_chars')} 字")
        # **列表接口不带正文是刻意的**（见 draft_service.draft_to_dict）——
        # 这里断言它不含 content，防止有人哪天"顺手"加回去。
        check("列表不泄露正文", "content" not in top, fail_hint="列表带上了全文")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="端到端全链路冒烟（真实劳动法 PDF）")
    parser.add_argument("--pdf", default=DEFAULT_PDF)
    parser.add_argument("--query", default=DEFAULT_QUERY)
    parser.add_argument("--no-parse", action="store_true", help="跳过解析（PDF 很大时）")
    parser.add_argument("--base", default=DEFAULT_BASE, help="前端地址，用于拼展示页链接")
    args = parser.parse_args(argv)
    global _BASE
    _BASE = args.base.rstrip("/")

    from backend.app.logging_conf import setup_logging

    setup_logging("WARNING")   # 只要结论，不要逐节点的 INFO

    init_db()
    pdf = Path(args.pdf)
    started = time.time()

    async def run() -> tuple[str, int]:
        text, n = ("", 0)
        step("1/5", "解析 PDF → 正文 + 条款")
        if not args.no_parse:
            if not pdf.exists():
                print(f"!! 找不到 PDF：{pdf}")
                FAILED.append("PDF 存在")
            else:
                text, n = await stage_parse(pdf)
        step("2/5", "混合检索（语义 + BM25）")
        await stage_retrieve(args.query)
        step("3/5", "Agent 编排（完整节点图）")
        body = await stage_agent(args.query)
        # 后续的校验与文书两步要用编排产物；asyncio.run 的内部变量出不来，
        # 所以显式带出来（比再跑一次编排便宜得多）。
        _LAST_BODY["body"] = body
        return body.get("task_id") or "", len(text)

    task_id, _ = asyncio.run(run())
    body = _LAST_BODY.get("body") or {}
    if body:
        step("4/5", "引用校验（能否回溯到检索证据）")
        stage_citations(body)
        step("5/5", "文书草稿落地")
        stage_draft(body)

    print(f"\n{'=' * 66}")
    print(f"用时 {time.time() - started:.1f}s")
    if task_id:
        print("\n前端展示页（后端起来后可直接打开）：")
        print(f"  任务详情：{_ui('/tasks/' + task_id)}")
        print(f"  文书草稿：{_ui('/drafts')}")
        print(f"  能力体检：{_ui('/capabilities')}")
    if FAILED:
        print(f"\n❌ 未通过 {len(FAILED)} 项：")
        for f in FAILED:
            print(f"   · {f}")
        return 1
    print("\n✅ 全链路跑通（解析 → 检索 → 编排 → 校验 → 文书）")
    return 0


_LAST_BODY: dict[str, Any] = {}
_BASE = DEFAULT_BASE


def _ui(hash_path: str) -> str:
    """前端直达链接。

    **端口不从 ``settings.PORT`` 取。** 那个值是应用内部默认（8000），
    而实际端口是启动 uvicorn 时命令行给的（本项目用 8001）。
    按配置拼会得到一个打不开的链接 —— 而它看起来完全合理，
    足够让人怀疑是前端坏了而不是端口写错了。
    """
    return f"{_BASE}/ui/#{hash_path}"


if __name__ == "__main__":
    raise SystemExit(main())
