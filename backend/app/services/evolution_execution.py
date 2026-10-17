"""L2 候选排序与 L3 进化执行层。

对应「Skill 自进化飞轮」图：

- L2 **帕累托前沿**：四目标按支配排序，不可支配者全收
- L3 **失败场景回滚**：灰事实（对照成功轨迹）定位关键步骤，切片轨迹 + 证据包
- L3 **失败聚类**：embedding 语义相似归并，无向量时降级字符集并如实标注
- L3 **根因分析（离线 LLM）**：诊断 / 假设 / 证据校验三个角色独立产出，
  校验角色有权否掉全部假设
- L3 **补丁生成**：单点定向 · 最小可审计 diff，禁全局重写

本模块同样只做纯计算：输入是已读出来的观测/轨迹，输出是候选、聚类、
补丁内容。**不读盘、不写盘**，落盘由 ``evolution_service`` 统一负责 ——
这样"能不能生成补丁"和"补丁有没有写进生产"是两件可以分别验证的事。

**关于"离线"**：根因分析不占用在线请求的模型预算与时间窗口。这里通过
``llm`` 参数注入一个可调用对象（默认 None = 走确定性规则），注入与否由
调用方决定，模块自身不 import 任何模型客户端。
"""

from __future__ import annotations

import uuid
from collections import Counter
from typing import Any, Callable, Iterable, Sequence

from backend.app.services.evolution_embedding import (
    DEFAULT_EMBEDDING_THRESHOLD,
    cosine_similarity,
)
from backend.app.services.evolution_features import extract_param_keys

# ------------------------------------------------------------------ L2
# 四目标。顺序即"报告顺序"，不表示权重。
PARETO_OBJECTIVES = ("quality", "latency", "coverage", "cost")

# 候选预算：本周最多产出多少个候选（图 L2「补足至预算 K=3」）。
CANDIDATE_BUDGET = 3


def pareto_frontier(
    items: Sequence[dict[str, Any]],
    *,
    objectives: Sequence[str] = PARETO_OBJECTIVES,
) -> list[dict[str, Any]]:
    """返回帕累托前沿：不被任何其他候选**在所有目标上不劣、且至少一项更优**的项。

    缺目标时按 1.0 补齐（中位水平）。这是刻意为之：老的三目标候选没有
    ``cost``，若缺失按 0 处理，它们会被任何带 cost 的新候选支配掉 ——
    "缺数据"变成"考核不合格"，进化会整体偏向新格式候选。
    """
    def score(item: dict[str, Any], key: str) -> float:
        objectives_map = item.get("objectives") or {}
        value = objectives_map.get(key, 1.0)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 1.0

    def dominates(left: dict[str, Any], right: dict[str, Any]) -> bool:
        no_worse = all(score(left, k) >= score(right, k) for k in objectives)
        strictly_better = any(score(left, k) > score(right, k) for k in objectives)
        return no_worse and strictly_better

    return [
        item for item in items
        if not any(dominates(other, item) for other in items if other is not item)
    ]


# ------------------------------------------------------------------ L3 补丁模板
# 每个补丁模板**只改一个点**（个别模板改两处，且必须同属一个配置族）。
# 图里写的是"单点定向·最小可审计 diff，禁全局重写"：改得越少，回滚越干净，
# 也越容易判断"失败到底是这个改动造成的，还是别的东西"。
PATCH_TEMPLATES: dict[str, list[dict[str, Any]]] = {
    "failure_clustering": [
        {
            "path": "skills.{skill}.config.failure_fallback",
            "op": "set",
            "before": "silent_default",
            "after": "raise_with_evidence_gap",
            "reason": "失败模式已聚类，收紧默认回落：宁可显式声明证据缺口，也不用兜底结果冒充成功。",
        },
    ],
    "timeout_budget": [
        {
            "path": "skills.{skill}.config.timeout_ms",
            "op": "scale",
            "factor": 1.5,
            "reason": "P95 已越门禁，先给超时预算，不动检索逻辑（改逻辑的影响面大得多）。",
        },
    ],
    "retrieval_coverage": [
        {
            "path": "skills.{skill}.config.retrieve_top_k",
            "op": "set",
            "before": 20,
            "after": 30,
            "reason": "覆盖广度不足，先扩召回；不重排、不改融合权重，便于归因。",
        },
    ],
    "routing_threshold": [
        {
            "path": "skills.{skill}.config.route_threshold",
            "op": "set",
            "before": 0.60,
            "after": 0.68,
            "reason": "近期分布已漂移，按新分布重估路由阈值。",
        },
    ],
    "knowledge_refresh": [
        {
            "path": "skills.{skill}.config.knowledge_snapshot",
            "op": "refresh",
            "before": "pinned",
            "after": "latest",
            "reason": "staleness 超阈值，刷新知识快照与版本标签。",
        },
    ],
    "guardrail_tuning": [
        {
            "path": "skills.{skill}.config.guardrail_strictness",
            "op": "set",
            "before": "lenient",
            "after": "strict",
            "reason": "人工巡检发起的保守调整：只收紧门禁，不放开任何权限。",
        },
    ],
}

