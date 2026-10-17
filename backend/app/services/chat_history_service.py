"""AI 助手页的历史会话服务。

为什么要有它
------------
``/chat/rag`` 原先明确不落任何库（"随手问一句"），代价是**刷新即失忆**：
律师上午问过"医疗期怎么算"，下午想翻回那段结论，界面上无处可找。
本模块补的就是这件事 —— 但**只补这一件**：会话记录服务"我上次问过什么"，
不是任务档案。需要交付物与留痕的提问仍然走 ``/chat``（见 ``chat.py`` 的入口说明）。

四条纪律
--------

1. **权限判定必须失败即拒绝，且要有主体。** 会话是律师个人的问答记录，
   泄露的后果不是"看到别人的文书"而是"看到别人在查什么问题"——
   对律所来说这本身就是敏感信息。所以 ``viewer is None`` 一律抛错，
   **不设"空就放行"的默认**（接口层漏传一次登录用户就是一次静默的全量越权）。
   与 ``case_service.assert_access`` 同一条纪律，理由见那里的注释。

2. **追加绝不失败。** ``append_turn`` 是在答案已经生成完之后才调用的，
   这时候抛错等于"答完了但丢掉"——用户看到的是答案已出、历史里却没有。
   所以会话号查不到、无权限、甚至撞并发唯一约束，一律**降级成"另起一段会话"**，
   只有连续两次撞车才放弃（返回空 ``conversation_id``，前端据此不点亮侧栏）。

3. **落库内容整份存，不做字段裁剪。** 存的是 ``RagAnswer.to_dict()`` 全文 ——
   ``citations`` 没了正文里的 ``[S1]``/``[C1]`` 就点不开，
   ``ref_check.unsupported`` 没了"查无此条"的告警就消失，
   卡片顺序（= 正文首次出现序）错了正文与参考法规区就对不上。
   任何"只存正文省点空间"的做法都会重放出一段残废的历史。

4. **删除是标记删除。** ``status='deleted'``，行留着。
   与审计表「只插不删」、任务表「不可删」的项目调性一致；
   草稿是唯一真删的用户数据，本表不跟它走。
"""

from __future__ import annotations

import logging
import math
import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from backend.app.db.models import ChatTurn
from backend.app.db.session import session_scope

logger = logging.getLogger("legal_agent.services.chat_history")

# 会话标题截断长度。目标稿每行是一行标题 + 时间，20 字是标题不会挤掉时间的上限。
TITLE_CHARS = 20

# 首句切分：中英文句末标点 + 分号。**不切逗号**——
# "公司违法辞退我，医疗期怎么算" 是一个问题，切在逗号上会得到半句话。
_SENTENCE_END = re.compile(r"[。！？!?；;]")


class ChatHistoryError(ValueError):
    """业务校验失败（接口层转 400）。"""


class ChatHistoryNotFound(LookupError):
    """会话不存在或已被标记删除（接口层转 404）。"""


class ChatHistoryForbidden(PermissionError):
    """无权访问（接口层转 403）。"""


# --------------------------------------------------------------------------- 纯函数
def conversation_title(query: str) -> str:
    """会话标题 = 首句提问截断。

    **不调 LLM、不让用户手动重命名**（已定的口径）：标题只是列表里用来认路的一行字，
    为它多打一次模型调用，既慢又可能把"公司违法辞退我"概括成一句谁都认不出的摘要。
    """
    text = re.sub(r"\s+", " ", (query or "").strip())
    if not text:
        return "未命名会话"
    head = _SENTENCE_END.split(text, maxsplit=1)[0].strip() or text
    return head[:TITLE_CHARS] + ("…" if len(head) > TITLE_CHARS else "")


def new_conversation_id() -> str:
    return f"conv_{uuid.uuid4().hex[:12]}"


def _iso(value: Any) -> str:
    return value.isoformat(timespec="seconds") if isinstance(value, datetime) else ""


