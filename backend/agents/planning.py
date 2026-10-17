"""动态规划：按**问题本身**（而不是只按意图）决定这一次跑哪些节点。

为什么要替换固定路由
--------------------
``INTENT_ROUTES`` 是"意图 → 固定节点序列"。它解决的是"别把简单问题塞进
完整状态机"，但粒度仍然太粗：同一个 ``consult`` 意图里

    「竞业限制补偿金最低标准是多少」
    「公司两年不签合同又不缴社保，我能主张哪些权利、要走什么程序」

复杂度差一个量级，却跑同一条 7 节点链路 —— 前者浪费（只要法条），
后者可能还不够（要事实认定、举证责任、程序路径）。

本模块做两件事，顺序是**规则在前、模型在后**（与 router.py 同一套纪律）：

1. :func:`draft_plan` —— 确定性规则，给出候选节点 + 命中的规则；
2. :func:`refine_with_llm` —— 可选，让模型在候选集内再挑一次并给出理由。

三条硬约束（决定这个模块能不能上线）
------------------------------------
* **只能从候选池里挑**：模型不能凭空造节点，否则出现幻觉节点名会让
  ``next_node`` 直接返回 END（流程静默结束，没有任何报错）。
* **必需节点不可删**：DraftNode（起草）/ ReviewNode（有材料的审查）/
  ReportNode（收尾）缺失会产出"看着完整其实缺一块"的交付物。
* **失败一律回退**：模型不可用、返回非 JSON、结果不合法 → 沿用规则结果，
  并且**声明**是规则兜底（不静默）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from backend.agents.harness.router import route_for_intent
from backend.app.config import settings

logger = logging.getLogger("legal_agent.agents.planning")

# 每个意图**允许**出现的节点（静态路由 ∪ 可扩张节点）。
# 可扩张节点的意义：规则识别到强信号时，允许超出静态路由去加节点 ——
# 例如 quick_answer 默认只有 [Statute, Report]，但问到"能主张哪些权利"
# 时应该加 CaseNode（历史上这正是"类案 0 个"的根因）。
_EXPANDABLE: dict[str, tuple[str, ...]] = {
    "off_topic": (),
    "quick_answer": ("CaseNode", "KnowledgeNode", "ValidatorNode"),
    "statute_lookup": ("CaseNode", "KnowledgeNode"),
    "case_search": ("KnowledgeNode", "AnalysisNode"),
    "consult": ("CaseNode", "KnowledgeNode", "AnalysisNode", "ValidatorNode", "DraftNode"),
    "case_analysis": ("KnowledgeNode", "DraftNode"),
    "contract_review": ("CaseNode", "AnalysisNode"),
    "doc_draft": ("StatuteNode", "CaseNode", "KnowledgeNode", "ValidatorNode"),
}

# 节点执行顺序（与 planner.NODE_AGENT 的语义顺序一致）：
# 动态规划可以自由增删，但**不能**打乱依赖顺序 ——
# 例如 ValidatorNode 必须在检索节点之后（它核的是检索到的引用），
# AnalysisNode 必须在 Review/Intake 之后（它归纳的是解析出来的事实）。
_ORDER = ("IntakeNode", "StatuteNode", "CaseNode", "KnowledgeNode",
          "ReviewNode", "AnalysisNode", "ValidatorNode", "DraftNode",
          "ReportNode")

# --------------------------------------------------------------------------- 信号词
# 末段是**救济诉求**（主张权利/维权/索赔…）：这类问题要的是"法院支持不支持、
# 一般怎么判"，没有类案支撑就只能给法条，答不到点上。
# （router.py 的 _HEAVY_DEMAND_RE 里有一组同义判据，两处必须保持一致，
#   否则会出现"判定为咨询却按快速问答只跑两个节点"的自相矛盾。）
_RE_CASE = re.compile(
    r"(类案|相似案例|类似案例|相关案例|判例|裁判观点|怎么判|如何认定|有没有.*案例|法院一般会|"
    r"主张|维权|索赔|可以要求|有哪些权利|什么权利)"
)
_RE_DRAFT = re.compile(r"(起草|拟(一份|个)?|写(一份|个)?(起诉状|答辩状|意见书|律师函|上诉状|申请书)|帮我写|代书)")
_RE_REVIEW = re.compile(r"(审查|审阅|逐条|过一下|风险(点|清单)|有没有问题|哪里有问题|有坑)")
_RE_KNOWLEDGE = re.compile(r"(本所|所里|内部|指引|审查清单|办案指引|模板|惯例)")
_RE_ANALYSIS = re.compile(r"(分析|争议焦点|攻防|策略|怎么打|胜诉|把握|评估|研判)")
_RE_VALIDATE = re.compile(
    r"(《[^》]{2,40}》\s*第\s*[一二三四五六七八九十百零两\d]+\s*条|"
    r"第\s*[一二三四五六七八九十百零两\d]+\s*条|"
    r"[(（]?\d{4}[)）]?\s*[^\s]{0,12}号)"
)


@dataclass
class PlanDraft:
    """一次动态规划的结果。"""

    intent: str
    nodes: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    why: str = ""
    source: str = "rule"          # rule / llm / static
    rejected: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"intent": self.intent, "nodes": self.nodes, "rules": self.rules,
                "why": self.why, "source": self.source, "rejected": self.rejected}


# --------------------------------------------------------------------------- 规则层
def draft_plan(*, intent: str, query: str, has_material: bool = False,
               doc_type: str = "", material_text: str = "") -> PlanDraft:
    """按确定性规则给出候选节点序列。

    规则是**可解释、可回归测试、不依赖外部服务**的：日志里能看到命中的是哪条，
    运营同学可以直接调词表；模型不可用也不会让链路退化。
    """
    route = route_for_intent(intent)
    effective_intent = route["intent"]
    base = list(route["nodes"])
    allow = set(base) | set(_EXPANDABLE.get(effective_intent, ()))
    text = f"{query}\n{material_text[:2000]}" if material_text else query

    draft = PlanDraft(intent=effective_intent, nodes=list(base))
    draft.rules.append(f"基线：静态路由 {route['label']}（{'→'.join(base)}）")

    # ---- 1) 与法律无关：只直答，任何检索都是浪费
    if effective_intent == "off_topic":
        draft.nodes = ["ReportNode"]
        draft.rules.append("非法律问题：只保留直答")
        return _finalize(draft, allow)

    nodes = set(base)

    # ---- 2) 没有材料时，解析/逐条审查没有输入。
    # ⚠ 但**以材料为主**的意图（合同审查 / 案件分析）不能因为这一条被裁掉：
    # 材料可能通过 material_ids 传入（此时 State 里的 material_text 还是空的，
    # 要等 IntakeNode 去取），按"没材料"裁掉 Intake/Review 的后果是
    # 合同审查跑完 clauses=0 —— 一份没有条款的风险审查，比不跑更糟。
    material_bound = ("contract_review", "case_analysis", "doc_draft")
    if not has_material and not material_text and effective_intent not in material_bound:
        for n in ("IntakeNode", "ReviewNode"):
            if n in nodes:
                nodes.discard(n)
                draft.rules.append(f"未提供材料，去掉 {n}（没有可解析/可逐条扫描的输入）")

    # ---- 3) 强信号 ⇒ 加节点（这才是"按问题规划"的核心）
    if _RE_CASE.search(text) and "CaseNode" in allow and "CaseNode" not in nodes:
        nodes.add("CaseNode")
        draft.rules.append("命中「类案/裁判观点」诉求，增加 CaseNode")
    if _RE_DRAFT.search(text) and "DraftNode" in allow and "DraftNode" not in nodes:
        nodes.add("DraftNode")
        draft.rules.append("命中「起草文书」诉求，增加 DraftNode")
    if _RE_REVIEW.search(text) and has_material and "ReviewNode" in allow and "ReviewNode" not in nodes:
        nodes.add("ReviewNode")
        draft.rules.append("命中「审查/风险」诉求且有材料，增加 ReviewNode")
    if _RE_KNOWLEDGE.search(text) and "KnowledgeNode" in allow and "KnowledgeNode" not in nodes:
        nodes.add("KnowledgeNode")
        draft.rules.append("命中「本所指引/清单」诉求，增加 KnowledgeNode")
    if _RE_ANALYSIS.search(text) and "AnalysisNode" in allow and "AnalysisNode" not in nodes:
        nodes.add("AnalysisNode")
        draft.rules.append("命中「争议焦点/策略」诉求，增加 AnalysisNode")
    # 终审：**只要链路里检索了法条/类案，就必须有节点对它们做出效力判定。**
    # 原先只在提问里出现具体法条/案号时才加 —— 但"有没有核过"不该由律师的
    # 提问措辞决定：报告正文正是从检索结果拼出来的，一条已废止的法条混进去，
    # 与他有没有点名它是同一回事。快速问答（statute → report）原先因此
    # 永远没有终审，`citation_check` 恒为 pending，报告里写着"未执行引用终审"。
    if "ValidatorNode" not in nodes and "ValidatorNode" in allow:
        if _RE_VALIDATE.search(text):
            nodes.add("ValidatorNode")
            draft.rules.append("提问含具体法条/案号，增加 ValidatorNode 逐条核验")
        elif "StatuteNode" in nodes or "CaseNode" in nodes:
            nodes.add("ValidatorNode")
            draft.rules.append("链路含法条/类案检索，增加 ValidatorNode 核效力与案号")

    # ---- 4) 弱信号 ⇒ 减节点：一句话能答完的，不跑完整证据链
    if _looks_shallow(text) and effective_intent in ("consult", "case_analysis"):
        for n in ("IntakeNode", "AnalysisNode", "DraftNode"):
            if n in nodes:
                nodes.discard(n)
                draft.rules.append(f"问题为单一结论型，去掉 {n}")

    draft.nodes = sorted(nodes, key=_rank)
    return _finalize(draft, allow)


def _looks_shallow(text: str) -> bool:
    """是否是"只要一个结论"的浅问题（短、无程序诉求、无争议描述）。"""
    t = re.sub(r"\s+", "", text or "")
    if not t or len(t) > 45:
        return False
    if _RE_CASE.search(text) or _RE_DRAFT.search(text) or _RE_REVIEW.search(text):
        return False
    # 问"多少/多久/是不是/有没有"这类数值或判断题
    return bool(re.search(r"(多少|多久|几(天|个月|年)|是不是|有没有|能否|标准|时效|上限|下限)", text))


def _rank(node: str) -> int:
    return _ORDER.index(node) if node in _ORDER else len(_ORDER)


def _finalize(draft: PlanDraft, allow: set[str]) -> PlanDraft:
    """收口：过滤候选池外节点、保证必需节点与顺序。"""
    kept, dropped = [], []
    for n in draft.nodes:
        (kept if n in allow or n == "ReportNode" else dropped).append(n)
    for n in dropped:
        draft.rejected.append(f"{n}（不在意图 {draft.intent} 的候选池内）")

    # ReportNode 必须收尾：它是唯一产出交付物与置信度的节点
    if "ReportNode" not in kept:
        kept.append("ReportNode")
    else:
        kept = [n for n in kept if n != "ReportNode"] + ["ReportNode"]

    draft.nodes = sorted(dict.fromkeys(kept), key=_rank)
    draft.why = f"动态规划（{len(draft.nodes)} 个节点）：{'、'.join(draft.rules[:3])}"
    return draft


# --------------------------------------------------------------------------- 模型层
_LLM_SYSTEM = """你是法律 Agent 的编排器。给定律师的提问与可用的节点候选集，
挑选**本次真正需要**的节点，不要为"显得严谨"多加节点。