# ---------------------------------------------------------------- 安全红线
# 进化**永远不能**碰的东西（图 L5「禁改名单·权限最小化」）。
# 用子串匹配而不是精确路径：进化补丁的 path 是逻辑配置路径
# （``skills.x.config.y``），不是文件路径，精确匹配管不住写法变体。
FORBIDDEN_TOKENS: tuple[str, ...] = (
    "auth", "permission", "security", "secret", "token", "password", "audit",
    "deps", "database", "config.py", "middleware", "admin",
)


def patch_changes(patch_type: str, *, skill_name: str = "") -> list[dict[str, Any]]:
    """按补丁类型产出最小变更集（模板里的 ``{skill}`` 会被替换成实际 Skill 名）。"""
    template = PATCH_TEMPLATES.get(patch_type) or PATCH_TEMPLATES["guardrail_tuning"]
    return [
        {key: (value.replace("{skill}", skill_name) if isinstance(value, str) else value)
         for key, value in change.items()}
        for change in template
    ]


def guard_changes(changes: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """安全红线校验：命中禁改名单的补丁**直接判定不合法**。

    返回 ``{"ok": bool, "violations": [...]}``。调用方必须把 ``ok=False``
    视为硬失败（拒绝落盘），而不是打个警告继续 —— 能改权限的补丁哪怕
    "只是看起来像"，也不该被写进版本库。
    """
    violations: list[dict[str, str]] = []
    for change in changes:
        path = str(change.get("path") or "")
        lowered = path.lower()
        for token in FORBIDDEN_TOKENS:
            if token in lowered:
                violations.append({"path": path, "token": token,
                                   "detail": f"补丁路径命中禁改名单（{token}），不允许生成。"})
                break
    return {"ok": not violations, "violations": violations}


# ------------------------------------------------------------------ L3 失败切片
def slice_failure_case(
    trace: dict[str, Any],
    *,
    skill_name: str,
    skill_tools: Iterable[str],
    success_traces: Sequence[dict[str, Any]] = (),
) -> dict[str, Any] | None:
    """从一条运行轨迹里切出某个 Skill 的失败场景（灰事实定位关键步骤）。

    切的是"关键步骤前后各一步"而不是整条轨迹：整条的体积是 O(任务长度)，
    存进证据包后人工根本看不完；而定位失败**只**需要出事那一步和它的上下文。

    ``success_traces`` 是**成功的同类轨迹**，用来做灰事实对照 ——
    只盯着失败轨迹，能说出的只有"这一步错了"；有了成功轨迹才说得出
    "这一步为什么错"（它用的工具在成功轨迹里从没出现过，或者步序已经
    超出成功轨迹的上界）。没有对照时退化为"第一个失败步骤"，
    并在 ``located_by`` 里如实标明定位依据，不让两种结论长得一样。
    """
    if not isinstance(trace, dict):
        return None
    tools = {str(t) for t in skill_tools}
    steps = [s for s in (trace.get("steps") or []) if isinstance(s, dict) and str(s.get("tool") or "") in tools]
    if not steps:
        return None

    failed = next((s for s in steps if not s.get("ok") or s.get("degraded")), None)
    if failed is None:
        return None  # 没有失败步骤的轨迹不是失败场景

    index = _step_of(failed, 0)
    sliced = [s for s in steps if max(0, index - 1) <= _step_of(s, 0) <= index + 1]

    error_type = str(failed.get("error") or "").strip() or (
        "degraded" if failed.get("degraded") else "tool_failure"
    )
    verdict = "degraded" if failed.get("degraded") and failed.get("ok") else "fail"
    grey_fact = _grey_fact(steps, index, success_traces, tools)
    case_id = "case_" + uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"{trace.get('trace_id')}:{skill_name}:{index}:{error_type}",
    ).hex[:12]
    return {
        "case_id": case_id,
        "trace_id": str(trace.get("trace_id") or ""),
        "task_id": str(trace.get("task_id") or ""),
        # session / turn 是图 L0 明确要求的三级之一（session → turn → tool-call），
        # 缺了它们就没法把"同一个会话里第 3 轮开始变差"这类现象归拢起来。
        "session_id": str(trace.get("session_id") or ""),
        "turn_index": int(trace.get("turn_index") or 0),
        "skill_name": skill_name,
        "verdict": verdict,
        "key_step": {
            "step": index,
            "tool": str(failed.get("tool") or ""),
            "agent": str(failed.get("agent") or ""),
            "error": error_type,
            "degraded": bool(failed.get("degraded")),
            "retries": int(failed.get("retries") or 0),
            "located_by": ("grey_fact_divergence" if grey_fact["divergences"]
                           else "first_failed_or_degraded_step"),
        },
        "grey_fact": grey_fact,
        "sliced_steps": [
            {
                "step": _step_of(s, 0),
                "tool": str(s.get("tool") or ""),
                "agent": str(s.get("agent") or ""),
                "ok": bool(s.get("ok")),
                "degraded": bool(s.get("degraded")),
                "elapsed_ms": float(s.get("elapsed_ms") or 0.0),
                "retries": int(s.get("retries") or 0),
            }
            for s in sliced
        ],
        "evidence": {
            "error_type": error_type,
            "degraded": bool(failed.get("degraded")),
            "retries": int(failed.get("retries") or 0),
            "elapsed_ms": float(failed.get("elapsed_ms") or 0.0),
            # 参数**键名**从 args_summary 里抽，值一律丢弃（见 extract_param_keys）
            "param_keys": extract_param_keys(failed.get("args_summary")),
            "trace_elapsed_ms": float(trace.get("elapsed_ms") or 0.0),
        },
        "created_at": str(trace.get("started_at") or ""),
    }


