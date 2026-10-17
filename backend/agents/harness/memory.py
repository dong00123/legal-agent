"""Harness Memory：把分层记忆接到 Agent 运行链路上。

方案 4.4 节 Harness 的 ``Memory`` 组件。它不重新实现存储，而是负责三件
MemoryFacade 不该管的事：

1. **任务内工具结果缓存**：同一任务里 ``statute_search`` 被不同节点以相同参数
   调用时直接复用短期记忆里的结果。这同时是防死循环第三层的"便宜"实现 ——
   回环时如果参数没变，根本不会再次打库。
2. **按上下文裁剪**：注入 Prompt 的记忆块有长度上限，防止案件档案 +
   历史经验把 Context 挤爆导致检索证据被截断。
3. **审计**：记忆读取也算"访问了案件相关资料"，需要留痕。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from backend.agents.harness.context import RunContext
from backend.memory import memory as memory_facade
from backend.memory.short_term import short_term_memory

logger = logging.getLogger("legal_agent.harness.memory")

# 记忆块长度上限：案件档案与经验各留一段，总量受控，避免挤掉检索证据
MAX_CASE_DIGEST = 2000
MAX_EXPERIENCE = 1500


class HarnessMemory:
    """一次任务运行期间使用的记忆视图。"""

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self.facade = memory_facade
        self.task_id = ctx.task_id
        self._cache_hits = 0
        self._cache_misses = 0

    # ------------------------------------------------------------------ 工具缓存
    @staticmethod
    def cache_key(tool: str, args: dict[str, Any]) -> str:
        """参数指纹：只取影响结果的参数，避免把无关字段（如 agent 名）算进去。"""
        relevant = {k: v for k, v in sorted((args or {}).items())
                    if k not in ("agent", "step", "trace_id")}
        raw = f"{tool}|{relevant}"
        return hashlib.blake2b(raw.encode("utf-8"), digest_size=10).hexdigest()

    def cached_tool(self, tool: str, args: dict[str, Any]) -> Any:
        value = short_term_memory.get_tool_cache(self.task_id, tool, self.cache_key(tool, args))
        if value is None:
            self._cache_misses += 1
            self.ctx.tracer.info("Memory", "cache_miss", f"{tool} 无缓存，将实际执行")
            return None
        self._cache_hits += 1
        self.ctx.tracer.info("Memory", "cache_hit", f"{tool} 命中任务内缓存，跳过重复执行")
        return value

    def cache_tool(self, tool: str, args: dict[str, Any], value: Any) -> None:
        short_term_memory.put_tool_cache(self.task_id, tool, self.cache_key(tool, args), value)

    # ------------------------------------------------------------------ 读记忆
    async def context_for(self, query: str, case_id: str = "") -> dict[str, Any]:
        """读取本次任务的记忆上下文（已裁剪）。"""
        ctx = await self.facade.context_for(query=query, case_id=case_id or self.ctx.case_id)

        if ctx.get("case_digest") and len(ctx["case_digest"]) > MAX_CASE_DIGEST:
            ctx["case_digest"] = ctx["case_digest"][:MAX_CASE_DIGEST] + "\n…（档案过长已截断）"
        if ctx.get("experience") and len(ctx["experience"]) > MAX_EXPERIENCE:
            ctx["experience"] = ctx["experience"][:MAX_EXPERIENCE] + "\n…（经验列表过长已截断）"

        if ctx.get("case_digest"):
            # 读案件档案属于"访问案件资料"，需要留痕
            self.ctx.audit_material(f"case_digest:{case_id or self.ctx.case_id}",
                                    resource="case_archive")
        if ctx.get("sources"):
            self.ctx.tracer.info("Memory", "recall",
                                 f"记忆上下文 {len(ctx['sources'])} 段"
                                 f"（档案 {len(ctx.get('case_digest') or '')} 字，"
                                 f"经验 {len(ctx.get('experience') or '')} 字）")
        else:
            self.ctx.tracer.info("Memory", "recall", "无可用记忆上下文")
        return ctx

    def prompt_block(self, ctx: dict[str, Any]) -> str:
        return self.facade.prompt_block(ctx)

    # ------------------------------------------------------------------ 写记忆
    async def remember(self, state: dict[str, Any]) -> dict[str, Any]:
        """任务收尾时把产出沉淀为长期记忆。"""
        report = await self.facade.remember(
            state,
            owner_id=self.ctx.user_id,
            team_id=self.ctx.team_id,
        )
        d = report.to_dict()
        self.ctx.tracer.info(
            "Memory", "remember",
            f"候选 {d['candidates']}，结构化 {len(d['written_long_term'])}，"
            f"语义 {len(d['written_semantic'])}，拒收 {len(d['rejected'])}",
        )
        for r in d["rejected"][:5]:
            self.ctx.tracer.info("Memory", "reject", f"{r['reason']}｜{r['text'][:40]}")
        return d

    # ------------------------------------------------------------------ 状态
    def stats(self) -> dict[str, Any]:
        return {
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "hit_rate": round(self._cache_hits / max(1, self._cache_hits + self._cache_misses), 2),
        }