可用节点及其作用：
- IntakeNode：解析案件材料，提取当事人/事实/时间线（没有材料时不选）
- StatuteNode：检索适用法条并校验时效性（法律问题几乎都需要）
- CaseNode：检索类案、提炼裁判要旨（问"法院怎么判/有没有类似案例"时选）
- KnowledgeNode：检索本所办案指引与审查清单（涉及本所内部口径时选）
- ReviewNode：逐条风险扫描（有合同/文书材料且要"审风险"时选）
- AnalysisNode：归纳争议焦点、攻防要点与诉讼策略（问"怎么打/胜算/焦点"时选）
- ValidatorNode：引用终审，逐条核对法条编号与案号（引用较多或问到具体条文时选）
- DraftNode：生成文书草稿（要"起草/代书"时选）
- ReportNode：汇总输出（必选）

规则：
1. 只能从候选集里选，不得新增；ReportNode 必须在最后。
2. 宁少勿多：多跑一个检索节点就是多等几十秒，且会引入无关证据。
3. 只输出 JSON：{"nodes": [...], "why": "一句话说明为什么这样裁"}"""


# 每个意图**赖以成立**的节点：裁掉它们，这个意图就不名副其实了。
# 例：合同审查删掉 Intake/Review ⇒ 没有条款、没有风险点，只剩"引了 13 条法条"，
# 交付物看着像审查报告，实际一份条款都没审（真机断言 clauses>0 直接挂）。
_ESSENTIAL: dict[str, tuple[str, ...]] = {
    "contract_review": ("IntakeNode", "ReviewNode"),
    "case_analysis": ("IntakeNode", "AnalysisNode"),
    "doc_draft": ("DraftNode",),
    "case_search": ("CaseNode",),
    "statute_lookup": ("StatuteNode",),
}


def _protected_of(draft: PlanDraft, query: str = "") -> set[str]:
    """不可被模型裁掉的节点。

    真机踩过：模型把「能主张哪些权利」的链路裁成 StatuteNode → ReportNode，
    理由是"基础法律适用问题，无需类案"——顺手也把 ValidatorNode 裁了，
    于是引用**没有经过终审**就交付（citation_check=pending、置信度 0.35）。
    模型的"宁少勿多"倾向会优先砍掉**不产出可见文字**的节点，
    而核验恰恰是不产出文字、只保证不出错的那一个。

    所以这里明确：法条检索、引用核验、收尾，以及规则因**强信号**加进来的
    节点（如"主张权利"触发的 CaseNode），一律不许删。
    """
    # 只有这几类是"锦上添花"，允许模型裁；其余一律不许删。
    trimmable = {"KnowledgeNode", "AnalysisNode", "DraftNode", "IntakeNode"}
    # 类案节点：**提问里带类案/救济诉求时不可裁**。
    # 注意不能只看"规则是否新增了 CaseNode" —— consult 的基线路由本来就含它，
    # 于是"能主张哪些赔偿"这种强诉求反而被当成可裁剪项，又被裁回只给法条。
    if "CaseNode" in draft.nodes and not _RE_CASE.search(query or ""):
        trimmable.add("CaseNode")

    protected = {"StatuteNode", "ValidatorNode", "ReportNode"}
    protected.update(_ESSENTIAL.get(draft.intent, ()))
    protected.update(n for n in draft.nodes if n not in trimmable)
    return {n for n in protected if n in draft.nodes or n == "ReportNode"}


async def refine_with_llm(draft: PlanDraft, *, query: str, has_material: bool = False,
                          doc_type: str = "", timeout: float = 20.0) -> PlanDraft:
    """让模型在候选集内复核一次。

    **任何异常都回退到规则结果**：编排器不可用不该让整条链路跑不起来，
    也不该让律师等着一个规划调用超时。规划失败的最坏后果只是"按规则跑"，
    而规则本就是可运行的结果 —— 所以这里静默降级是安全的（会写日志）。
    """
    from backend.llm.client import llm_client

    allow = sorted(set(draft.nodes) | set(_EXPANDABLE.get(draft.intent, ())) | {"ReportNode"},
                   key=_rank)
    user = (
        f"律师提问：{query[:600]}\n"
        f"是否提供材料：{'是' if has_material else '否'}\n"
        f"文档类型：{doc_type or '未知'}\n"
        f"候选集：{allow}\n"
        f"规则先给出的方案：{draft.nodes}（依据：{'；'.join(draft.rules[:3])}）"
    )
    try:
        data, result = await llm_client.chat_json(
            [{"role": "system", "content": _LLM_SYSTEM},
             {"role": "user", "content": user}],
            task="plan", default={},
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("动态规划未走模型（沿用规则）：%s", exc)
        return draft

    # 规则引擎兜底时 chat_json 也给得出结果，但那不是"模型编排"，不能算数
    if getattr(result, "provider", "") == "rule":
        logger.info("动态规划：模型不可用，沿用规则结果")
        return draft

    picked = data.get("nodes")
    if not isinstance(picked, list) or not picked:
        return draft

    cleaned = [n for n in picked if n in allow]
    if "ReportNode" not in cleaned:
        cleaned.append("ReportNode")
    cleaned = sorted(dict.fromkeys(cleaned), key=_rank)

    # 必需节点补齐：模型漏掉的必须加回来（它倾向于砍掉"不产出文字"的核验节点）
    protected = _protected_of(draft, query)
    missing = protected - set(cleaned)
    if missing:
        logger.info("模型裁掉了必需节点 %s，已补回（理由：%s）",
                    sorted(missing), str(data.get("why") or "")[:80])
    cleaned = sorted(set(cleaned) | protected, key=_rank)

    if len(cleaned) <= 1 and draft.intent != "off_topic":
        logger.info("动态规划：模型给出的链路退化为空，沿用规则结果")
        return draft

    note = f"模型复核：{str(data.get('why') or '')[:120]}"
    if missing:
        note += f"｜已补回必需节点：{'、'.join(sorted(missing))}"
    out = PlanDraft(
        intent=draft.intent, nodes=cleaned,
        rules=draft.rules + [note],
        why=str(data.get("why") or ""), source="llm", rejected=draft.rejected,
    )
    logger.info("动态规划已由模型复核：%s → %s", draft.nodes, cleaned)
    return out


# --------------------------------------------------------------------------- 对外
def sanitize(nodes: list[str] | None, *, intent: str, fallback: list[str]) -> list[str]:
    """最后一道闸：接口/模型给的节点序列在这里被兜住。

    ``next_node`` 只认 ``state["route"]``，出现未知节点名会让流程静默结束
    （图里没有这个节点 → 条件边无匹配 → END），所以必须过滤。
    """
    if not nodes:
        return list(fallback)
    allow = set(route_for_intent(intent)["nodes"]) | set(_EXPANDABLE.get(intent, ()))
    kept = [n for n in nodes if n in allow or n == "ReportNode"]
    if "ReportNode" not in kept:
        kept.append("ReportNode")
    kept = sorted(dict.fromkeys(kept), key=_rank)
    return kept or list(fallback)


def dynamic_enabled() -> bool:
    return bool(getattr(settings, "DYNAMIC_PLAN_ENABLED", False))