def _grey_fact(
    steps: Sequence[dict[str, Any]],
    failed_index: int,
    success_traces: Sequence[dict[str, Any]],
    tools: set[str],
) -> dict[str, Any]:
    """灰事实：拿成功轨迹当对照，说清失败那一步「哪里不像正常的一步」。"""
    success_steps = [
        step
        for trace in success_traces
        for step in (trace.get("steps") or [])
        if isinstance(step, dict) and str(step.get("tool") or "") in tools
    ]
    if not success_steps:
        return {"available": False, "divergences": [],
                "reason": "没有成功的同类轨迹可比对，只能按'第一个失败步骤'定位"}

    success_tools = {str(step.get("tool") or "") for step in success_steps}
    upper = max((_step_of(step) for step in success_steps), default=0)
    failed_tool = next(
        (str(step.get("tool") or "") for step in steps if _step_of(step) == failed_index),
        "",
    )

    divergences: list[str] = []
    if failed_tool and failed_tool not in success_tools:
        divergences.append(f"失败步骤用的工具 {failed_tool} 在成功轨迹里从未出现")
    if failed_index > upper:
        divergences.append(f"失败发生在第 {failed_index} 步，而成功轨迹最多只走到第 {upper} 步")

    return {
        "available": True,
        "divergences": divergences,
        "success_step_upper": upper,
        "success_tool_count": len(success_tools),
        "failed_index": failed_index,
        "failed_tool": failed_tool,
        "success_reference_count": len(success_traces),
    }


# ------------------------------------------------------------------ L3 失败聚类
def failure_signature(case: dict[str, Any]) -> frozenset[str]:
    """失败指纹：Skill + 错误类型 + 涉及工具。用于相似度归并。"""
    evidence = case.get("evidence") or {}
    tokens = {str(case.get("skill_name") or ""), str(evidence.get("error_type") or "unknown")}
    tokens |= {str(step.get("tool") or "") for step in (case.get("sliced_steps") or [])}
    return frozenset(token for token in tokens if token)


