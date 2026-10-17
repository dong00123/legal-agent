"""Agent 全局 State 与领域实体定义。

对应方案 4.2 节「LangGraph 状态机」的 State 结构，并在其基础上补齐工程化字段
（steps / tool_calls / trace_id / evidence_gap / citation 明细）。

约定：
- 实体用 pydantic 模型，保证可序列化、可审计、跨端 Schema 一致；
- ``LegalState`` 用 TypedDict，字段全部可选（LangGraph 节点只写自己负责的部分）；
- 不做隐式 reducer：每个节点返回「完整的新值」，流程可回放、可回滚。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field

Intent = Literal[
    "consult",       # 法律咨询 / 智能问答
    "contract_review",  # 合同审查
    "case_search",   # 类案检索
    "statute_lookup",  # 法规检索
    "doc_draft",     # 文书起草
    "case_analysis",  # 案件分析
    "quick_answer",  # 法律快速问答：单一结论/概念解释，只检索法条后直答
    "off_topic",     # 与法律无关：不检索、不终审，直接作答
]

DocType = Literal["contract", "litigation", "consult", "evidence", "opinion", "statute", "unknown"]

# 法律领域：先识别用户问题的法律领域，再按领域检索与回答（跨领域错配治理）。
# 枚举与 backend/rag/domain.py 的 DOMAINS 一致；unknown = 规则/模型均无法判定，
# 此时检索层不做领域过滤（退回改造前行为）。
LegalDomain = Literal["labor", "tax", "env", "civil", "criminal", "admin",
                      "constitution", "ip", "transport", "military", "agriculture",
                      "trade", "safety", "culture", "ethnic", "social_security",
                      "legislation", "judicature", "economy", "unknown"]

RiskLevel = Literal["high", "medium", "low"]

# 法条效力状态：法律场景的硬性标注字段
StatuteStatus = Literal["现行有效", "已修订", "已废止", "尚未生效", "未知"]


# --------------------------------------------------------------------------- 缺口措辞
# 证据缺口的**规范措辞**。同一件事只能有一种说法。
#
# 为什么把字符串提到公共位置：同一件事（"没提取到案件事实"）此前在三个地方各写了一遍，
# 措辞互不相同（「无结构化案件事实，无法归纳争议焦点」／「未提取到结构化案件事实」／
# 「未提取到结构化事实，需补充案件材料。」），而缺口去重是按理由精确比对的，
# 三条谁也去不掉谁，于是同一件事在报告里并列出现三遍。律师看到三条说的是同一件事，
# 会认为系统在凑数 —— 一旦对缺口清单失去信任，真正要紧的那条也就没人看了。
#
# 分工约定：**reason 只说"事实缺什么"（意图中立），impact 说"对本次交付的影响"**。
# 把后果写进 reason 会导致同一事实在不同意图下措辞不同，去重再次失效。
#
# 规则：所有节点与降级路径登记这几类缺口时，**一律引用这里的常量**，不要手写字符串。
GAP_NO_FACTS = "未提取到结构化案件事实"
GAP_NO_STATUTES = "法规检索未命中适用条文"
GAP_NO_CASES = "类案检索未获取到可用判例"


# A2A 委派方给被调方指定的**专项检索式**（如 AnalysisNode 让 StatuteNode 只查
# 某一条法条，而不是按 Planner 的宽检索式重跑一遍）。
#
# 键定义在这里而不是 ``protocol/delegation.py``：它首先是一个 **State 键**，
# 而各节点要用它就得 import，从 delegation 引会成环
# （``delegation → protocol.client → nodes → statute_node``）。
A2A_FOCUS_KEY = "a2a_focus_query"


def focus_of(state: LegalState) -> str:
    """读委派方指定的专项检索式。**就地读，不弹出**。

    原先 ``StatuteNode`` 是 ``state.pop(...)``（读取即消费），当时的理由是
    "只影响本次委派"。但那个理由只在"一次任务里同一 Agent 只会被委派一次"时成立 ——
    而现在 Agent 会自主委派（同一条法令可能在 Analysis 的不同环节被要两次），
    弹出式读取会让**第二次委派拿到空检索式**，于是它退回按 Planner 的宽检索式
    重跑一遍：既贵，又和委派方的意图完全无关。

    清理交给委派层的 ``finally``（见 ``protocol/delegation.delegate``），
    那里记录了委派前的原值，能原样还回去。
    """
    value = state.get(A2A_FOCUS_KEY) or ""
    return value.strip() if isinstance(value, str) else ""


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def new_task_id() -> str:
    """生成一个任务编号。

    暴露出来是给"先建任务、后执行"这条路用的（``/chat?enqueue=true`` 会先入队）。
    **不直接对外暴露 ``_uid``**：``_uid`` 还能生成 trace_id、case_id 等，
    放出去迟早会有人拿它生成 case_id 而漏掉案件编号的格式约束。
    这里只固定 ``task_`` 前缀，和 :func:`new_state` 里用的是同一行代码 ——
    两处各写一遍的话，表现是"排队建的任务编号格式和直接跑的不一样"。
    """
    return _uid("task")


# --------------------------------------------------------------------------- 实体
class Party(BaseModel):
    """主体信息。"""

    name: str = ""
    role: str = ""            # 原告/被告/甲方/乙方/用人单位/劳动者
    kind: str = ""            # 自然人/法人/非法人组织
    extra: dict[str, Any] = Field(default_factory=dict)


class Fact(BaseModel):
    """案件事实。"""

    id: str = Field(default_factory=lambda: _uid("fact"))
    text: str
    source: str = ""          # 来源文件 / 条款
    page: int | None = None
    confidence: float = 1.0


class Issue(BaseModel):
    """争议焦点。"""

    id: str = Field(default_factory=lambda: _uid("issue"))
    title: str
    description: str = ""
    severity: RiskLevel = "medium"
    basis: list[str] = Field(default_factory=list)   # 关联法条引用


class StatuteHit(BaseModel):
    """检索到的法条。``status`` 为强制字段——过时的法律依据比没有依据更危险。"""

    doc_name: str                  # 法规名称，如《中华人民共和国劳动合同法》
    article_no: str                # 条文号，如「第八十七条」
    text: str = ""
    status: StatuteStatus = "未知"
    effective_date: date | None = None
    repealed_date: date | None = None
    version: str = ""
    publisher: str = ""
    chunk_id: str = ""
    source_path: str = ""
    score: float = 0.0
    retriever: str = ""            # vector / bm25 / hybrid / rerank
    verified: bool = False         # 是否通过存在性校验


class CaseHit(BaseModel):
    """检索到的类案。``case_no`` / ``case_name`` 至少一项可校验。

    现实约束：公开典型案例（最高法/人社部发布）常常只有**案例名称**而没有案号，
    所以校验支持两条路径——有案号按案号校验，无案号按「案例名称 + 来源」校验，
    两条都不通过则判定为不可信引用。
    """

    case_no: str = ""              # 案号，如（2023）京01民终12345号
    case_name: str = ""            # 案例名称，如「某高纤公司与崔某劳动合同纠纷案」
    court: str = ""
    judge_date: date | None = None
    cause_of_action: str = ""      # 案由
    keywords: list[str] = Field(default_factory=list)
    gist: str = ""                 # 裁判要旨 / 裁判要点
    result: str = ""               # 裁判结果
    reasoning: str = ""            # 裁判理由
    issues: list[str] = Field(default_factory=list)
    related_statutes: list[str] = Field(default_factory=list)  # 相关法条
    source: str = ""               # 来源（典型案例批次 / 案例库）
    full_text: str = ""
    chunk_id: str = ""
    source_path: str = ""
    score: float = 0.0
    retriever: str = ""
    verified: bool = False


class KnowledgeHit(BaseModel):
    """律所知识库 / 办案指引 / 合同模板命中的片段。指引必须标注版本。"""

    title: str
    doc_type: str = ""             # 审查清单 / 办案指引 / 合同模板
    version: str = ""
    effective_date: date | None = None
    snippet: str = ""
    chunk_id: str = ""
    source_path: str = ""
    score: float = 0.0


class RiskPoint(BaseModel):
    """合同审查风险点：条款定位 + 风险等级 + 修改建议 + 法律依据。"""

    clause_no: str = ""            # 第8条
    clause_title: str = ""
    clause_text: str = ""
    risk: str = ""
    level: RiskLevel = "medium"
    suggestion: str = ""
    basis: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    clause_index: int = -1         # 在原文条款序列中的下标，供 EXE 端逐条高亮定位


class MissingClause(BaseModel):
    """缺失条款提示。"""

    name: str
    reason: str = ""
    level: RiskLevel = "medium"


class CitationRecord(BaseModel):
    """统一引用记录，Citation Validator 逐条核对的对象。"""

    type: Literal["statute", "case", "knowledge"] = "statute"
    name: str = ""                 # 法规名 / 判例标题 / 知识库标题
    article: str = ""              # 条文号
    case_no: str = ""              # 案号
    court: str = ""
    status: str = ""               # 效力状态
    version: str = ""
    source_path: str = ""
    verified: bool = False
    reason: str = ""               # 校验结论说明
    raw: str = ""                  # 原始引用文本


class EvidenceGap(BaseModel):
    """证据缺口声明。法律场景降级原则：宁可缩小结论范围，也不假装有证据。"""

    kind: str                      # statute / case / knowledge / vlm / tool
    reason: str
    impact: str = ""
    degraded: bool = True


class ToolCallRecord(BaseModel):
    """一次工具调用记录，供 Trace / 审计 / 统计使用。"""

    step: int = 0
    tool: str = ""
    agent: str = ""
    args_summary: str = ""
    ok: bool = True
    elapsed_ms: int = 0
    retries: int = 0
    error: str = ""
    cached: bool = False
    started_at: datetime | None = None


class TraceEvent(BaseModel):
    """全链路 Trace 事件。"""

    ts: datetime = Field(default_factory=datetime.now)
    node: str = ""
    event: str = ""
    detail: str = ""
    level: str = "info"
    elapsed_ms: int = 0


# --------------------------------------------------------------------------- State
class LegalState(TypedDict, total=False):
    """LangGraph 全局状态。"""

    # 任务与归属
    task_id: str
    case_id: str
    trace_id: str
    user_id: str
    team_id: str
    created_at: str

    # 意图与路由
    query: str
    intent: Intent
    # 调用方**显式钉住**的意图（产品页面各自的 Agent 入口用它）。
    #
    # 与 ``intent`` 的区别是"谁定的"：``intent`` 是 IntentNode 判出来的结果，
    # 这个字段是入口传进来的**要求**。留空 = 由 IntentNode 判定。
    #
    # 为什么必须单独存一个字段而不是直接写 ``intent``：IntentNode 会把判定结果
    # 写进 ``intent``，如果入口也写同一个键，事后从 State 里根本分不清
    # "它判对了"还是"入口钉住了、它没判" —— 而这两件事在排障时处置完全相反。
    intent_override: str
    # 法律领域（labor/tax/env/civil/unknown）。由 IntentNode 在意图判定时一并
    # 写入：StatuteNode / KnowledgeNode / 检索工具按它过滤跨领域错配。
    domain: LegalDomain
    route: list[str]                 # 本次实际执行的节点序列
    doc_type: DocType
    # 交付物形态：``report``（默认，七节报告骨架）/ ``conversational``（对话正文＋报告尾巴）。
    #
    # 为什么是 State 上的一个开关、而不是直接换掉提示词：**四个产品入口共用
    # ReportNode**，其中合同审查页的 ``formatStructuredAnswer`` 依赖报告骨架里的
    # 「风险点」「证据缺口」等区块（``web/app.js``）。全局替换会把那一页一起改掉。
    #
    # ⚠ 它改变交付物 ⇒ **必须折进** ``orchestrator._cache_material``（与 ``role=`` /
    #   ``intent=`` / ``top_k=`` 同一批），否则同一条 query 先被合同审查页跑过、
    #   助手页再问会静默领回一份报告型交付物，且处处不报错。
    answer_style: str
    plan_source: str                 # 节点序列由谁定的：rule（规则规划）/ llm（模型复核后）；见 planning.draft_plan
    cache_hit: dict[str, Any]        # 命中相似问题缓存时的溯源信息（原 task_id / 相似度 / 原始提问）

    # 材料解析
    material_text: str
    # 范本合同正文（**比对基准，可选**）。只被 ``ReviewNode`` 消费。
    #
    # 刻意与 ``material_text`` 分开、而不是拼成一整段：拼了以后两份合同的
    # 条款编号会在 ``IntakeNode`` 拆条时混进同一个序列，风险点里的
    # 「第 X 条」就可能指到范本上去 —— 那是定位错误，律师照着找不到。
    template_text: str
    # 审查立场（甲方/乙方/……）。**界面显式指定的优先**，留空则由 ReviewNode
    # 从 query 里正则兜底，再兜不到就是中立。见 ``review_node._client_role``。
    client_role: str
    material_paths: list[str]
    clauses: list[dict[str, Any]]    # 合同条款序列（供条款定位与逐条高亮）
    tables: list[dict[str, Any]]
    placeholders: list[dict[str, Any]]
    parties: list[dict[str, Any]]
    facts: list[dict[str, Any]]
    issues: list[dict[str, Any]]

    # 检索证据
    statutes: list[dict[str, Any]]
    cases: list[dict[str, Any]]
    knowledges: list[dict[str, Any]]
    # 检索条数（调用方指定，0 = 用节点默认值）。
    #
    # 存在的理由：类案检索页面上有「返回条数」面板，选了却发不出去，是这套界面
    # 最容易出现的假功能。而 ``case_node`` 原先把 ``top_k=5`` 写死在调用点上，
    # 页面的选择**没有任何通路进来** —— 面板动一下，命中数纹丝不动。
    top_k: int

    # 审查与分析
    risk_points: list[dict[str, Any]]
    missing_clauses: list[dict[str, Any]]
    strategy: list[str]              # 攻防要点 / 诉讼策略
    limitation_check: dict[str, Any]  # 诉讼时效核查

    # 文书
    draft: str
    revision_draft: str
    draft_type: str

    # 引用终审
    citations: list[dict[str, Any]]
    citation_check: Literal["passed", "failed", "pending"]
    citation_summary: dict[str, Any]

    # 降级与合规
    evidence_gap: list[dict[str, Any]]
    confidence: float
    disclaimer: str

    # 运行时
    report: str
    steps: int
    tool_calls: list[dict[str, Any]]
    trace: list[dict[str, Any]]
    notes: list[str]
    error: str


def new_state(
    query: str,
    *,
    task_id: str | None = None,
    case_id: str = "",
    user_id: str = "",
    team_id: str = "",
    material_text: str = "",
    template_text: str = "",
    client_role: str = "",
    material_paths: list[str] | None = None,
    doc_type: DocType = "unknown",
    intent_override: str = "",
    domain: LegalDomain = "unknown",
    top_k: int = 0,
    answer_style: str = "",
    expert_persona: str = "",
    expert_domains: list[str] | None = None,
    expert_hints: list[str] | None = None,
    expert_template: str = "",
) -> LegalState:
    """构造初始 State。

    ``intent_override`` / ``top_k`` / ``answer_style`` / ``expert_persona`` 都由
    **入口**传进来（产品页面的 Agent 端点），普通提问留空即可 ——
    留空时行为与改动前逐字一致（``answer_style`` 空值按 ``report`` 处理）。

    ``expert_domains`` / ``expert_hints`` / ``expert_template`` 是**专家增强配置**：
    与 ``expert_persona`` 同源（``expert_registry.enhance_for``），
    定向检索领域 / 检索增强词 / 专属回答模板。全部留空时专家与普通助手逐字一致。
    """
    from datetime import datetime as _dt

    from backend.app.config import settings

    return LegalState(
        task_id=task_id or _uid("task"),
        case_id=case_id,
        trace_id=_uid("trace"),
        user_id=user_id,
        team_id=team_id,
        created_at=_dt.now().isoformat(timespec="seconds"),
        query=query,
        intent_override=intent_override,
        domain=domain,
        top_k=top_k,
        answer_style=answer_style or "report",
        expert_persona=expert_persona,
        expert_domains=list(expert_domains or []),
        expert_hints=list(expert_hints or []),
        expert_template=expert_template or "",
        route=[],
        doc_type=doc_type,
        material_text=material_text,
        template_text=template_text,
        client_role=client_role,
        material_paths=material_paths or [],
        clauses=[],
        tables=[],
        placeholders=[],
        parties=[],
        facts=[],
        issues=[],
        statutes=[],
        cases=[],
        knowledges=[],
        risk_points=[],
        missing_clauses=[],
        strategy=[],
        limitation_check={},
        draft="",
        revision_draft="",
        draft_type="",
        citations=[],
        citation_check="pending",
        citation_summary={},
        evidence_gap=[],
        confidence=0.0,
        disclaimer=settings.DISCLAIMER,
        report="",
        steps=0,
        tool_calls=[],
        trace=[],
        notes=[],
        error="",
    )


# --------------------------------------------------------------------------- 工具
LIST_FIELDS = (
    "clauses", "tables", "placeholders", "parties", "facts", "issues",
    "statutes", "cases", "knowledges", "risk_points", "missing_clauses",
    "citations", "evidence_gap", "tool_calls", "trace", "notes", "route", "strategy",
)


def state_summary(state: LegalState) -> dict[str, Any]:
    """给接口 / 前端返回的状态摘要（不泄露全文）。"""
    return {
        "task_id": state.get("task_id"),
        "intent": state.get("intent"),
        "domain": state.get("domain"),
        "doc_type": state.get("doc_type"),
        "steps": state.get("steps", 0),
        "route": state.get("route", []),
        "counts": {f: len(state.get(f) or []) for f in LIST_FIELDS},
        "citation_check": state.get("citation_check"),
        "confidence": state.get("confidence", 0.0),
        "evidence_gap": len(state.get("evidence_gap") or []),
        "draft_length": len(state.get("draft") or ""),
    }


def as_dicts(items: list[Any]) -> list[dict[str, Any]]:
    """pydantic 实体列表 -> dict 列表（写回 State 用）。"""
    out: list[dict[str, Any]] = []
    for it in items:
        if isinstance(it, BaseModel):
            out.append(it.model_dump(mode="json"))
        elif isinstance(it, dict):
            out.append(it)
    return out


def as_models(items: list[dict[str, Any]], cls: type[BaseModel]) -> list[Any]:
    """dict 列表 -> pydantic 实体列表（读 State 用）。"""
    out: list[Any] = []
    for it in items or []:
        try:
            out.append(cls.model_validate(it))
        except Exception:
            continue
    return out
