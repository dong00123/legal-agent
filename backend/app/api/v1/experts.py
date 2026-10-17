"""专家（Expert）功能接口。

按 WorkBuddy 专家功能形态提供后端支撑，覆盖前端专家页所需的全部数据动作：
- ``GET /experts``            —— 搜索（职称/描述/标签）+ 分类 + 排序 + 分页；
- ``GET /experts/categories`` —— 动态分类列表；
- ``GET /experts/mine``       —— 我的专家（自建 + 收藏）；
- ``POST /experts``           —— 新建专家（author = 当前用户）；
- ``POST /experts/{id}/favorite`` —— 收藏 / 取消收藏；
- ``GET /experts/{id}``       —— 专家详情。

数据全部来自内存注册表（``services.expert_registry``），与
``agents/protocol/registry.py`` 的「派生不另写一份」风格一致：种子数据
启动时从 ``app/data/experts_seed.json`` 加载，自建与收藏以用户为键存进程内。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from backend.app.api.deps import RequireUser
from backend.app.security import CurrentUser
from backend.app.services.expert_registry import expert_registry

logger = logging.getLogger("legal_agent.api.experts")

router = APIRouter(tags=["专家"])

MAX_LIMIT = 200


class ExpertCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, description="专家名 / 职称")
    description: str = Field(
        ..., min_length=1, max_length=2000,
        description="人设描述（进入专家对话后作为 system 人设前缀注入）",
    )
    tags: list[str] = Field(default_factory=list, description="领域标签")
    category: str = Field(..., min_length=1, max_length=32, description="分类")
    hot: int = Field(0, ge=0, description="热度（默认 0）")


def _uid(user: CurrentUser) -> str:
    return str(getattr(user, "id", "") or "")


@router.get("/experts", summary="专家清单：搜索 / 分类 / 排序 / 分页")
async def list_experts(
    user: RequireUser,
    keyword: Annotated[str, Query(description="搜索职称 / 描述 / 标签，留空 = 全部")] = "",
    category: Annotated[str, Query(description="分类筛选，留空 = 全部")] = "",
    sort: Annotated[str, Query(
        pattern="^(default|hot|latest)$",
        description="排序：default 综合 / hot 最热 / latest 最新",
    )] = "default",
    offset: Annotated[int, Query(ge=0, description="偏移量")] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT, description="每页条数")] = 10,
) -> dict[str, Any]:
    """专家清单。每项卡片含 ``favorited``（当前用户是否已收藏），前端可直接渲染。"""
    try:
        return expert_registry.list_experts(
            keyword=keyword,
            category=category,
            sort=sort,
            offset=offset,
            limit=limit,
            user_id=_uid(user),
        )
    except ValueError as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/experts/categories", summary="动态分类列表")
async def list_categories(user: RequireUser) -> dict[str, Any]:
    """动态分类：种子 + 用户自建出现过的分类，按展示顺序返回。"""
    return {"categories": expert_registry.categories()}


@router.get("/experts/mine", summary="我的专家：自建 + 收藏")
async def list_mine(user: RequireUser) -> dict[str, Any]:
    """当前用户自建 + 收藏的专家。"""
    return expert_registry.mine(_uid(user))


@router.post("/experts", status_code=status.HTTP_201_CREATED, summary="新建专家")
async def create_expert(body: ExpertCreate, user: RequireUser) -> dict[str, Any]:
    """新建专家：``author`` = 当前用户，登记进「我的专家」的自建列表。"""
    expert = expert_registry.create(
        _uid(user),
        str(getattr(user, "username", "") or ""),
        body.model_dump(),
    )
    return {"expert": expert}


@router.post("/experts/{expert_id}/favorite", summary="收藏 / 取消收藏")
async def toggle_favorite(expert_id: str, user: RequireUser) -> dict[str, Any]:
    """收藏 / 取消收藏专家，返回切换后是否已收藏。"""
    favorited = expert_registry.toggle_favorite(_uid(user), expert_id)
    if favorited is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"专家不存在：{expert_id}")
    return {"expert_id": expert_id, "favorited": favorited}


@router.get("/experts/{expert_id}", summary="专家详情")
async def expert_detail(expert_id: str, user: RequireUser) -> dict[str, Any]:
    """单个专家详情（含当前用户收藏状态）。"""
    expert = expert_registry.get(expert_id)
    if expert is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"专家不存在：{expert_id}")
    expert = dict(expert)
    expert["favorited"] = expert_registry.is_favorited(_uid(user), expert_id)
    return {"expert": expert}


__all__ = ["router"]
