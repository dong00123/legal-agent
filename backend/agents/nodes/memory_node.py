"""MemoryNode：分层记忆落库（方案 4.1 Memory Agent / 第七章）。

原文约束：「案件档案摘要、历史办案经验的存取与脱敏；**脱敏后入库，权限隔离**」。

本节点是最"安静"的一个 —— 它没有产出物，只有副作用。所以它的设计重点全在
**什么时候不该写**：

1. **失败或未过终审的任务不写长期档案**。一份引用都被拦下来的分析结论，
   进了案件档案就会在下次办案时被当成"历史经验"检索出来，错误被二次传播。
   所以只在 ``citation_check == "passed"`` 时写案件档案；否则只写一条
   审计级记录（"本次任务因引用未通过而未归档"）。
2. **脱敏由长期记忆层强制完成，本节点不提供开关**。
   ``MemoryLongTerm.upsert_case`` 内部无条件调用脱敏器（见 memory/long_term.py
   的写入纪律说明），本节点不参与也不干预，避免"为了调试方便关掉脱敏"。
3. **写入失败不影响主流程**。归档是副产物，不该让它把办案流程搞崩。
"""

from __future__ import annotations

import logging
from typing import Any

from backend.agents.harness import Harness
from backend.agents.state import LegalState

logger = logging.getLogger("legal_agent.nodes.memory")

AGENT = "memory"


async def memory_node(state: LegalState, harness: Harness) -> LegalState:
    """归档案件档案与办案经验。"""
    harness.step("MemoryNode")

    check = state.get("citation_check") or "pending"
    if check != "passed":
        # 不写长期档案，但要让 Trace 与审计都说清楚为什么
        reason = {
            "failed": "引用终审未通过",
            "pending": "引用终审未执行",
        }.get(check, f"引用状态为 {check}")
        harness.tracer.warn("MemoryNode", "skip_archive",
                            f"{reason}，本次任务不写入案件档案（避免错误结论被历史检索复用）")
        await _record_skip(state, harness, reason)
        harness.snapshot()
        return state

    # ---- 案件档案 + 经验沉淀（脱敏由长期记忆层强制完成，本节点无从绕过）
    try:
        digest = await harness.memory.remember(state) or {}
    except Exception as exc:  # noqa: BLE001 - 归档失败不能中断办案
        harness.tracer.warn("MemoryNode", "archive_failed", f"记忆写入失败：{exc}")
        harness.gaps("memory", f"案件档案归档失败（{exc}）",
                     "本次办案经验未沉淀，不影响本次结论")
        harness.snapshot()
        return state

    lt = digest.get("written_long_term") or []
    sm = digest.get("written_semantic") or []
    rejected = digest.get("rejected") or []

    harness.tracer.info("MemoryNode", "archived",
                        f"长期档案 {len(lt)} 条、语义记忆 {len(sm)} 条已脱敏写入；"
                        f"拒收 {len(rejected)} 条")

    if not lt and not sm:
        # 没写进去不一定是错：闲聊/内容过短会被质量判断拒收，这是设计行为。
        # 但必须让律师看到"这次没有沉淀经验"，避免以为历史里已经有记录。
        harness.gaps("memory", "本次任务未沉淀任何可复用的办案记忆",
                     "下次同类案件不会从本次获得经验参考；若预期应沉淀，请检查输入是否过短")
        harness.tracer.warn("MemoryNode", "nothing_written",
                            f"全部 {digest.get('candidates') or 0} 条候选记忆未通过质量判断")

    # 拒收理由逐条记 Trace：记错了比没记住更糟，所以拒收原因必须可查
    for r in rejected[:8]:
        harness.tracer.info("MemoryNode", "rejected",
                            f"未入库（{r.get('reason') or '质量判断未通过'}）："
                            f"{(r.get('text') or '')[:60]}")

    _record_progress(state, harness)

    harness.snapshot()
    return state


async def _record_skip(state: LegalState, harness: Harness, reason: str) -> None:
    """记录"未归档"这一事实本身。

    为什么要专门记一笔：如果什么都不写，事后追查"这个案子为什么没有历史记录"
    时无从判断是"没跑归档节点"还是"跑了但被拒绝了"。审计的价值在于能区分
    "没做"与"做了但结果是否"。
    """
    try:
        harness.ctx.audit("memory.archive_skipped", resource="case",
                          resource_id=state.get("case_id") or state.get("task_id") or "",
                          result="skipped", detail=reason)
    except Exception as exc:  # noqa: BLE001
        logger.debug("审计写入失败（不影响主流程）：%s", exc)


def _record_progress(state: LegalState, harness: Harness) -> None:
    """关联案件时写一条进度流水（业务类工具，非幂等，失败只告警）。"""
    case_id = state.get("case_id") or ""
    if not case_id:
        return
    import asyncio

    from backend.tools.business_tools import case_progress_record

    async def _write() -> None:
        try:
            await case_progress_record(
                case_id,
                stage="分析完成",
                note=f"意图={state.get('intent')} 引用终审={state.get('citation_check')} "
                     f"置信度={state.get('confidence')}",
                operator=harness.ctx.username or "",
                task_id=state.get("task_id") or "",
                result=f"证据 {len(state.get('statutes') or [])} 法条 / "
                       f"{len(state.get('cases') or [])} 类案；"
                       f"缺口 {len(state.get('evidence_gap') or [])} 项",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("进度写入失败（不影响主流程）：%s", exc)

    try:
        asyncio.get_running_loop().create_task(_write())
    except RuntimeError:  # pragma: no cover - 无事件循环
        logger.debug("无事件循环，跳过进度写入")


def memory_digest(state: LegalState) -> dict[str, Any]:
    return {
        "case_id": state.get("case_id") or "",
        "archived": (state.get("citation_check") or "") == "passed",
    }
