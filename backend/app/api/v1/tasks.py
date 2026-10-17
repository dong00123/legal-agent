"""任务接口（方案 FR13 双端任务互通 + 任务中心）。

要点：

1. **列表不带 Trace / State。** 一次拉 50 条任务，如果每条都带上完整 Trace
   （实测单任务可达 70+ 条事件）与 State 快照（含合同全文，单条可达数百 KB），
   响应体会直接膨胀到几十 MB，而列表页只显示状态与进度。
   需要细节时逐个调 ``GET /tasks/{task_id}``。这一点与
   ``task_service.list`` 的 ``with_trace=False`` 是同一条约定。

2. **任务是只读的。** 没有删除接口 —— 任务快照与 Trace 是"这份报告当时是怎么出来的"
   的唯一记录。要清理只能由运维直接操作数据库，而不是提供一个按钮。

3. **越权判定在服务层之后再做一次。** ``task_service`` 只管存储、不做权限，
   因为它也被服务端内部（归档、统计）使用。所以权限判定放在这里：
   非管理员只能看自己的任务或本团队的任务。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.app.api.deps import Page, RequireUser, page_of
from backend.app.services import citation_service

# **必须导入 ``task_service`` 这个实例，不是同名模块。**
# ``from backend.app.services import task_service`` 拿到的是**模块对象**，
# 而 ``get`` / ``list`` / ``count`` / ``stats`` 都是 ``TaskService`` 的方法 ——
# 模块上根本没有这些属性，任何一个任务接口都会在运行时抛
# ``AttributeError: module ... has no attribute 'get'``。
# 这个错在模块导入期不会暴露，只在第一次真实调用时出现。
from backend.app.services.task_service import task_service

logger = logging.getLogger("legal_agent.api.tasks")

router = APIRouter(prefix="/tasks", tags=["任务中心"])


def _assert_task_access(user: Any, row: dict[str, Any]) -> None:
    """任务可见性：管理员全通；律师限本人或本团队。

    ``user_id`` 为空（服务端脚本/worker 发起、没挂账号）的任务**只对管理员可见** ——
    这类任务可能含未脱敏的内部调试内容，不该出现在普通律师的列表里。
    """
    if user.is_admin:
        return
    if row.get("user_id") is not None and int(row["user_id"]) == int(user.id):
        return
    tid = row.get("team_id")
    if tid is not None and user.team_id is not None and int(tid) == int(user.team_id):
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权查看该任务")


@router.get("", summary="任务列表")
def list_tasks(
    user: RequireUser,
    page: Page,
    status_filter: Annotated[str, Query(alias="status", description="按状态过滤")] = "",
    kind: Annotated[str, Query(description="按类型过滤")] = "",
    intent: Annotated[str, Query(description="按意图过滤")] = "",
    case_id: Annotated[str, Query(description="按案件过滤")] = "",
) -> dict[str, Any]:
    """任务列表（按更新时间倒序，不含 Trace 与 State）。"""
    if user.is_admin:
        rows = task_service.list(status=status_filter, kind=kind, case_id=case_id,
                                 limit=page.limit, offset=page.offset)
        if intent:
            rows = [r for r in rows if r.get("intent") == intent]
    else:
        # 非管理员一次拉「本人 + 本团队」两批再合并去重。
        # 不做成一次 SQL 是因为 task_service.list 每次只接受一个归属条件，
        # 与其在接口层拼 SQL（绕过服务层），不如多查一次再按 task_id 去重。
        mine = task_service.list(user_id=user.id, status=status_filter, kind=kind,
                                 case_id=case_id, limit=page.limit)
        team = (task_service.list(team_id=user.team_id, status=status_filter, kind=kind,
                                  case_id=case_id, limit=page.limit)
                if user.team_id else [])
        seen: set[str] = set()
        rows = []
        for r in mine + team:
            if r["task_id"] in seen:
                continue
            seen.add(r["task_id"])
            rows.append(r)
        if intent:
            rows = [r for r in rows if r.get("intent") == intent]
        rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
        rows = rows[page.offset: page.offset + page.limit]

    total = task_service.count(status=status_filter) if user.is_admin else None
    return page_of(rows, page, total=total)


@router.get("/stats", summary="任务统计")
def task_stats(user: RequireUser) -> dict[str, Any]:
    """任务统计。非管理员只看得到自己的数字 —— 但**字段与全所版完全一致**。

    见 ``task_service.stats`` 的注释：字段少给会让工作台的「平均置信度 / 引用拦截」
    静默显示成「— / 0 次」，看着像"没有数据"，其实接口没给。
    """
    if user.is_admin:
        return task_service.stats()
    out = task_service.stats(user_id=user.id)
    out["scope"] = "本人"
    return out


@router.get("/{task_id}", summary="任务详情（含 Trace 与 State）")
def get_task(
    task_id: str,
    user: RequireUser,
    with_state: Annotated[bool, Query(description="是否返回 State 快照（含材料全文，较大）")] = False,
    with_trace: Annotated[bool, Query(description="是否返回全链路 Trace")] = True,
) -> dict[str, Any]:
    row = task_service.get(task_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"任务不存在：{task_id}")
    _assert_task_access(user, row)

    out = dict(row)
    if not with_state:
        # State 里可能躺着几十万字的合同原文。默认不返回，
        # 但**要告诉调用方裁掉了什么** —— 静默少一个字段比报错更难查
        # （前端会以为"任务没跑出状态"）。
        state = out.pop("state", None) or {}
        out["state_summary"] = {
            "available": bool(state),
            "field_count": len(state),
            "note": "State 快照默认不返回；加 ?with_state=true 获取（可能包含材料全文）",
        }
    if not with_trace:
        out.pop("trace", None)
    out["disclaimer"] = (row.get("state") or {}).get("disclaimer", "")
    return out


@router.get("/{task_id}/trace", summary="任务全链路 Trace")
def get_trace(
    task_id: str,
    user: RequireUser,
    node: Annotated[str, Query(description="只看某个节点")] = "",
    level: Annotated[str, Query(description="按级别过滤：info / warning / error")] = "",
) -> dict[str, Any]:
    row = task_service.get(task_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"任务不存在：{task_id}")
    _assert_task_access(user, row)

    events = list(row.get("trace") or [])
    if node:
        events = [e for e in events if isinstance(e, dict) and e.get("node") == node]
    if level:
        events = [e for e in events if isinstance(e, dict) and e.get("level") == level]

    by_node: dict[str, int] = {}
    for e in row.get("trace") or []:
        if isinstance(e, dict):
            by_node[e.get("node") or ""] = by_node.get(e.get("node") or "", 0) + 1

    return {
        "task_id": task_id,
        "total": len(row.get("trace") or []),
        "returned": len(events),
        "by_node": by_node,
        "events": events,
        "route": list((row.get("state") or {}).get("route") or []),
    }


@router.get("/{task_id}/citations", summary="任务引用清单")
def get_citations(task_id: str, user: RequireUser) -> dict[str, Any]:
    """引用清单。**读的是 ``la_citation`` 表而不是任务快照里的 JSON** ——
    快照会裁剪（列表超 400 条即截断），而被拦截的引用恰恰最容易先被截掉。"""
    row = task_service.get(task_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"任务不存在：{task_id}")
    _assert_task_access(user, row)

    items = citation_service.list_citations(task_id=task_id, limit=500)
    return {
        "task_id": task_id,
        "citation_check": row.get("citation_check"),
        "citation_summary": (row.get("state") or {}).get("citation_summary") or {},
        "count": len(items),
        "verified": sum(1 for i in items if i["verified"]),
        "rejected": sum(1 for i in items if not i["verified"]),
        "items": items,
    }


@router.get("/{task_id}/gaps", summary="任务证据缺口清单")
def get_gaps(task_id: str, user: RequireUser) -> dict[str, Any]:
    """证据缺口。方案第十三章的降级原则要求缺口必须显式声明 ——
    这里把它单独做成一个接口，前端在报告顶部常驻展示。"""
    row = task_service.get(task_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"任务不存在：{task_id}")
    _assert_task_access(user, row)
    gaps = list(row.get("evidence_gap") or [])
    return {
        "task_id": task_id,
        "count": len(gaps),
        "high_impact": [g for g in gaps if isinstance(g, dict) and g.get("degraded")],
        "items": gaps,
    }


@router.get("/{task_id}/stream", summary="任务进度（SSE，轮询落库快照）")
async def stream_task(
    task_id: str,
    user: RequireUser,
    interval: Annotated[float, Query(ge=0.5, le=10.0, description="轮询间隔（秒）")] = 1.0,
):
    """订阅**已在运行**的任务（例如 EXE 端发起、Web 端来围观）。

    与 ``/chat/stream`` 的区别：那个是"发起并订阅"，这个是"只订阅已存在的任务"。
    实现是轮询数据库快照而不是内存事件总线 —— 因为发起方可能在**另一个进程**
    （EXE 走的是同一后端，但 worker 进程也可能在跑任务），
    进程内的事件总线接不到别人的任务。落库快照是唯一的跨进程真相。
    """
    row = task_service.get(task_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"任务不存在：{task_id}")
    _assert_task_access(user, row)

    def _frame(event: str, data: Any) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"

    async def _gen():
        last_progress = -1
        last_trace_len = -1
        deadline = 60 * 60  # 一小时兜底，避免订阅一个永远不结束的任务
        waited = 0.0
        while waited < deadline:
            current = task_service.get(task_id)
            if current is None:
                yield _frame("error", {"error": "任务已被清理"})
                return
            trace = list(current.get("trace") or [])
            if current["progress"] != last_progress or len(trace) != last_trace_len:
                last_progress = current["progress"]
                last_trace_len = len(trace)
                yield _frame("progress", {
                    "task_id": task_id, "status": current["status"],
                    "progress": current["progress"], "intent": current.get("intent"),
                    "confidence": current.get("confidence"),
                    "citation_check": current.get("citation_check"),
                    "trace_count": len(trace),
                    "latest": trace[-1] if trace else None,
                })
            if current["status"] in ("succeeded", "degraded", "failed"):
                yield _frame("done", {
                    "task_id": task_id, "status": current["status"],
                    "progress": 100, "confidence": current.get("confidence"),
                    "citation_check": current.get("citation_check"),
                    "elapsed_ms": current.get("elapsed_ms"),
                    "error": current.get("error") or "",
                    "evidence_gap": current.get("evidence_gap") or [],
                })
                return
            await asyncio.sleep(interval)
            waited += interval
            yield ": keep-alive\n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------------- 人工抽查闭环
# 双模型复核（初审 + 复审）是纯 AI；人工抽查是"人"在里面的环节：按比例抽中 → 入队 →
# 律师在「待人工抽查」队列里核 → 点「通过/有误」→ 结论记录并回灌自我改进。
# ⚠ 路径刻意用两段（``/tasks/spot-check/...``）：单段会撞上 ``/{task_id}`` 那条路由。
class SpotCheckResolve(BaseModel):
    task_id: str = Field(..., description="被抽查的审查任务 id")
    outcome: str = Field(..., description="ok=通过；wrong=有误")
    note: str = Field("", description="有误时的说明（哪里错了）")


@router.get("/spot-check/queue", summary="待人工抽查队列")
def spot_check_queue(user: RequireUser) -> dict[str, Any]:
    """列出待人工抽查的合同审查任务（pending 在前，已处理的沉底）。"""
    from backend.app.services import spot_check_service

    return spot_check_service.list_queue()


@router.post("/spot-check/resolve", summary="记录抽查结论（通过 / 有误）")
def spot_check_resolve(user: RequireUser, payload: SpotCheckResolve) -> dict[str, Any]:
    """律师核完一条：点「通过」或「有误」。有误会写进自我改进样本，回灌复审提示词。"""
    from backend.app.services import spot_check_service

    if not spot_check_service.resolve(payload.task_id, payload.outcome, payload.note):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="待抽查队列里没有这个任务")
    return {"ok": True}
