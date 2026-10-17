"""技能（Skill）功能接口。

按 WorkBuddy 技能功能形态提供后端支撑，覆盖前端技能页所需的全部数据动作：
- ``GET /skills``            —— 技能市场：搜索（名称/描述/标签）+ 分类 + 启用状态 + 排序 + 分页；
- ``GET /skills/categories`` —— 动态分类列表；
- ``GET /skills/mine``       —— 已安装技能（安装过的全部，含关闭中；关闭 ≠ 卸载）；
- ``POST /skills``           —— 创建技能（author = 当前用户，默认启用）；
- ``PUT /skills/{id}``       —— 切换启用 / 关闭（仅记用户配置，不改种子文件）；
- ``PUT /skills/{id}/edit``  —— 编辑自建技能（仅 author = 当前用户的自建技能可改）；
- ``PUT /skills/{id}/pin``   —— 置顶 / 取消置顶（内存保序，重启恢复默认顺序）；
- ``DELETE /skills/{id}``    —— 卸载技能（市场按钮恢复「安装」，自建技能连市场一并删除）。

数据全部来自内存注册表（``services.skill_registry``），与专家模块同一模式：
种子数据启动时从 ``app/data/skills_seed.json`` 加载，自建与开关覆盖以用户为键存进程内。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from backend.app.api.deps import RequireUser
from backend.app.security import CurrentUser
from backend.app.services.skill_registry import skill_registry

logger = logging.getLogger("legal_agent.api.skills")

router = APIRouter(tags=["技能"])

MAX_LIMIT = 200


class SkillCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, description="技能名")
    description: str = Field(
        ..., min_length=1, max_length=2000,
        description="技能描述（技能卡副文案）",
    )
    tags: list[str] = Field(default_factory=list, description="标签")
    category: str = Field(..., min_length=1, max_length=32, description="分类")
    icon: str = Field("i-zap", max_length=32, description="图标 symbol id（web 内联 sprite）")
    page: str = Field("", max_length=32, description="点击卡片跳转的目标页面 id（可为空）")
    hot: int = Field(0, ge=0, description="热度（默认 0）")


class SkillToggle(BaseModel):
    enabled: bool = Field(..., description="true = 启用，false = 关闭")


class SkillEdit(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, description="技能名")
    description: str = Field(..., min_length=1, max_length=2000, description="技能描述")
    tags: list[str] = Field(default_factory=list, description="标签")
    category: str = Field(..., min_length=1, max_length=32, description="分类")
    icon: str = Field("i-zap", max_length=32, description="图标 symbol id（web 内联 sprite）")
    page: str = Field("", max_length=32, description="功能页 id（可为空）")


class SkillPin(BaseModel):
    pinned: bool = Field(..., description="true = 置顶，false = 取消置顶")


def _uid(user: CurrentUser) -> str:
    return str(getattr(user, "id", "") or "")


@router.get("/skills", summary="技能市场：搜索 / 分类 / 启用状态 / 排序 / 分页")
async def list_skills(
    user: RequireUser,
    keyword: Annotated[str, Query(description="搜索技能名 / 描述 / 标签，留空 = 全部")] = "",
    category: Annotated[str, Query(description="分类筛选，留空 = 全部")] = "",
    sort: Annotated[str, Query(
        pattern="^(default|hot|latest)$",
        description="排序：default 综合 / hot 最热 / latest 最新",
    )] = "default",
    enabled: Annotated[str, Query(
        pattern="^(true|false)?$",
        description="启用状态筛选：留空 = 全部 / true = 仅启用 / false = 仅关闭",
    )] = "",
    offset: Annotated[int, Query(ge=0, description="偏移量")] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT, description="每页条数")] = 10,
) -> dict[str, Any]:
    """技能市场清单。每项含 ``enabled``（当前用户生效状态）与 ``installed``，前端可直接渲染。"""
    try:
        return skill_registry.list_skills(
            keyword=keyword,
            category=category,
            sort=sort,
            enabled=enabled,
            offset=offset,
            limit=limit,
            user_id=_uid(user),
        )
    except ValueError as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/skills/categories", summary="动态分类列表")
async def list_categories(user: RequireUser) -> dict[str, Any]:
    """动态分类：种子 + 用户自建出现过的分类，按展示顺序返回。"""
    return {"categories": skill_registry.categories()}


@router.get("/skills/mine", summary="已安装技能（安装过的全部，含关闭中）")
async def list_mine(user: RequireUser) -> dict[str, Any]:
    """当前用户安装过的全部技能（种子默认自带 + 显式安装 + 自建），含关闭中。

    关闭 ≠ 卸载：关闭的技能仍在已安装列表，只是 ``enabled`` 为 False。
    """
    return skill_registry.mine(_uid(user))


@router.post("/skills", status_code=status.HTTP_201_CREATED, summary="创建技能")
async def create_skill(body: SkillCreate, user: RequireUser) -> dict[str, Any]:
    """创建技能：``author`` = 当前用户，默认启用，进入「已安装」列表与市场清单。"""
    skill = skill_registry.create(
        _uid(user),
        str(getattr(user, "username", "") or ""),
        body.model_dump(),
    )
    return {"skill": skill}


@router.put("/skills/{skill_id}", summary="切换启用 / 关闭")
async def toggle_enabled(skill_id: str, body: SkillToggle, user: RequireUser) -> dict[str, Any]:
    """切换技能启用状态：仅记录当前用户配置，不改动种子文件（对齐 WorkBuddy 语义）。"""
    enabled = skill_registry.set_enabled(_uid(user), skill_id, body.enabled)
    if enabled is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"技能不存在：{skill_id}")
    return {"skill_id": skill_id, "enabled": enabled}


@router.put("/skills/{skill_id}/edit", summary="编辑自建技能")
async def edit_skill(skill_id: str, body: SkillEdit, user: RequireUser) -> dict[str, Any]:
    """编辑技能：仅 author = 当前用户的自建技能可改，内置技能返回 403。"""
    try:
        skill = skill_registry.edit(_uid(user), skill_id, body.model_dump())
    except PermissionError as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ValueError as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"技能不存在：{skill_id}")
    return {"skill": skill}


@router.put("/skills/{skill_id}/pin", summary="置顶 / 取消置顶技能")
async def pin_skill(skill_id: str, body: SkillPin, user: RequireUser) -> dict[str, Any]:
    """置顶 / 取消置顶：内存保序（最新置顶在前），重启恢复默认顺序。"""
    pinned = skill_registry.set_pinned(_uid(user), skill_id, body.pinned)
    if pinned is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"技能不存在：{skill_id}")
    return {"skill_id": skill_id, "pinned": pinned}


@router.delete("/skills/{skill_id}", summary="卸载技能")
async def uninstall_skill(skill_id: str, user: RequireUser) -> dict[str, Any]:
    """卸载技能：从「已安装」移除，市场按钮恢复「安装」；自建技能连市场一并删除。"""
    ok = skill_registry.uninstall(_uid(user), skill_id)
    if ok is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"技能不存在：{skill_id}")
    return {"skill_id": skill_id, "uninstalled": ok}


__all__ = ["router"]
