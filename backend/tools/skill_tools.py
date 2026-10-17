"""市场技能可调工具（方案 B）。

把「技能市场（skill_registry）」中用户已启用的技能注册为 MCP 可调工具，
使启用/关闭真实进入 AI 回答链路：

- ``skill_list``：列出当前用户已启用（enabled）的市场技能，供模型/前端了解可用能力；
- ``skill_invoke``：按 skill_id / skill_name 取回某个技能的完整能力卡片
  （名称/分类/描述/标签/作者/启用态），调用方（回答链路）据此把该技能的
  能力边界纳入本次回答的作业指导。

设计约束：
1. 只读、幂等：两个工具都不改任何数据，可安全重试，走通用只读通配组。
2. 用户维度：提供 ``user_id`` 时只允许返回该用户「已启用」的技能
   （关闭 ≠ 卸载：关闭中的技能不在此列）；未提供 ``user_id`` 时仅返回技能
   元数据（不判定启用态），避免把用户级配置泄露给无身份的调用方。
3. 与原生技能（``backend.skills``，节点固定引用）互不干扰：本模块只服务
   市场技能（``backend.app.services.skill_registry``）。
"""

from __future__ import annotations

import logging
from typing import Any

from backend.tools.base import ToolResult

logger = logging.getLogger("legal_agent.tools.skill_tools")


def _registry():
    """延迟导入：避免 backend.app 依赖反向拉进 tools 包。"""
    from backend.app.services.skill_registry import skill_registry

    return skill_registry


def _find(skills: list[dict[str, Any]], skill_id: str, skill_name: str) -> dict[str, Any] | None:
    """在技能卡片列表里按 id 或 name 精确匹配（id 优先）。"""
    sid = str(skill_id or "").strip()
    sname = str(skill_name or "").strip()
    for s in skills or []:
        if sid and str(s.get("id") or "") == sid:
            return s
        if sname and str(s.get("name") or "").strip() == sname:
            return s
    return None


def _card_text(skill: dict[str, Any], *, scoped: bool) -> str:
    """把技能卡片拼成可读文本，供回答链路直接使用。"""
    lines = [
        f"技能名称：{skill.get('name') or ''}",
        f"技能ID：{skill.get('id') or ''}",
        f"分类：{skill.get('category') or ''}",
        f"描述：{skill.get('description') or ''}",
    ]
    tags = skill.get("tags") or []
    if tags:
        lines.append(f"标签：{'、'.join(str(t) for t in tags)}")
    author = skill.get("author") or ""
    if author:
        lines.append(f"作者：{author}")
    created = skill.get("created_at") or ""
    if created:
        lines.append(f"创建时间：{created}")
    if scoped:
        lines.append(f"启用状态：{bool(skill.get('enabled'))}")
        lines.append("说明：该技能已由当前用户启用，回答应按其能力范围组织内容。")
    else:
        lines.append("说明：当前调用未携带用户身份，仅返回技能元数据，未判定启用状态。")
    return "\n".join(lines)


async def skill_list(user_id: str = "", **kwargs: Any) -> ToolResult:
    """列出当前用户已启用的市场技能（供模型/回答链路了解可用能力）。"""
    reg = _registry()
    uid = str(user_id or "")
    if uid:
        items = (reg.mine(uid) or {}).get("items") or []
        summary = [
            {"id": s.get("id"), "name": s.get("name"),
             "category": s.get("category"), "description": s.get("description")}
            for s in items if s.get("enabled")
        ]
        data = {"user_id": uid, "count": len(summary), "skills": summary,
                "scoped": True, "note": "仅列出当前用户已启用的技能；关闭中的技能不在此列"}
    else:
        all_items = (reg.list_skills() or {}).get("items") or []
        summary = [
            {"id": s.get("id"), "name": s.get("name"),
             "category": s.get("category"), "description": s.get("description")}
            for s in all_items
        ]
        data = {"user_id": "", "count": len(summary), "skills": summary,
                "scoped": False, "note": "未携带用户身份，列出全部技能元数据（不判定启用态）"}
    return ToolResult(ok=True, tool="skill_list", data=data)


async def skill_invoke(skill_id: str = "", skill_name: str = "", query: str = "",
                       user_id: str = "", **kwargs: Any) -> ToolResult:
    """按 id / 名称取回技能能力卡片，供回答链路纳入作业指导。

    携带 ``user_id`` 时只在「已启用」技能中匹配；命中启用技能才返回完整卡片，
    否则返回 ok=False（明确拒绝，不让模型误以为关掉的技能仍在生效）。
    """
    reg = _registry()
    uid = str(user_id or "")

    if uid:
        items = (reg.mine(uid) or {}).get("items") or []
        skill = _find(items, skill_id, skill_name)
        if skill is None:
            return ToolResult(
                ok=False, tool="skill_invoke", error=f"技能未启用或不存在：id={skill_id!r} name={skill_name!r}",
                degraded=True,
                fallback_note="当前用户未启用该技能（或技能不存在）；启用后才会进入回答链路",
            )
        text = _card_text(skill, scoped=True)
        data = {"user_id": uid, "skill": skill, "text": text,
                "scoped": True, "query": query or ""}
    else:
        all_items = (reg.list_skills() or {}).get("items") or []
        skill = _find(all_items, skill_id, skill_name)
        if skill is None:
            return ToolResult(
                ok=False, tool="skill_invoke", error=f"技能不存在：id={skill_id!r} name={skill_name!r}",
                degraded=True, fallback_note="技能市场中没有该技能",
            )
        text = _card_text(skill, scoped=False)
        data = {"user_id": "", "skill": skill, "text": text,
                "scoped": False, "query": query or ""}

    logger.info("skill_invoke 命中 %s（scoped=%s, user=%s）",
                skill.get("name"), data["scoped"], uid or "-")
    return ToolResult(ok=True, tool="skill_invoke", data=data)
