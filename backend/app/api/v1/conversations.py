"""AI 助手历史会话接口。

单独一个文件而不是塞进 ``chat.py``：那个文件已经一千多行、自述是"发起"类接口
（问答 / 合同审查 / 文书起草 / 检索），而这里是同一批数据的**读与删**。
项目惯例是一资源一文件（``cases.py`` / ``docs.py`` / ``tasks.py``）。

三条口径：
1. **写不在这里。** 会话是 ``/chat/rag`` 答完以后顺手落下的（见 ``chat.py`` 的落库段），
   没有"手动新建一段会话"的接口 —— 空会话没有意义，「新建」在前端就是把
   ``conversationId`` 清空，下一次提问自然会开一段新的。
2. **默认只看自己的会话。** 会话记录是"这位律师在查什么问题"，
   同所同事互相可见没有正当性。管理员可加 ``all_users=true``（照 ``docs.py``）。
3. **删除是标记删除**，见 ``chat_history_service.delete_conversation``。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Query

from backend.app.api.deps import Meta, Page, RequireUser, apply_page_params, effective_page, page_of
from backend.app.services import chat_history_service

logger = logging.getLogger("legal_agent.api.conversations")

router = APIRouter(prefix="/chat/conversations", tags=["AI 助手会话"])


@router.get("", summary="历史会话列表")
def list_conversations(
    user: RequireUser,
    page: Page,
    page_number: Annotated[int | None, Query(alias="page", ge=1, description="页码（从 1 起）")] = None,
    page_size: Annotated[int | None, Query(ge=1, le=200, description="每页条数")] = None,
    all_users: Annotated[bool, Query(description="管理员可看全部用户的会话")] = False,
) -> dict[str, Any]:
    """列表项只有 ``conversation_id / title / case_id / created_at / updated_at``。

    **不含 ``payload``**：列表要的是"哪一段"，把每段的完整答案都塞进列表响应
    会让首页多拉几十万字。正文在详情接口里。
    """
    user_id = None if (all_users and user.is_admin) else user.id
    limit, offset = apply_page_params(page, page_number, page_size)
    out = chat_history_service.list_conversations(
        user, user_id=user_id, limit=limit, offset=offset)
    return page_of(out["items"], effective_page(page, limit, offset), total=out["total"])


@router.get("/{conversation_id}", summary="历史会话详情（含每轮完整内容）")
def get_conversation(conversation_id: str, user: RequireUser) -> dict[str, Any]:
    """返回每轮的**整份** ``RagAnswer.to_dict()``，供前端完整重渲染。

    前端拿到 ``turns`` 后必须把每一轮都交给它自己的 ``formatRagAnswer`` 渲染 ——
    正文里的 ``[S1]``/``[C1]`` 标签靠 ``citations`` 才能点开，
    绕开那一步自己拼 HTML 会让标签变成死文本（而且不会报错）。
    """
    out = chat_history_service.get_conversation(conversation_id, user)
    out["ok"] = True
    return out


@router.delete("/{conversation_id}", summary="删除会话（标记删除）")
def delete_conversation(conversation_id: str, user: RequireUser, meta: Meta) -> dict[str, Any]:
    """标记删除：``status`` 改成 ``deleted``，行留在库里。

    为什么要留痕：会话记录是"这位律师问过什么"的凭据，
    而删除是用户可发起的、不可逆的语义变更（列表里再也看不到）。
    真要清理只能由 DBA 走库，接口层不做物理删除。
    """
    out = chat_history_service.delete_conversation(conversation_id, user)

    from backend.agents.harness.audit import audit_writer

    audit_writer.record("conversation.delete", user_id=user.id, username=user.username,
                        team_id=user.team_id, client=meta.client, ip=meta.ip,
                        resource="chat_conversation", resource_id=conversation_id,
                        detail=f"标记删除 AI 助手会话（{out.get('turns', 0)} 轮"
                               f"{'，重复删除' if out.get('already') else ''}）")
    return out
