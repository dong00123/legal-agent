"""Memory Extractor：防记忆膨胀的提取流水线。

方案 9.2：
```
原始对话 → 提取候选记忆 → 去重 → 质量判断 → 脱敏
        （去当事人姓名、案号敏感细节）→ 结构化 → 写入对应库
```
关键约束：**闲聊与案件无关内容不进长期记忆**。

本模块的四个环节都不是"走个形式"，每个环节都有明确的拒收理由（写进 ``rejected``），
因为"记错了"比"没记住"更糟：律师三个月后翻到一条张冠李戴的办案经验，
比没有这条经验危害大得多。

质量判断是**确定性规则**而不是让 LLM 自己说"这条重要" —— 与
:mod:`backend.llm.rule_engine` 一致：法律场景里，能被测试的规则优先于
不可复现的判断。
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from backend.memory.long_term import long_term_memory
from backend.memory.sanitizer import sanitize
from backend.memory.semantic import semantic_memory
from backend.memory.short_term import short_term_memory

logger = logging.getLogger("legal_agent.memory.extractor")

MemoryKind = Literal["case_archive", "experience", "preference", "reject"]


@dataclass
class Candidate:
    """一条候选记忆。"""

    kind: MemoryKind
    text: str
    title: str = ""
    reason: str = ""
    score: float = 0.0
    target: str = ""          # long_term / semantic / both


@dataclass
class ExtractReport:
    """提取结果（含每条的处置理由，便于审计与调参）。"""

    candidates: list[Candidate] = field(default_factory=list)
    written_long_term: list[str] = field(default_factory=list)
    written_semantic: list[str] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": len(self.candidates),
            "written_long_term": self.written_long_term,
            "written_semantic": self.written_semantic,
            "rejected": self.rejected,
            "notes": self.notes,
        }


# --------------------------------------------------------------------------- 规则
# 与案件无关的闲聊特征
CHITCHAT_PATTERNS = [
    re.compile(r"^(你好|您好|谢谢|多谢|再见|在吗|好的|收到|嗯|哦|哈哈)"),
    re.compile(r"(天气|吃饭|午饭|晚安|早上好|周末愉快)"),
    re.compile(r"^(帮我|请帮|麻烦)?(写|生成|翻译)[^法律]*$"),
]

# 高质量办案经验的特征（命中越多分越高）
EXPERIENCE_SIGNALS: list[tuple[str, float, re.Pattern[str]]] = [
    ("裁判口径", 0.30, re.compile(r"(法院(认为|认定|支持)|裁判(要旨|口径|倾向)|判决(支持|驳回))")),
    ("结果与比例", 0.25, re.compile(r"(按|以)[^。]{0,20}?(\d{1,3})\s*%[^。]{0,20}?(支持|获支持|获得|标准)")),
    ("可复用方法", 0.20, re.compile(r"(办案(要点|思路|指引)|建议(采取|先|应)|操作(要点|步骤)|注意事项)")),
    ("具体金额/期限", 0.15, re.compile(r"(违约金|赔偿金|补偿金|利息)[^。]{0,30}?\d")),
    ("证据要点", 0.10, re.compile(r"(关键证据|举证责任|证据(链|不足|充分))")),
]

# 需要脱敏的敏感细节特征
SENSITIVE_DETAIL = re.compile(
    r"(\d{11,}|[\w.+-]+@[\w-]+|（\d{4}）[^\s]{2,20}号|〔\d{4}〕[^\s]{2,20}号)"
)

# 长度阈值：太短没信息量，太长不适合进语义记忆（会被切块）
MIN_LEN = 24
MAX_LEN = 2000
MIN_EXPERIENCE_SCORE = 0.30

# 案件无关的通用法律常识，收录价值低
LOW_VALUE_PATTERNS = [
    re.compile(r"^(法律|法规|法条)(是什么|有哪些|怎么规定)"),
    re.compile(r"^(解释一下|介绍一下|讲讲)(法律|法规|法条)"),
]


class MemoryExtractor:
    """把一次任务的产出提炼成长期记忆。"""

    async def extract(self, state: dict[str, Any], *,
                      owner_id: int | None = None,
                      team_id: int | None = None,
                      persist: bool = True) -> ExtractReport:
        """从 State 提取候选记忆，去重、判质、脱敏后写入对应库。"""
        report = ExtractReport()

        for cand in self._candidates(state):
            verdict, reason = self._judge(cand, state)
            if verdict == "reject":
                report.rejected.append({"text": cand.text[:80], "reason": reason})
                continue
            cand.kind = verdict  # type: ignore[assignment]
            cand.reason = reason
            report.candidates.append(cand)

        if not report.candidates:
            report.notes.append("本次任务未产出可长期留存的记忆")

        if not persist:
            return report

        # ---- 结构化：案件档案
        archived = [c for c in report.candidates if c.target in ("long_term", "both")]
        if archived and state.get("case_id"):
            try:
                res = long_term_memory.upsert_from_state(state, owner_id=owner_id,
                                                         team_id=team_id)
                if not res.get("skipped"):
                    report.written_long_term.append(res["case_id"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("案件档案归档失败：%s", exc)
                report.notes.append(f"案件档案归档失败：{exc}")

        # ---- 语义化：办案经验
        for c in [x for x in report.candidates if x.target in ("semantic", "both")]:
            try:
                res = await semantic_memory.remember(
                    c.text, title=c.title or "办案经验",
                    meta={"memory_type": c.kind, "case_id": state.get("case_id") or "",
                          "tags": [state.get("intent") or ""]},
                    source_id=state.get("case_id") or state.get("task_id") or "",
                )
                if res.get("written"):
                    report.written_semantic.append(c.title or c.text[:40])
                    if res.get("anonymized"):
                        report.notes.append(f"「{(c.title or '')[:20]}」写入前已脱敏")
            except Exception as exc:  # noqa: BLE001
                logger.warning("语义记忆写入失败：%s", exc)
                report.notes.append(f"语义记忆写入失败：{exc}")

        # ---- 短期：任务痕迹（TTL 24h 自动过期）
        task_id = state.get("task_id") or ""
        if task_id:
            short_term_memory.set(task_id, {
                "intent": state.get("intent"), "citation_check": state.get("citation_check"),
                "confidence": state.get("confidence"), "extract": report.to_dict(),
            }, kind="extract")

        logger.info("记忆提取完成：候选 %d，档案 %d，语义 %d，拒收 %d",
                    len(report.candidates), len(report.written_long_term),
                    len(report.written_semantic), len(report.rejected))
        return report

    # ------------------------------------------------------------------ 候选生成
    def _candidates(self, state: dict[str, Any]) -> list[Candidate]:
        out: list[Candidate] = []

        # 1) 案件档案（只要有案件编号就归档）
        if state.get("case_id"):
            out.append(Candidate(kind="case_archive", target="long_term", score=1.0,
                                 title=(state.get("query") or "")[:60],
                                 text=(state.get("report") or state.get("draft") or
                                       state.get("query") or "")))

        # 2) 咨询/审查结论 → 可能成为办案经验
        conclusion = state.get("report") or state.get("draft") or ""
        if conclusion:
            out.append(Candidate(kind="experience", target="semantic", score=0.0,
                                 title=_title_of(state),
                                 text=conclusion[:MAX_LEN]))

        # 3) 争议焦点 + 结论的组合，是最有价值的"经验"形态
        issues = [i.get("title", "") for i in (state.get("issues") or []) if i.get("title")]
        if issues and conclusion:
            out.append(Candidate(
                kind="experience", target="semantic", score=0.0,
                title=f"{'；'.join(issues[:3])}",
                text=f"争议焦点：{'；'.join(issues[:5])}\n\n处理结论：{conclusion[:1200]}",
            ))

        # 4) 风险点清单 → 条文化经验
        for rp in (state.get("risk_points") or [])[:8]:
            risk = rp.get("risk") or ""
            sug = rp.get("suggestion") or ""
            if risk and sug:
                out.append(Candidate(
                    kind="experience", target="semantic", score=0.0,
                    title=f"条款风险：{(rp.get('clause_title') or rp.get('clause_no') or '')[:30]}",
                    text=f"风险：{risk}\n修改建议：{sug}",
                ))

        return out

    # ------------------------------------------------------------------ 质量判断
    def _judge(self, cand: Candidate, state: dict[str, Any]) -> tuple[str, str]:
        """返回 (verdict, reason)。verdict 为 reject / case_archive / experience / preference。"""
        text = (cand.text or "").strip()

        # 环节 1：长度
        if len(text) < MIN_LEN:
            return "reject", f"内容过短（{len(text)} < {MIN_LEN} 字），信息量不足"
        if len(text) > MAX_LEN:
            text = text[:MAX_LEN]
            cand.text = text

        # 环节 2：闲聊过滤
        for pat in CHITCHAT_PATTERNS:
            if pat.search(text[:40]):
                return "reject", "疑似闲聊/寒暄内容，不进长期记忆"

        # 环节 3：低价值常识
        for pat in LOW_VALUE_PATTERNS:
            if pat.search(text[:40]):
                return "reject", "通用法律常识问答，无本所特有信息，收录价值低"

        # 环节 4：案件档案直接通过（它进的是结构化库，不是语义库）
        if cand.kind == "case_archive":
            if not state.get("case_id"):
                return "reject", "未关联案件"
            return "case_archive", "关联案件，归档进结构化案件库"

        # 环节 5：经验质量打分
        score = 0.0
        hits: list[str] = []
        for name, weight, pat in EXPERIENCE_SIGNALS:
            if pat.search(text):
                score += weight
                hits.append(name)

        # 有法条依据是经验可信的前提，加权
        if state.get("statutes") and state.get("citation_check") == "passed":
            score += 0.20
            hits.append("引用已校验")
        # 置信度太低的内容不进长期记忆 —— 记忆会被反复引用，错的危害会放大
        if float(state.get("confidence") or 0) < 0.45:
            score -= 0.25
            hits.append("置信度偏低（扣分）")

        cand.score = round(score, 2)
        if score < MIN_EXPERIENCE_SCORE:
            return "reject", (f"经验质量不足（{score:.2f} < {MIN_EXPERIENCE_SCORE}），"
                              f"命中特征：{'、'.join(hits) or '无'}")
        return "experience", f"命中经验特征：{'、'.join(hits)}（得分 {score:.2f}）"

    # ------------------------------------------------------------------ 去重
    @staticmethod
    def fingerprint(text: str) -> str:
        """内容指纹：用于跨任务去重（同样的经验只存一次）。

        归一化后再哈希（去空白/标点），否则同一句经验因换行位置不同就会被存两遍。
        """
        norm = re.sub(r"[\s，,。.；;：:、！!？?（）()\[\]【】]", "", text or "")
        return hashlib.blake2b(norm.encode("utf-8"), digest_size=16).hexdigest()

    async def dedupe(self, texts: list[str], *, top_k: int = 3) -> list[tuple[str, float]]:
        """对候选文本做语义去重：返回 (文本, 最高相似度)。

        相似度超过阈值说明库里已有等价记忆，调用方可据此跳过写入。
        """
        out: list[tuple[str, float]] = []
        for t in texts:
            try:
                hits = await semantic_memory.recall(t[:400], top_k=top_k)
            except Exception:  # noqa: BLE001
                out.append((t, 0.0))
                continue
            best = 0.0
            fp = self.fingerprint(t)
            for h in hits:
                if self.fingerprint(h.get("text") or "") == fp:
                    best = 1.0
                    break
                best = max(best, float(h.get("score") or 0.0))
            out.append((t, best))
        return out


def _title_of(state: dict[str, Any]) -> str:
    q = (state.get("query") or "").strip()
    intent = state.get("intent") or ""
    label = {
        "contract_review": "合同审查经验",
        "case_analysis": "案件分析经验",
        "consult": "法律咨询经验",
        "doc_draft": "文书起草经验",
        "case_search": "类案检索记录",
        "statute_lookup": "法规查询记录",
    }.get(intent, "办案经验")
    return f"{label}：{q[:40]}" if q else label


memory_extractor = MemoryExtractor()