def _step_of(step: dict[str, Any], fallback: int = 0) -> int:
    """取工具调用序号。真实字段是 ``step``（见 ``ToolCallRecord``）；
    ``index`` 只是本模块早期笔误的兜底，保留它是为了读得动已经落盘的老轨迹。"""
    for key in ("step", "index"):
        if key in step:
            try:
                return int(step[key])
            except (TypeError, ValueError):
                continue
    return int(fallback)


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left and not right:
        return 1.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def failure_case_text(case: dict[str, Any]) -> str:
    """失败案例的文本表示 —— embedding 归并的输入。

    刻意只放**结构性**字段（Skill / 错误类型 / 工具序列 / 关键步骤），不放
    参数值：一是法律场景的参数里可能带当事人信息，二是参数值本身噪声很大，
    放进去会让"语义完全相同、参数不同"的两条失败被算成不相似。
    """
    evidence = case.get("evidence") or {}
    tools = " ".join(str(step.get("tool") or "") for step in (case.get("sliced_steps") or []))
    return " ".join(filter(None, [
        str(case.get("skill_name") or ""),
        str(evidence.get("error_type") or ""),
        str((case.get("key_step") or {}).get("tool") or ""),
        tools,
    ]))


def cluster_failures(
    cases: Sequence[dict[str, Any]],
    *,
    threshold: float = 0.6,
    embedder: Callable[[Sequence[str]], list[list[float]] | None] | None = None,
    embedding_threshold: float = DEFAULT_EMBEDDING_THRESHOLD,
) -> list[dict[str, Any]]:
    """相似失败归并（单链聚类）。

    **单链而不是 K-Means**：簇数事先不知道，而且"一个簇"本身就可能是 1 条 ——
    K-Means 会把单例硬塞进某个簇，导致新出现的失败模式被合并进旧簇，
    正好丢掉最该看的那条。

    **两条相似度路径**：给了 ``embedder`` 就走语义（图 L3 要求的
    "embedding 相似失败归并"），否则回退到字符集 Jaccard。哪条生效**写在
    结果里**（``similarity`` 字段）—— 两种算法的阈值不可通用（余弦普遍 0.8+、
    Jaccard 多在 0.5~0.7），混着看会把结论读歪。
    """
    vectors: list[list[float]] | None = None
    if embedder is not None and cases:
        try:
            vectors = embedder([failure_case_text(case) for case in cases])
        except Exception:  # noqa: BLE001 - 向量化失败即回退，不阻断聚类
            vectors = None
        if vectors is not None and len(vectors) != len(cases):
            vectors = None

    signatures = [failure_signature(case) for case in cases]
    if vectors is not None:
        similarity_mode = "embedding"
        limit = embedding_threshold

        def similar(left: int, right: int) -> bool:
            return cosine_similarity(vectors[left], vectors[right]) >= limit
    else:
        similarity_mode = "jaccard"
        limit = threshold

        def similar(left: int, right: int) -> bool:
            return jaccard(signatures[left], signatures[right]) >= limit

    parent = list(range(len(cases)))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for i in range(len(cases)):
        for j in range(i + 1, len(cases)):
            if similar(i, j):
                union(i, j)

    grouped: dict[int, list[int]] = {}
    for index in range(len(cases)):
        grouped.setdefault(find(index), []).append(index)

    clusters: list[dict[str, Any]] = []
    for members in grouped.values():
        member_cases = [cases[i] for i in members]
        tokens = sorted({token for i in members for token in signatures[i]})
        error_types = Counter(str((c.get("evidence") or {}).get("error_type") or "unknown") for c in member_cases)
        skills = Counter(str(c.get("skill_name") or "") for c in member_cases)
        clusters.append({
            "cluster_id": "clu_" + uuid.uuid5(uuid.NAMESPACE_URL, "|".join(tokens)).hex[:12],
            "size": len(member_cases),
            "skill_name": skills.most_common(1)[0][0],
            "error_type": error_types.most_common(1)[0][0],
            "signature": tokens,
            "case_ids": [str(c.get("case_id")) for c in member_cases],
            "representative": member_cases[0],
            "similarity": similarity_mode,
            "similarity_threshold": limit,
            "created_at": min((str(c.get("created_at") or "") for c in member_cases if c.get("created_at")), default=""),
        })
    return sorted(clusters, key=lambda item: (-item["size"], item["cluster_id"]))


