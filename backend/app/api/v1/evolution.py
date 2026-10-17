"""Skill 自进化飞轮 API（L0 观测 → L5 治理）。

路由按飞轮的层分块排，与 ``evolution_service`` 的分层注释一一对应，
便于"看接口就知道系统分几层"。

两条鉴权纪律：

1. **读全部 ``RequireUser``**：律师需要能看见自己的 Skill 退化到什么程度；
   把可观测性也锁给管理员，结果是没人发现问题。
2. **写全部 ``RequireAdmin``**：创建任务、推进灰度、Keep/Revert、熔断，
   每一个都会改变生产行为，必须是人（管理员）按下去的动作。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from backend.app.api.deps import RequireAdmin, RequireUser
from backend.app.services.evolution_service import evolution_service

router = APIRouter(prefix="/evolution", tags=["Skill 自进化"])


# ================================================================ 读接口短缓存 + ETag
# 监控中心会以秒级轮询 /overview 与 /north-star，而这两个是全服务最重的读接口
# （overview 对每个 Skill 各跑一次健康诊断，内含 PSI/KL/MMD 三检验）。
# 这里加一层**极短 TTL**的进程内缓存：
#   - TTL 内且状态未变 → 复用同一份快照，不重复计算；
#   - 带 If-None-Match 且内容未变 → 304，连响应体都不传。
# 失效判据不是"每个写接口记得来清缓存"（十几个写接口漏一个就出脏读），
# 而是 **state.json 的 (mtime, size) 签名**：任何进程写入都会使其变化，
# 跨进程（worker）的写也一并覆盖。
# 语义边界：TTL 内返回的是"几秒前的快照"，不是实时值；写操作改了 state，
# 签名随即变化，下一次请求就会重算。
_CACHE_TTL_SECONDS = 5.0
# 基础 key → (到期时刻 monotonic, 当时的 signature, payload, etag)
_read_cache: dict[str, tuple[float, str, Any, str]] = {}
_read_cache_lock = threading.Lock()


def _cached_read(key: str, signature: str, producer: Any) -> tuple[Any, str]:
    """返回 ``(payload, etag)``。TTL 内且签名一致则复用，否则重算并回填。

    整个重算过程持锁：宁可让并发请求排队，也不要 N 个请求同时各算一遍
    O(N) 次健康诊断 —— 那正是这层缓存要消除的开销。
    """
    now = time.monotonic()
    with _read_cache_lock:
        hit = _read_cache.get(key)
        if hit and hit[0] > now and hit[1] == signature:
            return hit[2], hit[3]
        payload = producer()
        etag = 'W/"' + hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:20] + '"'
        _read_cache[key] = (now + _CACHE_TTL_SECONDS, signature, payload, etag)
        return payload, etag


def _respond_cached(request: Request, response: Response, key: str, producer: Any) -> Any:
    """短缓存 + ETag 的读接口外壳。内容未变且客户端已缓存 → 304。"""
    payload, etag = _cached_read(key, evolution_service.state_signature(), producer)
    headers = {"ETag": etag, "Cache-Control": f"private, max-age={int(_CACHE_TTL_SECONDS)}"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return payload


# ================================================================ 请求模型
class ObservationIn(BaseModel):
    skill_name: str = Field(min_length=1, max_length=120)
    version: str = "unknown"
    verdict: str = "unknown"
    # verdict 标注的置信度：默认 1.0。低于 1 的标注会按比例削弱对健康分的影响。
    confidence: float = Field(default=1.0, ge=0, le=1)
    latency_ms: float = Field(default=0, ge=0)
    trace_id: str = ""
    task_id: str = ""
    error_type: str = ""
    coverage: float | None = Field(default=None, ge=0, le=1)
    freshness_days: float | None = Field(default=None, ge=0)
    last_gain: float | None = Field(default=None, ge=0, le=1)
    tool_calls: int = Field(default=0, ge=0)


class EvolutionTaskIn(BaseModel):
    skill_name: str = Field(min_length=1, max_length=120)
    base_version: str = "current"
    hypothesis: str = ""
    candidate_id: str = ""
    max_runs: int = Field(default=100, ge=1, le=10000)
    max_tokens: int = Field(default=200000, ge=1, le=10000000)


class ValidationIn(BaseModel):
    success_rate: float = Field(ge=0, le=1)
    p95_latency_ms: float = Field(ge=0)
    critical_failures: int = Field(default=0, ge=0)
    coverage: float = Field(default=0.85, ge=0, le=1)
    run_count: int = Field(default=100, ge=1)


class FreezeIn(BaseModel):
    reason: str = Field(default="人工熔断", max_length=200)


class ReviewIn(BaseModel):
    verdict: str = Field(pattern="^(approve|reject)$")
    note: str = Field(default="", max_length=500)


class SchedulerIn(BaseModel):
    """周期调度控制。两个字段都可选 —— 只传要改的那一个，另一个保持不变。"""

    enabled: bool | None = None
    # 上限一年：周期再长就不叫"周级循环"了，更可能是把单位填错（比如把秒当成了天）。
    interval_days: float | None = Field(default=None, gt=0, le=365)


# ================================================================ L0 数据观测
@router.post("/observations", summary="记录 Skill 运行观测")
def record_observation(body: ObservationIn, user: RequireUser) -> dict[str, Any]:
    try:
        return evolution_service.observe(body.model_dump(), user_id=user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/traces", summary="读取轨迹（trace store，按天分片）")
def list_traces(
    user: RequireUser,
    limit: int = Query(default=100, ge=1, le=500),
    day: str = Query(default="", description="YYYY-MM-DD，留空取当天"),
) -> dict[str, Any]:
    items = evolution_service.traces(limit=limit, day=day or None)
    return {"items": items, "count": len(items)}


@router.get("/sessions", summary="L0 会话聚合（session → turn → tool-call 三级）")
def list_trace_sessions(
    user: RequireUser,
    limit: int = Query(default=50, ge=1, le=200),
    day: str = Query(default="", description="YYYY-MM-DD，留空取当天"),
) -> dict[str, Any]:
    items = evolution_service.trace_sessions(limit=limit, day=day or None)
    return {"items": items, "count": len(items)}


@router.get("/features/{skill_name}", summary="L0 特征工程：窗口特征向量")
def skill_features(skill_name: str, user: RequireUser) -> dict[str, Any]:
    return evolution_service.features(skill_name)


@router.post("/window/{skill_name}/aggregate", summary="L0 窗口聚合：落 feature store 快照")
def aggregate_window(skill_name: str, user: RequireAdmin) -> dict[str, Any]:
    return evolution_service.aggregate_window(skill_name)


@router.get("/observations/stats", summary="L0 观测读数：判定分布 · 小时吞吐 · 数据延迟")
def observation_stats(
    user: RequireUser,
    skill_name: str = Query(default=""),
    hours: int = Query(default=24, ge=1, le=168),
) -> dict[str, Any]:
    return evolution_service.observation_stats(skill_name or None, hours=hours)


# ================================================================ L1 健康诊断
@router.get("/health/{skill_name}", summary="查看 Skill 健康诊断（四维画像 + 基线 + 漂移 + 先行信号）")
def skill_health(skill_name: str, user: RequireUser) -> dict[str, Any]:
    return evolution_service.health(skill_name)


@router.get("/drift/{skill_name}", summary="L1 三检验漂移（PSI / MMD / KL 投票）")
def skill_drift(skill_name: str, user: RequireUser) -> dict[str, Any]:
    health = evolution_service.health(skill_name)
    return {
        "skill_name": skill_name,
        "drift": health["drift"],
        "drift_score": health["drift_score"],
        "signals": health["signals"],
    }


@router.get("/early-warning/{skill_name}", summary="L1 先行退化信号")
def skill_early_warning(skill_name: str, user: RequireUser) -> dict[str, Any]:
    health = evolution_service.health(skill_name)
    return {"skill_name": skill_name, **health["early_warning"]}


@router.get("/overview", summary="Skill 自进化飞轮总览")
def evolution_overview(request: Request, response: Response, user: RequireUser) -> Any:
    return _respond_cached(request, response, "overview", evolution_service.overview)


# ================================================================ L2 进化决策
@router.get("/candidates/{skill_name}", summary="生成本周 Top-K 进化候选")
def evolution_candidates(
    skill_name: str,
    user: RequireUser,
    limit: int = Query(default=3, ge=1, le=3),
) -> dict[str, Any]:
    items = evolution_service.candidates(skill_name, limit)
    return {"items": items, "count": len(items)}


# ================================================================ L3 进化执行
@router.get("/failures/{skill_name}", summary="L3 失败场景切片（含证据包）")
def list_failure_cases(
    skill_name: str,
    user: RequireUser,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    items = evolution_service.failure_cases(skill_name, limit=limit)
    return {"items": items, "count": len(items)}


@router.get("/clusters/{skill_name}", summary="L3 失败聚类归并（可走 embedding 语义相似）")
def list_failure_clusters(
    skill_name: str,
    user: RequireUser,
    threshold: float = Query(default=0.6, ge=0, le=1),
    semantic: bool = Query(default=False, description="embedding 语义归并；首次调用会加载向量模型"),
) -> dict[str, Any]:
    items = evolution_service.cluster_failures(skill_name, threshold=threshold, semantic=semantic)
    return {"items": items, "count": len(items)}


@router.get("/root-cause/{skill_name}", summary="L3 根因分析（诊断 → 假设 → 证据校验）")
def skill_root_cause(
    skill_name: str,
    user: RequireUser,
    cluster_id: str = Query(default="", description="留空取样本最多的那一簇"),
) -> dict[str, Any]:
    return evolution_service.root_cause(skill_name, cluster_id=cluster_id or None)


# ================================================================ L4 验证守门
@router.get("/preflight/{skill_name}", summary="三道防线·事前：漂移预演（灰度前的守门）")
def skill_preflight(skill_name: str, user: RequireUser) -> dict[str, Any]:
    return evolution_service.preflight(skill_name)


@router.post("/tasks", summary="创建进化任务")
def create_evolution_task(body: EvolutionTaskIn, user: RequireAdmin) -> dict[str, Any]:
    try:
        return evolution_service.create_task(body.model_dump(), user_id=user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/tasks", summary="进化任务列表")
def list_evolution_tasks(user: RequireUser, limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    items = evolution_service.list_tasks(limit)
    return {"items": items, "count": len(items)}


@router.post("/tasks/{task_id}/run", summary="执行候选验证（同基线回放 + 指标门禁）")
def run_evolution_task(task_id: str, body: ValidationIn, user: RequireAdmin) -> dict[str, Any]:
    try:
        task = evolution_service.run_task(task_id, body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if task is None:
        raise HTTPException(status_code=404, detail="进化任务不存在")
    return task


@router.get("/tasks/{task_id}/replay", summary="L4 同基线回放 A/B 差分对照")
def replay_evolution_task(task_id: str, user: RequireUser) -> dict[str, Any]:
    replay = evolution_service.replay(task_id)
    if replay is None:
        raise HTTPException(status_code=404, detail="任务不存在或尚未完成回放验证")
    return replay


@router.post("/tasks/{task_id}/micro-cycle", summary="内环·亚周级微循环：单个候选 根因 → 回放")
def run_evolution_micro_cycle(task_id: str, body: ValidationIn, user: RequireAdmin) -> Any:
    """把 ``EvolutionService.micro_cycle`` 接出来。

    它此前只在服务层定义、没有任何调用方（既没有接口也没有脚本），
    "单候选快速迭代"这条内环实际上是悬空的。入参与 ``/tasks/{id}/run``
    一致 —— 微循环就是"只跑一个候选"的回放 + 门禁，外加一次根因分析。
    """
    result = evolution_service.micro_cycle(task_id, body.model_dump())
    if result is None:
        raise HTTPException(status_code=404, detail="进化任务不存在")
    return result


@router.post("/tasks/{task_id}/rollout/start", summary="L4 启动三阶段灰度（影子模式）")
def start_evolution_rollout(task_id: str, user: RequireAdmin) -> dict[str, Any]:
    try:
        task = evolution_service.start_rollout(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if task is None:
        raise HTTPException(status_code=404, detail="进化任务不存在")
    return task


@router.post("/tasks/{task_id}/rollout/advance", summary="L4 推进灰度（影子 → 小流量 → 全量）")
def advance_evolution_rollout(task_id: str, body: ValidationIn, user: RequireAdmin) -> dict[str, Any]:
    try:
        task = evolution_service.advance_rollout(task_id, body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if task is None:
        raise HTTPException(status_code=404, detail="进化任务不存在")
    return task


@router.get("/tasks/{task_id}", summary="进化任务详情")
def get_evolution_task(task_id: str, user: RequireUser) -> dict[str, Any]:
    task = evolution_service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="进化任务不存在")
    return task


@router.post("/tasks/{task_id}/decision", summary="Keep 或 Revert")
def decide_evolution_task(
    task_id: str,
    user: RequireAdmin,
    decision: str = Query(..., pattern="^(keep|revert)$"),
) -> dict[str, Any]:
    try:
        task = evolution_service.decide(task_id, decision)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if task is None:
        raise HTTPException(status_code=404, detail="进化任务不存在")
    return task


@router.get("/ledger", summary="L4 版本账本（只进不退，回滚留痕）")
def evolution_ledger(
    user: RequireUser,
    skill_name: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    items = evolution_service.version_ledger(skill_name or None, limit=limit)
    return {"items": items, "count": len(items)}


# ================================================================ L5 状态与治理
@router.post("/cycle", summary="L5 外环：跑一轮周级主循环（L0 → L5）")
def run_evolution_cycle(user: RequireAdmin, max_skills: int = Query(default=3, ge=1, le=10)) -> dict[str, Any]:
    try:
        return evolution_service.run_cycle(max_skills=max_skills)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/scheduler", summary="L5 元控制：开关周期调度 / 调整进化周期")
def update_evolution_scheduler(body: SchedulerIn, user: RequireAdmin) -> dict[str, Any]:
    """在线控制进程内周期调度器。

    只管 state 里的 ``scheduler`` 段（``EVOLUTION_SCHEDULER_ENABLED`` 那个环境开关
    是运维的急停钮，不在接口可控范围内 —— 接口能改的不该包括"把自己关掉"）。
    """
    try:
        return evolution_service.set_scheduler(
            enabled=body.enabled, interval_days=body.interval_days,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/refresh", summary="L5 状态刷新：重算基线 + results.tsv 落盘")
def refresh_evolution_state(user: RequireAdmin) -> dict[str, Any]:
    return evolution_service.refresh_state()


@router.get("/meta", summary="L5 元控制：进化进度与预算配额")
def evolution_meta(user: RequireUser) -> dict[str, Any]:
    return evolution_service.meta()


@router.get("/audit", summary="L5 人工巡检审计台（决策 / 账本 / 抽查合成时间线）")
def evolution_audit(
    user: RequireUser,
    skill_name: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    return evolution_service.audit_log(skill_name=skill_name or None, limit=limit)


@router.get("/reviews", summary="HITL 人工抽查队列")
def list_evolution_reviews(
    user: RequireUser,
    status: str = Query(default="", description="pending / resolved，留空取全部"),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    items = evolution_service.list_reviews(status=status or None, limit=limit)
    return {"items": items, "count": len(items)}


@router.post("/reviews/{review_id}", summary="HITL 填写抽查结论（approve / reject）")
def resolve_evolution_review(review_id: str, body: ReviewIn, user: RequireAdmin) -> dict[str, Any]:
    try:
        review = evolution_service.resolve_review(
            review_id, body.verdict, note=body.note, user_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if review is None:
        raise HTTPException(status_code=404, detail="抽查任务不存在")
    return review


@router.get("/north-star", summary="北极星指标")
def evolution_north_star(request: Request, response: Response, user: RequireUser) -> Any:
    return _respond_cached(request, response, "north-star", evolution_service.north_star)


@router.post("/freeze", summary="安全红线：人工一键熔断")
def freeze_evolution(body: FreezeIn, user: RequireAdmin) -> dict[str, Any]:
    return evolution_service.freeze(body.reason, user_id=user.id)


@router.post("/unfreeze", summary="解除熔断")
def unfreeze_evolution(user: RequireAdmin) -> dict[str, Any]:
    return evolution_service.unfreeze(user_id=user.id)