def _json_safe(value: Any) -> Any:
    """把 ``NaN`` / ``±Inf`` 换成 ``None``。

    ``NaN`` 不是 JSON 规范里的值：SQLite 的 JSON 列是文本，``json.dumps`` 默认
    ``allow_nan=True`` 会写出裸 ``NaN`` 字面量，存得进也读得回；
    **MySQL 的 JSON 列会直接拒收**（``Invalid JSON text``）。
    而案例的 ``rerank_score`` 在重排降级时确实可能是 ``NaN`` ——
    属于"本地好好的、换库就炸"的那一类，所以在入口处一次性清掉。
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


# --------------------------------------------------------------------------- 权限
def _assert_viewer(viewer: Any) -> None:
    if viewer is None:
        raise ChatHistoryForbidden("缺少访问主体：对外接口必须传入登录用户")


def _can_access(viewer: Any, row_user_id: int | None) -> bool:
    """判定式（不抛错），供 ``append_turn`` 的降级路径用。"""
    if viewer is None:
        return False
    if getattr(viewer, "is_admin", False):
        return True
    if row_user_id is None:
        # 无归属人的会话（服务端脚本写的）归管理员 —— 与 ``docs._assert_owner`` 同口径。
        return False
    return int(row_user_id) == int(getattr(viewer, "id", -1))


def _assert_owner(viewer: Any, row_user_id: int | None) -> None:
    """访问判定，**失败即拒绝**（照 ``case_service.assert_access``）。

    刻意不返回布尔值：``draft_service`` 那套"查不到就返回 ``{'ok': False}``"的写法
    无法区分「越权」与「不存在」，而这两个必须给不同的状态码 ——
    越权给 404 会让用户在"这段会话是不是被我自己删了"上瞎猜。
    """
    _assert_viewer(viewer)
    if getattr(viewer, "is_admin", False):
        return
    if row_user_id is None:
        raise ChatHistoryForbidden("该会话无归属人（服务端生成），仅管理员可见")
    if int(row_user_id) != int(getattr(viewer, "id", -1)):
        raise ChatHistoryForbidden("无权访问他人的 AI 助手会话")


# --------------------------------------------------------------------------- 读
def _rows_of(db: Any, conversation_id: str) -> list[ChatTurn]:
    """整段会话按轮次升序取出。**一次查完**：一轮一行单查会变成 N+1。"""
    return list(db.execute(
        select(ChatTurn)
        .where(ChatTurn.conversation_id == conversation_id)
        .order_by(ChatTurn.turn_index)
    ).scalars().all())


def list_conversations(
    viewer: Any,
    *,
    user_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """会话列表。每段会话取 ``turn_index == 0`` 那一行（标题写在它上面）。

    ``user_id=None`` 表示"不按用户过滤"，**只有管理员能这么调**：
    非管理员传 None 一律抛错 —— 否则接口层少传一个参数就变成全所问答记录可见，
    而且不会报错。与 ``case_service.list_cases``「非管理员一律在 SQL 上加隔离条件」同一条纪律。

    列表时间用 ``updated_at`` 而**不是** ``created_at``：它是"最后问过一句"的时刻，
    ``append_turn`` 每追加一轮都会把首轮那行的时间推一下（见那里）。用创建时间的话，
    一段聊了一下午的会话会永远排在最底下。
    """
    _assert_viewer(viewer)
    if user_id is None and not getattr(viewer, "is_admin", False):
        raise ChatHistoryForbidden("缺少归属过滤：非管理员必须按本人过滤")

    limit = max(1, min(int(limit or 10), 200))
    offset = max(0, int(offset))
    stmt = select(ChatTurn).where(ChatTurn.turn_index == 0, ChatTurn.status == "active")
    if user_id is not None:
        stmt = stmt.where(ChatTurn.user_id == user_id)

    with session_scope() as db:
        total = int(db.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar_one() or 0)
        rows = db.execute(
            stmt.order_by(ChatTurn.updated_at.desc()).limit(limit).offset(offset)
        ).scalars().all()
        # 先转成 dict 再出 with 块：``session_scope`` 退出时会 commit，
        # ORM 实例随即过期，出去以后再读属性就是 DetachedInstanceError。
        items = [{
            "conversation_id": r.conversation_id,
            "title": r.title or "",
            "case_id": r.case_id or "",
            "created_at": _iso(r.created_at),
            "updated_at": _iso(r.updated_at),
        } for r in rows]
    return {"items": items, "count": len(items), "total": total}


def get_conversation(conversation_id: str, viewer: Any) -> dict[str, Any]:
    """整段会话（含每轮的完整 ``payload``，供前端完整重渲染）。"""
    cid = (conversation_id or "").strip()
    with session_scope() as db:
        rows = _rows_of(db, cid)
        head = rows[0] if rows else None
        # 已标记删除的会话按"不存在"处理：它已经不在列表里了，
        # 从别处点进来还要能打开才是奇怪的。
        if head is None or (head.status or "active") != "active":
            raise ChatHistoryNotFound(f"会话不存在：{cid}")
        _assert_owner(viewer, head.user_id)
        out = {
            "conversation_id": cid,
            "title": head.title or "",
            "status": head.status or "active",
            "case_id": head.case_id or "",
            "turns": [dict(r.payload or {}) for r in rows],
            "count": len(rows),
            "created_at": _iso(head.created_at),
            "updated_at": _iso(head.updated_at),
        }
    return out


# --------------------------------------------------------------------------- 写
def append_turn(
    *,
    user: Any,
    query: str,
    payload: dict[str, Any],
    conversation_id: str = "",
    case_id: str = "",
) -> dict[str, Any]:
    """把一轮问答追加进会话。**这是一个"绝不抛错"的函数**（见模块 docstring 第 2 条）。

    ``conversation_id`` 只用于**追加到已有会话**，不用来指定新会话号：
    空串 → 开一段新的；查不到 / 无权限 → 记一条 WARNING 后**另起一段**（不报错）；
    该段此前被标记删除 → 整段复活为 ``active``（用户在里面继续提问 = 撤销删除）。
    """
    body = _json_safe(dict(payload or {}))
    title = conversation_title(query)
    uid = None if user is None else getattr(user, "id", None)

    for attempt in (1, 2):
        try:
            with session_scope() as db:
                cid, idx, created = _resolve_slot(db, conversation_id, user)
                db.add(ChatTurn(
                    conversation_id=cid,
                    turn_index=idx,
                    # 标题只写在首轮那行，列表查的就是它
                    title=title if idx == 0 else "",
                    user_id=uid,
                    case_id=case_id or "",
                    status="active",
                    payload=body,
                ))
                if idx > 0:
                    # 把首轮那行的时间推到此刻 —— 列表按它排序与显示（见 list_conversations）
                    db.execute(
                        update(ChatTurn)
                        .where(ChatTurn.conversation_id == cid, ChatTurn.turn_index == 0)
                        .values(updated_at=func.now())
                    )
                    # 整段复活：往回接一段被删掉的会话，等于撤销那次删除
                    db.execute(
                        update(ChatTurn)
                        .where(ChatTurn.conversation_id == cid, ChatTurn.status != "active")
                        .values(status="active")
                    )
                db.flush()
                out = {"conversation_id": cid, "turn_index": idx,
                       "created": created, "title": title}
        except IntegrityError:
            # 两个标签页同时追问同一段会话 → 撞 (conversation_id, turn_index)。
            # 重算一次轮次即可；连续两次说明是别的约束问题，放弃但不抛。
            if attempt == 2:
                logger.exception("会话落库连续两次撞唯一约束，本轮不落库：%s", conversation_id)
                return {"conversation_id": "", "turn_index": -1, "created": False, "title": title}
            logger.warning("会话轮次撞车，重算后重试一次：%s", conversation_id)
            continue
        logger.info("RAG 会话落库：%s 第 %d 轮（%s）", out["conversation_id"], out["turn_index"],
                    "新会话" if out["created"] else "追加")
        return out
    return {"conversation_id": "", "turn_index": -1, "created": False, "title": title}


def _resolve_slot(db: Any, conversation_id: str, viewer: Any) -> tuple[str, int, bool]:
    """决定这一轮写进哪段会话的第几轮。返回 ``(会话号, 轮次, 是否新会话)``。"""
    cid = (conversation_id or "").strip()
    if not cid:
        return new_conversation_id(), 0, True

    head = db.execute(
        select(ChatTurn).where(ChatTurn.conversation_id == cid, ChatTurn.turn_index == 0)
    ).scalar_one_or_none()
    if head is None:
        logger.warning("追加的会话不存在，改为新建一段：%s", cid)
        return new_conversation_id(), 0, True
    if not _can_access(viewer, head.user_id):
        # 越权与不存在对**写**路径是同一处置：另起一段。
        # 这里不抛 403 —— 答案已经生成完了，为一次落库失败把它丢掉是本末倒置。
        logger.warning("无权写入会话 %s（归属 user_id=%s），改为新建一段", cid, head.user_id)
        return new_conversation_id(), 0, True

    nxt = int(db.execute(
        select(func.max(ChatTurn.turn_index)).where(ChatTurn.conversation_id == cid)
    ).scalar_one() or 0) + 1
    return cid, nxt, False


def delete_conversation(conversation_id: str, viewer: Any) -> dict[str, Any]:
    """标记删除整段会话。**行留着**，只把 ``status`` 改成 ``deleted``。

    重复删除返回 200（幂等）而不是 404：前端的删除按钮可能被点两次，
    第二次弹"会话不存在"会让用户以为删掉了别的东西。
    """
    cid = (conversation_id or "").strip()
    with session_scope() as db:
        rows = _rows_of(db, cid)
        head = rows[0] if rows else None
        if head is None:
            raise ChatHistoryNotFound(f"会话不存在：{cid}")
        _assert_owner(viewer, head.user_id)
        already = (head.status or "active") == "deleted"
        if not already:
            db.execute(
                update(ChatTurn).where(ChatTurn.conversation_id == cid).values(status="deleted")
            )
        out = {"ok": True, "conversation_id": cid, "turns": len(rows), "status": "deleted",
               "already": already, "note": "已标记删除，未从库中移除"}
    logger.info("会话标记删除：%s（%d 轮，%s）", cid, out["turns"], "重复删除" if already else "首次")
    return out


__all__ = [
    "TITLE_CHARS",
    "ChatHistoryError",
    "ChatHistoryForbidden",
    "ChatHistoryNotFound",
    "append_turn",
    "conversation_title",
    "delete_conversation",
    "get_conversation",
    "list_conversations",
    "new_conversation_id",
]
