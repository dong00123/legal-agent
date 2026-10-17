"""管理后台接口：用户与团队、系统配置、审计中心、运营统计（**全部限管理员**）。

四块内容放在一起，是因为它们共享同一个角色门槛，且共同构成"律所管理层视角"。

三条设计纪律：

1. **配置的可改范围由白名单决定，不由入参决定。**
   ``config_service.EDITABLE`` 之外一律拒绝（``DB_URL`` / ``SECRET_KEY`` 这类的
   热改后果是数据消失或全所掉线）。接口把禁改清单也返回出去 ——
   管理员能直接看到"为什么这个不能改"，而不是反复尝试后放弃。

2. **审计只读，不提供删除。** 方案第十四章要求审计"不可删改"，
   所以这里不存在任何 DELETE 路径。带 ``import_file_to_db`` 是为了
   在 DB 曾短暂不可用时从文件真源补录 —— 它**只增不改**。

3. **统计的口径必须随数字一起给出。** "采纳率"只是"已导出草稿 / 全部草稿"的
   近似（见 ``stats_service``），"节点耗时"是含内部调用的墙钟时间。
   数字离开口径就会被误读，所以口径字段和数字一起返回。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from backend.app.api.deps import Meta, Page, RequireAdmin, RequireUser, page_of
from backend.app.services import audit_service, config_service, knowledge_service
from backend.app.services import monitor_service, stats_service, user_service

logger = logging.getLogger("legal_agent.api.admin")

router = APIRouter(prefix="/admin", tags=["管理后台"])


def _audit(admin: Any, meta: Meta, action: str, *, resource: str, resource_id: str = "",
           detail: str = "", result: str = "ok") -> None:
    from backend.agents.harness.audit import audit_writer

    audit_writer.record(action, user_id=admin.id, username=admin.username,
                        team_id=admin.team_id, client=meta.client, ip=meta.ip,
                        resource=resource, resource_id=resource_id,
                        detail=detail, result=result)


# --------------------------------------------------------------------------- 模型
class TeamCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: str = ""
    case_isolation: bool = True


class TeamUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    case_isolation: bool | None = None


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=user_service.MIN_PASSWORD_LENGTH)
    role: str = Field("lawyer", description="admin / lawyer")
    full_name: str = ""
    license_no: str = ""
    team_id: int | None = None


class UserUpdate(BaseModel):
    password: str | None = Field(None, min_length=user_service.MIN_PASSWORD_LENGTH)
    role: str | None = None
    full_name: str | None = None
    license_no: str | None = None
    team_id: int | None = None
    is_active: bool | None = None


class ConfigSet(BaseModel):
    value: Any = Field(..., description="按该键声明的类型传值")
    value_type: str = ""
    group: str = ""
    description: str = ""


# --------------------------------------------------------------------------- 团队
@router.get("/teams", summary="团队列表")
def list_teams(admin: RequireAdmin) -> dict[str, Any]:
    return {"items": user_service.list_teams()}


@router.post("/teams", summary="新建团队", status_code=status.HTTP_201_CREATED)
def create_team(payload: TeamCreate, admin: RequireAdmin, meta: Meta) -> dict[str, Any]:
    try:
        out = user_service.create_team(payload.name, description=payload.description,
                                       case_isolation=payload.case_isolation)
    except user_service.UserError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    _audit(admin, meta, "team.create", resource="team", resource_id=str(out["id"]),
           detail=f"新建团队 {out['name']}（案件隔离={out['case_isolation']}）")
    return out


@router.put("/teams/{team_id}", summary="更新团队")
def update_team(team_id: int, payload: TeamUpdate, admin: RequireAdmin, meta: Meta) -> dict[str, Any]:
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    out = user_service.update_team(team_id, **fields)
    if out is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"团队不存在：{team_id}")
    _audit(admin, meta, "team.update", resource="team", resource_id=str(team_id),
           detail=f"更新字段 {', '.join(fields)}")
    return out


@router.delete("/teams/{team_id}", summary="删除团队")
def delete_team(
    team_id: int,
    admin: RequireAdmin,
    meta: Meta,
    reassign_to: Annotated[int | None, Query(description="团队成员改派到的团队 id")] = None,
) -> dict[str, Any]:
    """删除团队。有成员时必须显式指定改派目标（见 ``user_service.delete_team``）。"""
    out = user_service.delete_team(team_id, reassign_to=reassign_to)
    if not out.get("ok"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=out.get("reason", "删除失败"))
    _audit(admin, meta, "team.delete", resource="team", resource_id=str(team_id),
           detail=f"删除团队，改派成员 {out.get('reassigned', 0)} 人")
    return out


# --------------------------------------------------------------------------- 用户
@router.get("/users", summary="用户列表")
def list_users(
    admin: RequireAdmin,
    page: Page,
    keyword: Annotated[str, Query(description="登录名 / 姓名 / 执业证号")] = "",
    role: Annotated[str, Query()] = "",
    team_id: Annotated[int | None, Query()] = None,
    active_only: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    rows = user_service.list_users(keyword=keyword, role=role, team_id=team_id,
                                  active_only=active_only,
                                  limit=page.limit, offset=page.offset)
    return page_of(rows, page)


@router.post("/users", summary="新建用户", status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, admin: RequireAdmin, meta: Meta) -> dict[str, Any]:
    try:
        out = user_service.create_user(
            payload.username, payload.password, role=payload.role,
            full_name=payload.full_name, license_no=payload.license_no,
            team_id=payload.team_id,
        )
    except user_service.UserError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    # 审计里**不写口令**（哪怕是散列也不写）
    _audit(admin, meta, "user.create", resource="user", resource_id=str(out["id"]),
           detail=f"新建用户 {out['username']}（角色 {out['role']}）")
    return out


@router.put("/users/{user_id}", summary="更新用户")
def update_user(user_id: int, payload: UserUpdate, admin: RequireAdmin, meta: Meta) -> dict[str, Any]:
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="没有需要更新的字段")
    try:
        out = user_service.update_user(user_id, **fields)
    except user_service.UserError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if out is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"用户不存在：{user_id}")
    changed = [k for k in fields if k != "password"]
    _audit(admin, meta, "user.update", resource="user", resource_id=str(user_id),
           detail="更新字段 " + "、".join(changed + (["口令"] if "password" in fields else [])))
    return out


@router.post("/users/{user_id}/deactivate", summary="停用用户")
def deactivate_user(user_id: int, admin: RequireAdmin, meta: Meta) -> dict[str, Any]:
    """停用而**不是删除**（原因见 ``user_service`` 模块 docstring 第 1 条：
    删行会让案件归属与审计记录悬空）。"""
    out = user_service.deactivate(user_id)
    if not out.get("ok"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=out.get("reason", "停用失败"))
    _audit(admin, meta, "user.deactivate", resource="user", resource_id=str(user_id),
           detail=f"停用用户 {out.get('username', '')}")
    return out


@router.post("/users/{user_id}/reactivate", summary="启用用户")
def reactivate_user(user_id: int, admin: RequireAdmin, meta: Meta) -> dict[str, Any]:
    out = user_service.reactivate(user_id)
    if not out.get("ok"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=out.get("reason", "启用失败"))
    _audit(admin, meta, "user.reactivate", resource="user", resource_id=str(user_id),
           detail=f"启用用户 {out.get('username', '')}")
    return out


# --------------------------------------------------------------------------- 配置
@router.get("/config", summary="系统配置清单（含当前生效值）")
def list_config(
    admin: RequireAdmin,
    group: Annotated[str, Query(description="按分组过滤：model / agent / degrade / rag / system")] = "",
) -> dict[str, Any]:
    """返回**每个可改项的当前生效值、代码默认值、是否被覆盖**。

    只返回覆盖值是不够的 —— 没被改过的键在库里没有行，
    只给覆盖值会让界面看起来"配置是空的"，而系统其实跑在一堆默认值上。
    """
    items = knowledge_service.effective_config()
    if group:
        items = [i for i in items if i["group"] == group]
    return {
        "items": items,
        "groups": config_service.groups(),
        "editable_count": len(config_service.EDITABLE),
        "forbidden": config_service.forbidden(),
        "health": config_service.health(),
    }


@router.put("/config/{key}", summary="修改配置（写入覆盖值）")
def set_config(key: str, payload: ConfigSet, admin: RequireAdmin, meta: Meta) -> dict[str, Any]:
    try:
        out = knowledge_service.set_config(key, payload.value, value_type=payload.value_type,
                                           group=payload.group,
                                           description=payload.description,
                                           updated_by=admin.id)
    except knowledge_service.KnowledgeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    _audit(admin, meta, "config.set", resource="system_config", resource_id=key,
           detail=f"{key} = {out['value']}（{out['value_type']}）")
    return out


@router.delete("/config/{key}", summary="恢复配置默认值")
def reset_config(key: str, admin: RequireAdmin, meta: Meta) -> dict[str, Any]:
    out = knowledge_service.reset_config(key)
    _audit(admin, meta, "config.reset", resource="system_config", resource_id=key,
           detail="恢复为代码默认值")
    return out


# --------------------------------------------------------------------------- 审计
@router.get("/audit", summary="审计查询")
def query_audit(
    admin: RequireAdmin,
    page: Page,
    keyword: Annotated[str, Query()] = "",
    action: Annotated[str, Query()] = "",
    user_id: Annotated[int | None, Query()] = None,
    team_id: Annotated[int | None, Query()] = None,
    case_id: Annotated[str, Query()] = "",
    task_id: Annotated[str, Query()] = "",
    result: Annotated[str, Query(description="ok / denied / error")] = "",
    date_from: Annotated[str, Query(description="YYYY-MM-DD，含当日")] = "",
    date_to: Annotated[str, Query(description="YYYY-MM-DD，含当日")] = "",
) -> dict[str, Any]:
    """审计查询（读 DB 可查询副本）。**只有读，没有删改接口。**"""
    rows = audit_service.query_db(keyword=keyword, action=action, user_id=user_id,
                                 team_id=team_id, case_id=case_id, task_id=task_id,
                                 result=result, date_from=date_from, date_to=date_to,
                                 limit=page.limit, offset=page.offset)
    return page_of(rows, page)


@router.get("/audit/stats", summary="审计统计")
def audit_stats(
    admin: RequireAdmin,
    date: Annotated[str, Query(description="对账日期，默认今天")] = "",
) -> dict[str, Any]:
    return {**audit_service.stats(date=date), "actions": audit_service.actions()}


@router.get("/audit/consistency", summary="审计文件与数据库对账")
def audit_consistency(
    admin: RequireAdmin,
    date: Annotated[str, Query()] = "",
) -> dict[str, Any]:
    """审计**文件真源**与 DB 副本的条数对账。

    差值为正说明有记录只在文件里（DB 曾写入失败）。此时以文件为准，
    可调 ``POST /admin/audit/reconcile`` 从文件补录。
    """
    return audit_service.consistency(date=date)


@router.post("/audit/reconcile", summary="从审计文件补录到数据库")
def audit_reconcile(admin: RequireAdmin, meta: Meta,
                    date: Annotated[str, Query()] = "") -> dict[str, Any]:
    """把某天的审计文件补录进 DB（**只增不改**，已存在的跳过）。"""
    out = audit_service.import_file_to_db(date=date)
    _audit(admin, meta, "audit.reconcile", resource="audit_log",
           detail=f"补录 {out.get('date')}：新增 {out.get('imported', 0)}，"
                  f"跳过 {out.get('skipped', 0)}")
    return out


@router.get("/audit/trace/{task_id}", summary="按任务追溯审计链路")
def audit_trace(task_id: str, admin: RequireAdmin) -> dict[str, Any]:
    rows = audit_service.trace_of(task_id)
    return {"task_id": task_id, "count": len(rows), "items": rows}


@router.get("/audit/export", response_class=PlainTextResponse, summary="导出审计（JSONL）")
def audit_export(admin: RequireAdmin, meta: Meta,
                 date: Annotated[str, Query()] = "") -> PlainTextResponse:
    """导出某天审计为 JSONL 文本。**不做脱敏也不做删减** —— 审计的作用就是完整留痕。"""
    filename, content = audit_service.export_jsonl(date=date)
    _audit(admin, meta, "audit.exports", resource="audit_log",
           detail=f"导出 {filename}（{len(content.splitlines())} 行）")
    return PlainTextResponse(
        content,
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/audit/files", summary="审计文件清单")
def audit_files(admin: RequireAdmin) -> dict[str, Any]:
    return {"items": audit_service.daily_files()}


# --------------------------------------------------------------------------- 统计
@router.get("/stats/dashboard", summary="运营看板（首屏）")
def dashboard(admin: RequireAdmin,
              days: Annotated[int, Query(ge=1, le=365)] = 7) -> dict[str, Any]:
    return stats_service.dashboard(days=days)


@router.get("/stats/report", summary="运营报表（含耗时分布与工具使用）")
def report(admin: RequireAdmin,
           days: Annotated[int, Query(ge=1, le=365)] = 7) -> dict[str, Any]:
    return stats_service.full_report(days=days)


@router.get("/stats/agents", summary="各 Agent（节点）耗时分布")
def agent_elapsed(admin: RequireAdmin,
                  days: Annotated[int, Query(ge=1, le=365)] = 7) -> dict[str, Any]:
    return stats_service.agent_elapsed(days=days)


@router.get("/stats/tools", summary="工具调用分布")
def tool_usage(admin: RequireAdmin,
               days: Annotated[int, Query(ge=1, le=365)] = 7) -> dict[str, Any]:
    return stats_service.tool_usage(days=days)


@router.get("/stats/citations", summary="引用校验统计")
def citation_stats(admin: RequireAdmin,
                   case_id: Annotated[str, Query()] = "") -> dict[str, Any]:
    from backend.app.services import citation_service

    return {
        **citation_service.stats(case_id=case_id),
        "top_rejected": citation_service.top_rejected(limit=20),
        "caliber": "intercept_rate = 未通过存在性校验的引用条数 / 全部引用条数",
    }


# --------------------------------------------------------------------------- 监控中心
@router.get("/monitor/expert-usage", summary="专家使用统计（监控中心，登录可见）")
def monitor_expert_usage(user: RequireUser,
                         days: Annotated[int, Query(ge=1, le=365)] = 7) -> dict[str, Any]:
    """按专家聚合近 ``days`` 天的使用次数 / 平均置信度 / 平均耗时 / 最近使用 / 按天趋势。"""
    return monitor_service.expert_usage(days=days)


@router.get("/monitor/skill-usage", summary="技能命中统计（监控中心，登录可见）")
def monitor_skill_usage(user: RequireUser,
                        days: Annotated[int, Query(ge=1, le=365)] = 7) -> dict[str, Any]:
    """按技能聚合近 ``days`` 天的命中次数 / 命中意图分布 / 按天趋势。"""
    return monitor_service.skill_usage(days=days)


@router.get("/monitor/usage-logs", summary="调用明细（监控中心，登录可见）")
def monitor_usage_logs(user: RequireUser,
                       days: Annotated[int, Query(ge=1, le=365)] = 7,
                       limit: Annotated[int, Query(ge=1, le=500)] = 100) -> dict[str, Any]:
    """近 ``days`` 天调用明细（按时间倒序，最多 ``limit`` 条），供健康诊断与概览指标使用。"""
    return monitor_service.usage_logs(days=days, limit=limit)
