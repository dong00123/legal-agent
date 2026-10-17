"""v1 路由汇聚。

**只做汇聚，不做业务。** 各子路由文件的 ``prefix`` 已经写在自己身上
（``/auth``、``/admin``、``/tasks``、``/drafts``），没有前缀的那些是
"资源在根下"的接口（``/chat``、``/cases``、``/knowledge``、``/meta``、``/ws``）。
所以这里统一由 ``main.py`` 挂一个 ``settings.API_PREFIX``（``/api/v1``）即可，
**不要在汇聚层再加一次前缀**，否则会变成 ``/api/v1/v1/auth/login``。

两条容易踩的接线约束：

1. **``include_router`` 的顺序 = 路由匹配顺序。** 前缀重叠时先注册的赢。
   这里没有重叠路由（``/tasks`` 与 ``/tasks/{id}/...`` 都在同一个文件里），
   但仍按"元信息 → 鉴权 → 业务"的顺序排，让 ``/health``、``/meta/*``
   这类无鉴权接口在最前面 —— 探针路径上的匹配开销最小。

2. **``meta.router`` 会被挂两次**（``/api/v1/health`` 与根路径 ``/health``）。
   根路径那份在 ``main.py`` 里单独处理，**不在这里 include 第二遍** ——
   同一个 APIRouter 对象 include 两次会让 OpenAPI 里出现重复的 operationId，
   ``fastapi`` 会打印一长串 warning，而 warning 多了就没人看了。
"""

from __future__ import annotations

from fastapi import APIRouter

from backend.app.api.v1 import (
    admin,
    agents,
    auth,
    cases,
    chat,
    conversations,
    docs,
    evolution,
    experts,
    knowledge,
    meta,
    skills,
    tasks,
    usage,
    wechat,
)

api_router = APIRouter()

# 无鉴权或弱鉴权，放最前
api_router.include_router(meta.router)
api_router.include_router(auth.router)
# 业务
api_router.include_router(cases.router)
api_router.include_router(chat.router)
# 紧跟在 chat.router 后面：会话是 /chat/rag 的产物，两者放一起便于对照。
# 前缀 /chat/conversations 与 chat.py 里的路径无重叠（那边只有 /chat/rag[/stream]）。
api_router.include_router(conversations.router)
api_router.include_router(tasks.router)
api_router.include_router(docs.router)
# Skill 自进化飞轮（/evolution 观测 → 治理），与 r2 对齐；写接口 RequireAdmin。
api_router.include_router(evolution.router)
api_router.include_router(knowledge.router)
api_router.include_router(admin.router)
# Agent 目录与 A2A 旁路委派（只读展示 + 隔离委派，不影响案件流程）
api_router.include_router(agents.router)
# 专家（Expert）：搜索/分类/我的/新建/收藏/详情，注册表为内存 registry。
# 与 /agents 并列放在业务区末尾：不参与案件流程，仅提供专家目录与人设入口。
api_router.include_router(experts.router)
# 技能（Skill）：市场/已安装/搜索/分类/创建/启用开关，注册表为内存 registry。
# 与 /experts 同属门面层：不参与案件流程，仅提供技能目录与开关配置。
api_router.include_router(skills.router)
# 对话 / 技能使用记录（/usage/logs）：读 la_usage_log，监控中心「对话使用记录」数据源。
# 与 /tasks/stats（la_task）、/evolution/*（Skill 观测）相互独立。
api_router.include_router(usage.router)
# 微信机器人接入：/wechat/events 走桥接密钥，其余走 JWT（见 wechat.py 的说明）。
# 放在最后是因为它既有业务读写（上传材料、生成证据链）又有自己的鉴权方式，
# 与上面任何一组都不重叠。
api_router.include_router(wechat.router)

# **WebSocket 刻意不在这里 include。**
# 它由 ``main.py`` 单独挂在根路径的 ``/ws/...`` 上。原因是
# ``api_router`` 会被挂到 ``/api/v1`` 前缀下，WS 路由跟进去就变成
# ``/api/v1/ws/tasks/{id}`` —— 而 ``ws.py`` 的协议说明、前端与部署脚本里
# 写的都是 ``/ws/tasks/{id}``，两边对不上时的表现是"前端永远连不上，
# 且服务端日志里连一条握手记录都没有"（路径根本没匹配到任何路由）。
# WebSocket 属于"长连接通道"而不是某个版本的 REST 资源，不该被版本前缀管辖：
# 以后加 ``/api/v2`` 时也不必再开一条 ``/api/v2/ws/...``。


__all__ = ["api_router"]