# ------------------------------------------------------------------ L3 根因分析
# 错误类型 → (根因表述, 候选假设, 建议的补丁类型)。
ROOT_CAUSE_RULES: dict[str, tuple[str, list[str], str]] = {
    "tool_failure": ("上游工具本身调用失败，尚未进入业务判断",
                     ["外部依赖抖动（重试即可）", "参数构造与工具契约不符", "超时预算偏紧"],
                     "failure_clustering"),
    "timeout": ("调用在超时预算内跑不完",
                ["超时预算低于该 Skill 的真实 P95", "上游检索变慢", "重试叠加放大了墙钟时间"],
                "timeout_budget"),
    "degraded": ("链路降级交付，结果可用但证据不全",
                 ["某一路召回为空导致降级", "外部模型不可用触发确定性回落"],
                 "retrieval_coverage"),
    "coverage_gap": ("召回覆盖不足，证据链有缺口",
                     ["检索 top_k 偏小", "索引未覆盖新语料", "查询改写丢掉了关键限定词"],
                     "retrieval_coverage"),
    "distribution_drift": ("输入分布相对基线发生漂移，旧阈值不再贴合",
                           ["上游输入源变化", "阈值长期未重估"],
                           "routing_threshold"),
    "staleness": ("知识快照过期", ["知识库未随法规更新刷新"], "knowledge_refresh"),
}

# 错误类型里带这些词时归到上面某一条（观测里的 error_type 是自由文本）。
_ERROR_ALIASES = (
    ("timeout", "timeout"),
    ("drift", "distribution_drift"),
    ("stale", "staleness"),
    ("coverage", "coverage_gap"),
    ("degrad", "degraded"),
)


def _canonical_error_type(raw: str) -> str:
    lowered = (raw or "").strip().lower()
    for needle, canonical in _ERROR_ALIASES:
        if needle in lowered:
            return canonical
    return lowered or "tool_failure"


# 多智能体角色。三者各自独立产出，``validator`` 有权否掉 ``hypothesizer``。
AGENT_ROLES: tuple[tuple[str, str], ...] = (
    ("diagnostician", "现象诊断"),
    ("hypothesizer", "假设生成"),
    ("validator", "证据校验"),
)


