"""分层 Memory（方案第九章）。

```
                Memory
                   │
       ┌───────────┼───────────┐
       ↓           ↓           ↓
     Redis       MySQL      Milvus
    短期任务    案件档案    语义知识
     (TTL 24h)  (结构化)    (脱敏向量)
                   │
              审计日志（不可删改）
```

对外只暴露本模块的三个单例与 :class:`MemoryFacade`，上层不直接碰具体存储。
"""

from __future__ import annotations

import logging
from typing import Any

from backend.memory.extractor import ExtractReport, MemoryExtractor, memory_extractor
from backend.memory.long_term import LongTermMemory, long_term_memory
from backend.memory.sanitizer import (
    RULES,
    Rule,
    Sanitizer,
    find_residue,
    sanitize,
    sanitize_case_payload,
    sanitizer,
)
from backend.memory.semantic import SemanticMemory, semantic_memory
from backend.memory.short_term import ShortTermMemory, short_term_memory

logger = logging.getLogger("legal_agent.memory")

__all__ = [
    "LongTermMemory", "long_term_memory",
    "SemanticMemory", "semantic_memory",
    "ShortTermMemory", "short_term_memory",
    "MemoryExtractor", "memory_extractor",
    "ExtractReport",
    "Sanitizer", "sanitizer", "sanitize", "sanitize_case_payload",
    "find_residue", "Rule", "RULES",
    "MemoryFacade", "memory",
]


class MemoryFacade:
    """统一的记忆门面：一次调用拿齐"这次任务该知道的背景"。"""

    def __init__(self) -> None:
        self.short = short_term_memory
        self.long = long_term_memory
        self.semantic = semantic_memory
        self.extractor = memory_extractor

    # ------------------------------------------------------------------ 读
    async def context_for(self, *, query: str, case_id: str = "",
                          top_k: int = 3) -> dict[str, Any]:
        """组装注入 Prompt 的记忆上下文。"""
        ctx: dict[str, Any] = {"case_digest": "", "experience": "", "sources": []}

        if case_id:
            try:
                ctx["case_digest"] = self.long.case_digest(case_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("案件档案读取失败：%s", exc)

        try:
            ctx["experience"] = await self.semantic.recall_context(query, top_k=top_k)
        except Exception as exc:  # noqa: BLE001
            logger.warning("经验召回失败：%s", exc)

        ctx["sources"] = [s for s in (ctx["case_digest"], ctx["experience"]) if s]
        return ctx

    def prompt_block(self, ctx: dict[str, Any]) -> str:
        """拼成可直接塞进 Prompt 的文本块。"""
        parts: list[str] = []
        if ctx.get("case_digest"):
            parts.append("【案件档案（已脱敏）】\n" + ctx["case_digest"])
        if ctx.get("experience"):
            parts.append(ctx["experience"])
        if not parts:
            return ""
        return ("\n\n以下为本所已有资料与历史经验，**仅供参考背景**，"
                "不得替代法条检索结果，也不得作为法律依据直接引用：\n\n"
                + "\n\n".join(parts))

    # ------------------------------------------------------------------ 写
    async def remember(self, state: dict[str, Any], *, owner_id: int | None = None,
                       team_id: int | None = None) -> ExtractReport:
        return await self.extractor.extract(state, owner_id=owner_id, team_id=team_id)

    # ------------------------------------------------------------------ 状态
    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, fn in (("short_term", self.short.status),
                         ("long_term", self.long.stats),
                         ("semantic", self.semantic.status)):
            try:
                out[name] = fn()
            except Exception as exc:  # noqa: BLE001
                out[name] = {"error": str(exc)}
        out["sanitizer_rules"] = len(RULES)
        return out


memory = MemoryFacade()
