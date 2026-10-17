"""离线评测运行器：把指标算出来、写成报告。

**评测对象是"跑过的真实任务"，不是造出来的样本。**
本项目的质量指标几乎都能从 ``la_task`` 读出来 —— 引用过没过终审、
有没有声明证据缺口、各节点花了多久、哪个工具在失败。
造样本集当然也能算，但造出来的样本会随着代码演进而失真，
而历史任务是每天都在自然积累的。

两条刻意的设计：

1. **报告同时给人看和给机器看。** ``render()`` 出中文文本（贴进周报、
   给律师看），``run()`` 同时写一份 JSON（给流水线、给趋势图）。
   只给 JSON 会没人看，只给文本会让"对比上周"变成手工活。
2. **指标为 ``None`` 时打印"样本不足"，不打印 0。**
   0 条引用时"可采信率"不是 0%，是"无从判断"。打印 0 会让人以为引用全被拦了。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from backend.app.services import knowledge_service

from . import metrics as M

logger = logging.getLogger("legal_agent.evals")

# 生产路径生成的任务编号形如 ``task_`` + 12 位十六进制（见 ``state._uid``）。
# **用它区分"真实任务"与"测试/演示造出来的任务"**，而不是维护一份测试编号黑名单 ——
# 黑名单每加一个测试就得同步改一次，而漏掉的那个会让失败率虚高，
# 于是"本周失败率 34%"里的绝大多数其实来自测试夹具。
# 本机实测：155 条任务里 53 条 failed，其中绝大部分是测试留下的。
PRODUCTION_TASK_RE = re.compile(r"^task_[0-9a-f]{12}$")


def is_production_task(task_id: str) -> bool:
    return bool(PRODUCTION_TASK_RE.match(task_id or ""))


# --------------------------------------------------------------------------- 取数
def collect(*, days: int = 7, limit: int = 500, statuses: tuple[str, ...] = (),
            include_nonstandard: bool = False) -> tuple[list[M.TaskEval], int]:
    """读最近 ``days`` 天的任务。返回 ``(记录, 被排除的非标准任务数)``。

    两条过滤，都**报出被排除了多少**（不静默丢）：

    1. **默认排除仍在跑的任务**（``pending``/``running``）：它们没有结果，
       算进去只会把各项比率的分母撑大、把数字拉低，而那是「还没跑完」不是「质量差」。
    2. **默认排除非标准编号的任务**（测试与演示造的）。它们会被记成 failed 或
       degraded，把失败率整体抬高 —— 本机实测 155 条里 53 条 failed，
       绝大多数来自测试夹具。让这批数据混进来，"质量在变差"就成了假信号。
    """
    from sqlalchemy import select

    from backend.app.db.models.task import TaskRecord
    from backend.app.db.session import session_scope

    since = datetime.now() - timedelta(days=max(0, int(days)))
    try:
        with session_scope() as db:
            stmt = select(TaskRecord).where(TaskRecord.created_at >= since)
            if statuses:
                stmt = stmt.where(TaskRecord.status.in_(list(statuses)))
            else:
                stmt = stmt.where(TaskRecord.status.notin_(["pending", "running"]))
            rows = db.execute(
                stmt.order_by(TaskRecord.id.desc()).limit(max(1, int(limit)))
            ).scalars().all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("评测取数失败：%s: %s", type(exc).__name__, exc)
        return [], 0

    kept: list[M.TaskEval] = []
    excluded = 0
    for r in rows:
        if not include_nonstandard and not is_production_task(r.task_id):
            excluded += 1
            continue
        kept.append(M.task_eval_from_row(r))
    return kept, excluded


# --------------------------------------------------------------------------- 汇总
def summarize(records: list[M.TaskEval], *, excluded_nonstandard: int = 0) -> dict[str, Any]:
    """算全部指标。**结构稳定** —— 趋势图按固定键读，不要随意改名。"""
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "sample": {
            "tasks": len(records),
            "excluded_nonstandard": excluded_nonstandard,
            "with_citations": sum(1 for r in records if r.citations),
            "total_citations": sum(len(r.citations) for r in records),
            "total_tool_calls": sum(len(r.tool_calls) for r in records),
            "total_trace_events": sum(len(r.trace) for r in records),
        },
        "citation": {
            "trust_rate": M.as_dict(M.citation_trust(records)),
            "block_rate": M.as_dict(M.citation_block(records)),
            "validator_ran_rate": M.as_dict(M.citation_checked_rate(records)),
        },
        "gaps": {
            "rate": M.as_dict(M.gap_rate(records)),
            "by_kind": M.gap_by_kind(records),
        },
        "outcome": {
            "status": M.status_distribution(records),
            "degraded_rate": M.as_dict(M.degraded_rate(records)),
            "failure_rate": M.as_dict(M.failure_rate(records)),
            "confidence": M.confidence_stats(records),
            "steps": M.step_stats(records),
        },
        "latency": {
            "by_node": M.node_latency(records),
            "task_total_ms": _task_total(records),
        },
        "tools": M.tool_failures(records),
        "corpus": corpus_quality(),
    }


def _task_total(records: list[M.TaskEval]) -> dict[str, Any]:
    vals = sorted(int(r.elapsed_ms or 0) for r in records if int(r.elapsed_ms or 0) > 0)
    if not vals:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
    n = len(vals)
    return {
        "count": n,
        "p50_ms": vals[n // 2],
        "p95_ms": vals[min(n - 1, int(n * 0.95))],
        "max_ms": vals[-1],
    }


def corpus_quality() -> dict[str, Any]:
    """语料体检。**这一块不依赖任务历史**，它查的是"库里有没有不该缺的东西"。

    最需要看的是 ``empty_docs``：那些登记为「现行有效」却一条条文都没有的法规。
    检索永远查不到它们，而律师看到的是「没有相关规定」 —— 与「压根没入库」
    在结果上无法区分，而这两件事的后果完全不同。
    """
    try:
        stats = knowledge_service.statute_stats()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    empty = stats.get("empty_docs") or []
    return {
        "statutes": stats.get("statutes"),
        "articles": stats.get("articles"),
        "indexed": stats.get("indexed"),
        "by_status": stats.get("by_status"),
        "empty_docs": empty,
        "empty_docs_count": len(empty),
        "note": stats.get("empty_docs_note", ""),
    }


# --------------------------------------------------------------------------- 渲染
def _degraded_reasons() -> list[str]:
    """当前环境里**真实**处于降级的子系统（与 ``/health`` 的 degraded 同源）。

    为什么评测报告要自己列一遍，而不是写"去看 /health"：
    降级率接近 100% 是这个报告能给出的最强告警，而它最有价值的部分
    ——**到底哪一项不在**——如果让人另开一个页面去查，大概率就没人查了。
    两者都读 ``settings.runtime_backends()``，所以不会说法不一致。
    """
    try:
        from backend.app.config import settings
    except Exception:  # noqa: BLE001
        return []
    try:
        backends = settings.runtime_backends()
    except Exception:  # noqa: BLE001
        return []
    return [f"{name} 已关闭，回落：{info.get('fallback')}"
            for name, info in backends.items() if not info.get("enabled")]


def _rate_line(label: str, rate: dict[str, Any], unit: str = "条") -> str:
    value = rate.get("value")
    if value is None:
        return f"  {label:<22} 样本不足（分母 0 {unit}）"
    return (f"  {label:<22} {value:.1%}  "
            f"（{rate.get('hit')}/{rate.get('total')} {unit}）")


def render(report: dict[str, Any]) -> str:
    """把汇总渲染成中文文本报告。"""
    lines: list[str] = []
    add = lines.append

    add("=" * 68)
    add("法律智能体 · 离线评测报告")
    add(f"生成时间 {report.get('generated_at')}")
    add("=" * 68)

    s = report.get("sample") or {}
    add("\n【样本】")
    add(f"  任务 {s.get('tasks')} 条，其中带引用 {s.get('with_citations')} 条；"
        f"引用共 {s.get('total_citations')} 条")
    add(f"  工具调用 {s.get('total_tool_calls')} 次；Trace 事件 {s.get('total_trace_events')} 条")
    if s.get("excluded_nonstandard"):
        add(f"  已排除 {s['excluded_nonstandard']} 条非标准编号的任务（测试与演示造的）。")
        add("  口径：真实任务编号形如 task_ + 12 位十六进制；测试夹具的编号不符合这个形状。")
        add("        不排除的话失败率会明显虚高 —— 实测那批里失败占多数。")
    if not s.get("tasks"):
        add("  **本窗口没有可评测的真实任务** —— 下面的比率都会显示「样本不足」，不是质量问题")

    add("\n【引用质量】这是最重要的一项：报告里的法条经没经过终审核实")
    cite = report.get("citation") or {}
    add(_rate_line("引用可采信率", cite.get("trust_rate") or {}))
    add(_rate_line("引用拦截率", cite.get("block_rate") or {}))
    add(_rate_line("终审执行率", cite.get("validator_ran_rate") or {}, "条任务"))
    add("  说明：拦截率**突然变成 0 更值得警惕** —— 配合上一行的终审执行率一起看，")
    add("        执行率也低就说明终审压根没跑，而不是引用都干净。")

    add("\n【证据缺口】")
    gaps = report.get("gaps") or {}
    add(_rate_line("有缺口的任务占比", gaps.get("rate") or {}, "条任务"))
    by_kind = gaps.get("by_kind") or {}
    add("  按类型：" + ("、".join(f"{k} {v} 条" for k, v in by_kind.items()) or "（无）"))
    add("  说明：**不是越低越好。** 缺口是「这几条我没查到证据」的诚实声明；")
    add("        复杂合同审查零缺口反而可疑。按类型分开看才能定位：")
    add("        tool 类扎堆=某工具在挂（修系统）；document 类扎堆=材料不齐（找当事人要）。")

    add("\n【任务结局】")
    out = report.get("outcome") or {}
    add("  状态分布：" + ("、".join(f"{k} {v}" for k, v in (out.get("status") or {}).items()) or "（无）"))
    add(_rate_line("降级完成占比", out.get("degraded_rate") or {}, "条任务"))
    add(_rate_line("失败占比", out.get("failure_rate") or {}, "条任务"))
    add("  说明：降级完成是**可用交付物**（带着缺口声明），不与失败混算。")
    deg = (out.get("degraded_rate") or {}).get("value")
    if deg is not None and deg >= 0.9:
        # 这一条是给"全绿但其实是残的"准备的：降级率接近 100% 时，
        # 表面上"零失败"，实际是某个子系统长期不可用，每次任务都少了一段能力。
        add("  **降级率接近 100% 值得当故障查。** 它通常意味着有子系统长期不可用。")
        # **直接把降级项列出来，而不是让读者自己去查。**
        # 猜一个原因（比如"LLM 回落"）比不写更糟：它会把人引向错误方向，
        # 而这条信息就在手边 —— /health 的 degraded 与这里同源于
        # settings.runtime_backends()，离线的评测进程一样读得到。
        for item in _degraded_reasons():
            add(f"        · {item}")
        if not _degraded_reasons():
            add("        （当前没有子系统报降级 —— 那要看任务自身的缺口声明）")
    conf = out.get("confidence") or {}
    if conf.get("count"):
        add(f"  置信度：均值 {conf['mean']}，中位 {conf['p50']}，范围 {conf['min']}~{conf['max']}")
        add("        只看均值会漏掉「一半很高一半很低」这种双峰。")
    steps = out.get("steps") or {}
    if steps.get("count"):
        add(f"  步数：均值 {steps['mean']}，中位 {steps['p50']}，最大 {steps['max']}")
        # **只在真的逼近上限时才告警。** 无条件把这句话贴上去，
        # 会在"最大 9 步、上限 16"时也喊一次 —— 喊多了就没人看了，
        # 而真正逼近上限的那一周反而会被忽略。
        try:
            from backend.app.config import settings as _s

            if _s.MAX_STEPS > 0 and steps["max"] >= _s.MAX_STEPS * 0.8:
                add(f"        **最大步数已达上限 {_s.MAX_STEPS} 的 "
                    f"{steps['max'] / _s.MAX_STEPS:.0%}** —— 有任务在「回环差点没跑完」边缘，")
                add("        它不表现为失败，只表现为结果偶尔变浅，建议调高 MAX_STEPS 或收紧回环条件。")
        except Exception:  # noqa: BLE001
            pass

    add("\n【耗时】")
    lat = (report.get("latency") or {}).get("by_node") or []
    total = (report.get("latency") or {}).get("task_total_ms") or {}
    if total.get("p50_ms") is not None:
        add(f"  整任务：中位 {total['p50_ms'] / 1000:.1f}s，"
            f"p95 {total['p95_ms'] / 1000:.1f}s，最长 {total['max_ms'] / 1000:.1f}s")
    if lat:
        add(f"  {'节点':<20} {'次数':>5} {'p50':>9} {'p95':>9} {'最长':>9}")
        for row in lat[:12]:
            add(f"  {row['node']:<20} {row['calls']:>5} "
                f"{row['p50_ms']:>7} ms {row['p95_ms']:>7} ms {row['max_ms']:>7} ms")
        add("  排序口径是**单次 p50** 而不是总耗时：总耗时会放大调用次数多的检索节点，")
        add("  而排查性能问题要看的是单次有多慢。")
        add("  取值口径是「任何带 elapsed_ms 的 Trace 事件」—— 项目的 trace 里")
        add("  step 事件的 elapsed_ms 恒为 0，真耗时在 tool:<name> 事件上。")
    else:
        add("  （Trace 里没有任何带 elapsed_ms 的事件）")

    add("\n【工具】按失败率排序（混在一起算会被高频工具稀释）")
    tools = report.get("tools") or []
    if tools:
        add(f"  {'工具':<26} {'调用':>5} {'失败':>5} {'降级':>5} {'失败率':>8}")
        for row in tools[:15]:
            rate = "—" if row["fail_rate"] is None else f"{row['fail_rate']:.0%}"
            flag = "  <= 需关注" if (row["fail_rate"] or 0) > 0.3 else ""
            add(f"  {row['tool']:<26} {row['calls']:>5} {row['failed']:>5} "
                f"{row['degraded']:>5} {rate:>8}{flag}")
    else:
        add("  （没有工具调用记录）")

    add("\n【语料体检】")
    corp = report.get("corpus") or {}
    if corp.get("error"):
        add(f"  查询失败：{corp['error']}")
    else:
        add(f"  法规 {corp.get('statutes')} 部，条文 {corp.get('articles')} 条，"
            f"已索引 {corp.get('indexed')} 部")
        empty = corp.get("empty_docs") or []
        if empty:
            add(f"  **{len(empty)} 部法规登记为有效却没有条文 —— 检索永远查不到它们：**")
            for d in empty[:10]:
                add(f"    · {d['doc_name']}（{d['status']}，indexed={d['indexed']}）")
            if len(empty) > 10:
                add(f"    …… 另有 {len(empty) - 10} 部")
            add("  这类「查不到」与「这部法规不适用」在结果上无法区分，")
            add("  律师会据此得出「没有相关规定」，比报错危险。修法：后台「法规库 → 重新入库」。")
        else:
            add("  没有空条文法规。")

    add("\n" + "=" * 68)
    return "\n".join(lines)


# --------------------------------------------------------------------------- 入口
def run(*, days: int = 7, limit: int = 500, out: str | Path | None = None,
        write_json: bool = True, include_nonstandard: bool = False
        ) -> tuple[dict[str, Any], str]:
    """取数 → 汇总 → 渲染 →（可选）落盘。返回 ``(report, 文本)``。"""
    records, excluded = collect(days=days, limit=limit,
                                include_nonstandard=include_nonstandard)
    report = summarize(records, excluded_nonstandard=excluded)
    report["sample"]["days"] = days
    text = render(report)

    if write_json and out:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
        text += f"\n（JSON 已写入 {path}）"
    return report, text