def diagnose_cluster(
    cluster: dict[str, Any],
    cases: Sequence[dict[str, Any]],
    *,
    llm: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """离线根因分析：诊断 → 假设 → 证据校验（图 L3「多智能体协作」）。

    三个角色**分开产出**，且 ``validator`` 有权否掉 ``hypothesizer`` 的全部
    假设 —— 这是多智能体在这里唯一说得通的理由。如果三个角色由一次调用
    连着吐完，"证据校验"就只是把前两段换个说法再说一遍，永远不会否决，
    结果是在**证据不足时照样产出一串看着挺像样的根因**，然后据此生成补丁。

    ``llm`` 由调用方注入；它抛任何异常都静默回退规则引擎 —— 离线增强挂掉
    不该让整轮进化停摆。回退事实写在 ``engine`` 字段里，不藏着。
    """
    error_type = _canonical_error_type(str(cluster.get("error_type") or ""))
    total_ms = sum(float((case.get("evidence") or {}).get("elapsed_ms") or 0.0) for case in cases)
    payload = {
        "cluster_id": cluster.get("cluster_id"),
        "skill_name": cluster.get("skill_name"),
        "error_type": error_type,
        "size": cluster.get("size"),
        "signature": cluster.get("signature"),
        "cases": [
            {
                "case_id": case.get("case_id"),
                "key_step": case.get("key_step"),
                "grey_fact": case.get("grey_fact"),
                "evidence": case.get("evidence"),
                "sliced_steps": case.get("sliced_steps"),
            }
            for case in cases[:5]
        ],
        "roles": [role for role, _ in AGENT_ROLES],
    }

    finding = ""
    hypotheses: list[str] = []
    patch_type: str | None = None
    engine = "rule"
    model_hint = ""

    if llm is not None:
        try:
            suggested = llm(payload) or {}
            finding = str(suggested.get("finding") or "").strip()
            hypotheses = [str(x) for x in (suggested.get("hypotheses") or []) if str(x).strip()]
            if finding and hypotheses:
                engine = "llm"
                model_hint = str(suggested.get("model") or "")
                patch_type = str(suggested.get("patch_type") or "").strip() or None
        except Exception:  # noqa: BLE001 - 离线增强，失败即回退规则
            finding, hypotheses, engine = "", [], "rule"

    if not finding or not hypotheses:
        finding, hypotheses, rule_patch = ROOT_CAUSE_RULES.get(
            error_type, ROOT_CAUSE_RULES["tool_failure"],
        )
        engine = "rule"
        patch_type = rule_patch

    # 补丁类型必须落在模板白名单里：模型给出的自由文本不能直接当补丁类型用，
    # 否则 patch_changes 会静默回落到 guardrail_tuning，生成的补丁与根因对不上。
    if patch_type not in PATCH_TEMPLATES:
        patch_type = ROOT_CAUSE_RULES.get(error_type, ROOT_CAUSE_RULES["tool_failure"])[2]

    return _diagnosis_result(
        cluster, error_type, finding, hypotheses,
        engine=engine, model_hint=model_hint,
        evidence_ms=total_ms, case_count=len(cases),
        patch_type=patch_type,
    )


def _diagnosis_result(
    cluster: dict[str, Any],
    error_type: str,
    finding: str,
    hypotheses: Sequence[str],
    *,
    engine: str,
    model_hint: str,
    evidence_ms: float,
    case_count: int,
    patch_type: str | None = None,
) -> dict[str, Any]:
    resolved_patch = patch_type or ROOT_CAUSE_RULES.get(
        error_type, ROOT_CAUSE_RULES["tool_failure"],
    )[2]
    evidence = [
        f"该族共 {case_count} 条失败，关键步骤累计耗时 {evidence_ms:.0f}ms。",
        f"错误类型：{error_type}。",
        f"失败指纹：{' / '.join(cluster.get('signature') or [])}。",
    ]
    checked = validate_hypotheses(
        hypotheses, case_count=case_count, evidence_ms=evidence_ms,
    )
    admitted = set(checked["kept"])

    priors = [round(1.0 / (1.0 + index), 4) for index in range(len(hypotheses))]
    total = sum(priors) or 1.0
    return {
        "cluster_id": cluster.get("cluster_id"),
        "skill_name": cluster.get("skill_name"),
        "engine": engine,
        "model_hint": model_hint,
        # 三个角色的产出**各自留痕**，而不是揉成一个结论：出问题时能看出
        # 是哪一环跑偏了（现象描述错、假设跑偏、还是校验放水）。
        "agents": [
            {"role": "diagnostician", "name": "现象诊断", "engine": engine,
             "output": {"finding": finding, "evidence": evidence}},
            {"role": "hypothesizer", "name": "假设生成", "engine": engine,
             "output": {"hypotheses": list(hypotheses)}},
            {"role": "validator", "name": "证据校验", "engine": engine,
             "output": checked},
        ],
        "diagnosis": {"finding": finding, "evidence": evidence},
        "hypotheses": [
            {"hypothesis": text, "prior": round(prior / total, 4),
             "verification": "在同基线回放里逐条对照，落败的假设从候选池剔除。",
             "admitted": text in admitted}
            for text, prior in zip(hypotheses, priors)
        ],
        # 证据校验：进补丁前先自问"这个结论有没有证据撑着"。
        "evidence_check": checked,
        "patch_type": resolved_patch,
    }


def validate_hypotheses(
    hypotheses: Sequence[str],
    *,
    case_count: int,
    evidence_ms: float,
) -> dict[str, Any]:
    """证据校验角色：**没有可复现样本就否掉全部假设**。

    这是三个角色里唯一有权说"不"的那个。允许它整体否决，是为了拦住
    "聚类出了一个名字，就顺着这个名字编出根因和补丁"这条最危险的路径 ——
    名字是算法给的，证据得是样本给的，两者不能混为一谈。
    """
    has_evidence = case_count > 0 and evidence_ms > 0
    return {
        "role": "validator",
        "passed": has_evidence,
        "case_count": case_count,
        "evidence_ms": round(evidence_ms, 3),
        "kept": list(hypotheses) if has_evidence else [],
        "dropped": [] if has_evidence else list(hypotheses),
        "missing": [] if has_evidence else ["没有任何可复现的失败样本"],
        "statement": ("证据充分：有可复现样本且带关键步骤耗时，假设全部进入候选池。"
                      if has_evidence
                      else "证据不足：只有聚类结论、没有可复现样本，本轮假设全部否掉，不生成补丁。"),
    }
