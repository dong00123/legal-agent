"""任务队列：把"发起"与"执行"分开的那一层。

**为什么需要它。** 现状是 ``POST /chat`` 在请求里直接 ``await orchestrator.execute``：
一次合同审查要跑 6~9 个节点、几十秒到几分钟，于是

1. 连接被网关按超时掐断时，任务**还在跑但已无人接收结果**（SSE 那条路靠
   "客户端断开不取消任务"兜住了，同步那条路没有）；
2. API 进程重启 = 所有在跑的任务消失且不留痕迹；
3. 想扩容只能整体多开 API 副本，而每个副本都会把检索与 LLM 调用打到同一后端。

``enqueue()`` 把请求快照写进 ``la_task.state["run_request"]`` 并立刻返回
``task_id``；worker 进程认领并执行。任务因此变成**可恢复的持久化作业**。

三条刻意的设计：

1. **数据库是唯一真源，broker 只是快通道。**
   入队先写库，再（启用时）往 RabbitMQ 发一条只含 ``task_id`` 的通知。
   worker 即使丢掉通知、或 broker 根本没启用，也能靠轮询把任务捞回来 ——
   反过来（先发消息、失败才写库）会出现"消息到了但库里没有这行"的幽灵任务。
2. **认领是一次带条件的 UPDATE，不是先查后改。**
   ``UPDATE ... WHERE status='pending'`` 的 ``rowcount`` 是唯一的裁决依据。
   "先 SELECT 看看再 UPDATE" 在多 worker 下会让同一个任务被执行两次 ——
   两次 LLM 调用、两次归档写入，而结果看起来只是"慢"。
3. **``run_request`` 是任务"被入队过"的标记。**
   内联路径（``/chat`` 直接跑）也会先建一行 ``status='pending'``，
   它没有这个键。worker 只认领带这个键的行，否则会和内联执行撞车：
   同一个 task_id 被跑两遍，而两边都以为自己是对的。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update

from backend.app.config import settings
from backend.app.db.models.task import TaskRecord
from backend.app.db.session import session_scope
# **常量从 task_service 引入，不在这里另定义一份。** 保住 ``run_request``
# 的逻辑住在 ``save_snapshot`` 里（见 ``_carry_queue_meta``），
# 两处各写一个字面量的话，改一处漏一处就会退化成"任务重试时认领不到"。
from backend.app.services.task_service import QUEUED_KEY, RUN_REQUEST_KEY

logger = logging.getLogger("legal_agent.services.task_queue")

# 一次轮询最多捞多少条。轮询间隔由 worker 控制，这里只防"一次拉太多"。
DEFAULT_BATCH = 20

# 判定"卡死在 running"的阈值（秒）。``None`` 之外的取值语义见
# :func:`effective_stale_seconds`：**0 表示不回收**。
DEFAULT_STALE_SECONDS = 0.0


def effective_stale_seconds() -> float:
    """算出"多久没动就算卡死"的阈值。``0`` = 不回收。

    默认取 ``WORKER_TASK_TIMEOUT`` 的两倍，而不是写死一小时：
    超时机制会在 ``WORKER_TASK_TIMEOUT`` 秒掐掉一次执行并放回队列，
    所以超过两倍时间还在 ``running`` 的必然是进程被杀了。
    写死成固定值的风险是"有人把超时调到 4 小时之后，那个固定值就不够用了"，
    于是回收逻辑开始误伤正常的长任务。

    **超时被关掉时（``WORKER_TASK_TIMEOUT<=0``）一律返回 0（不回收）。**
    这时候没有任何机制能把长任务和死任务区分开 —— 一个合法跑 3 小时的任务
    与一个卡了 3 小时的任务在库里长得一模一样。宁可让运维看到
    ``stale_running`` 报警后手工处理，也不要自动放回去造成并发重复执行。
    """
    if settings.WORKER_STALE_SECONDS > 0:
        return float(settings.WORKER_STALE_SECONDS)
    if settings.WORKER_TASK_TIMEOUT <= 0:
        return 0.0
    return float(settings.WORKER_TASK_TIMEOUT) * 2




def enqueue(
    request_payload: dict[str, Any],
    *,
    task_id: str,
    query: str = "",
    case_id: str = "",
    kind: str = "chat",
    intent: str = "",
    user_id: int | None = None,
    team_id: int | None = None,
    source_client: str = "api",
) -> dict[str, Any]:
    """把一次请求写进队列（= 建一行 ``pending`` 任务并把请求快照存进去）。

    ``request_payload`` 是 ``RunRequest`` 的字典形式。**要能完整重建**：
    worker 拿到它之后除了它不需要任何别的东西。所以调用方必须把
    ``material_paths``（服务端路径，已经过权限解析）也放进去 ——
    入队到执行之间隔了一段时间，那时再去解析 ``material_ids``
    会用到已经过期的权限判断。
    """
    from backend.app.services.task_service import task_service

    snapshot = {
        RUN_REQUEST_KEY: request_payload,
        QUEUED_KEY: True,
        "query": query or request_payload.get("query", ""),
    }
    created = task_service.create(
        task_id=task_id,
        kind=kind,
        intent=intent,
        query=query,
        case_id=case_id,
        user_id=user_id,
        team_id=team_id,
        source_client=source_client,
        state=snapshot,
    )
    if created is None:
        # 写库失败就**不要**发通知：发出去只会让 worker 捞到一个不存在的 id，
        # 然后反复记"任务不存在"的 warning。
        return {"queued": False, "task_id": task_id,
                "reason": "任务记录写入失败（数据库不可用？），本次没有入队"}

    notify = publish(task_id)
    return {
        "queued": True,
        "task_id": task_id,
        "notified": notify.get("published", False),
        "notify_note": notify.get("note", ""),
    }


def publish(task_id: str) -> dict[str, Any]:
    """（可选）把 task_id 发到 RabbitMQ。

    **没有 broker 也要成功。** 队列没启用时返回 ``published=False`` 并说明
    "靠轮询"，而不是抛异常 —— 入队的主要语义是"写进库里"，broker 只是快通道。

    ``aio_pika`` 没装时同理：如实说"没发通知"，而不是悄悄降级：
    静默降级会让人以为消息队列在工作，而实际上所有任务都在走轮询。
    """
    if not settings.RABBITMQ_ENABLED:
        return {"published": False, "note": "未启用消息队列（RABBITMQ_ENABLED=false），worker 靠轮询数据库领取任务"}
    try:
        import asyncio

        import aio_pika  # type: ignore[import-not-found]
    except ImportError:
        logger.warning("aio-pika 不可用，无法通知 worker（任务仍在库里，会被轮询捞到）")
        return {"published": False, "note": "未安装 aio-pika，无法发通知；任务仍在库里，worker 轮询时会领到"}

    async def _send() -> None:
        conn = await aio_pika.connect_robust(settings.RABBITMQ_URL)
        async with conn:
            channel = await conn.channel()
            await channel.declare_queue(settings.RABBITMQ_QUEUE, durable=True)
            await channel.default_exchange.publish(
                aio_pika.Message(body=task_id.encode("utf-8"),
                                 delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
                routing_key=settings.RABBITMQ_QUEUE,
            )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 同步接口里没有运行中的事件循环，自己起一个。
        asyncio.run(_send())
    else:  # pragma: no cover - 走异步路径时由调用方 await
        import asyncio as _asyncio

        task = _asyncio.get_running_loop().create_task(_send())
        task.add_done_callback(lambda t: t.exception() and logger.warning(
            "通知 worker 失败：%s", t.exception()))
    return {"published": True, "note": ""}


def peek(limit: int = DEFAULT_BATCH) -> list[dict[str, Any]]:
    """看一眼待执行的任务（不认领）。

    ``run_request`` 的过滤在 Python 侧做而不是写进 SQL：这个键在 JSON 列里，
    MySQL 与 SQLite 的 JSON 查询语法不一样，写成方言相关的一句会让
    sqlite 回落路径直接报错 —— 而 sqlite 回落正是最需要它可用的场景。
    代价是必须先捞出 ``pending`` 的行，所以 ``limit`` 只管住这一层。
    """
    try:
        with session_scope() as db:
            rows = db.execute(
                select(TaskRecord).where(TaskRecord.status == "pending")
                .order_by(TaskRecord.id.asc()).limit(limit * 5)
            ).scalars().all()
            out: list[dict[str, Any]] = []
            for r in rows:
                state = r.state or {}
                if isinstance(state, dict) and state.get(RUN_REQUEST_KEY):
                    out.append({
                        "task_id": r.task_id,
                        "kind": r.kind,
                        "intent": r.intent,
                        "query": r.query,
                        "case_id": r.case_id,
                        "user_id": r.user_id,
                        "team_id": r.team_id,
                        "source_client": r.source_client,
                        "request": dict(state[RUN_REQUEST_KEY]),
                    })
                if len(out) >= limit:
                    break
            return out
    except Exception as exc:  # noqa: BLE001
        # 轮询是常驻循环里的一步，一次查询失败不该让 worker 退出：
        # 数据库抖一下（或还没建表）就退出，等于把容错做成了单点。
        logger.warning("查询待执行任务失败：%s: %s", type(exc).__name__, exc)
        return []


def claim(task_id: str, worker_id: str) -> bool:
    """认领任务。**只有把 ``pending`` 改成 ``running`` 成功的那一方能执行它。**

    返回 ``True`` 表示认领成功，调用方现在是这个任务唯一的执行者。
    """
    try:
        with session_scope() as db:
            result = db.execute(
                update(TaskRecord)
                .where(TaskRecord.task_id == task_id, TaskRecord.status == "pending")
                .values(status="running", progress=1,
                        error=f"worker:{worker_id}"[:200])
            )
            claimed = int(getattr(result, "rowcount", 0) or 0) == 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("认领任务失败：%s | %s: %s", task_id, type(exc).__name__, exc)
        return False

    if claimed:
        # 认领成功后把它自己刚写进 ``error`` 的 worker 标记擦掉。
        # 那一列是给律师看的"这次为什么失败"，长期留着 ``worker:host:123``
        # 会出现在任务详情里。
        #
        # **必须传 ``clear_error=True``。** `set_progress` 默认只写 progress 与 status，
        # 不动 error —— 原先这里注释写着"用一次正常的状态写入把标记清掉"，
        # 而那一行其实什么都没清，标记一直留到任务结束或下一次写 error 为止。
        # 这类"注释描述了一个不存在的清理"是本项目最费时间的一种假象：
        # 代码看着已经处理过了，实际从没生效。
        try:
            from backend.app.services.task_service import task_service

            task_service.set_progress(task_id, 1, status="running", clear_error=True)
        except Exception:  # noqa: BLE001
            pass
    return claimed


def release(task_id: str, reason: str) -> None:
    """把任务放回 ``pending``（执行器崩溃/无法重建请求时用）。

    **不直接标 failed**：入队过的任务没能执行通常不是任务本身的问题
    （worker 缺少依赖、请求快照读不出来），标 failed 会让律师以为
    "这个案子分析失败了"，而实际上重试一次就好。
    """
    try:
        from backend.app.services.task_service import task_service

        task_service.set_progress(task_id, 0, status="pending")
        logger.warning("任务已放回队列待重试：%s（%s）", task_id, reason)
    except Exception as exc:  # noqa: BLE001
        logger.error("放回队列失败：%s | %s", task_id, exc)


def requeue_stale(older_than_seconds: float | None = None) -> list[str]:
    """把卡死在 ``running`` 的任务放回 ``pending``，返回被放回的 task_id 列表。

    **为什么需要它。** 进程被强杀（容器重启、OOM、Windows 上 ``docker stop``）
    时任务会永远停在 ``running``：:func:`peek` 只捞 ``pending``，
    于是它既不会被重试，也没有任何报错，只表现为"队列还没轮到"。
    本机现在就躺着两条（19:02 与 20:10 留下的），而没有任何界面能看出它们死了。

    **阈值必须显著大于任务超时**（见 :func:`effective_stale_seconds`）。
    阈值太小会把**正在跑**的长任务也放回去，接着被另一个 worker 认领 ——
    同一任务被并发执行两遍，结果互相覆盖，比"卡住"更难查。

    **只在 worker 启动时调用，不在轮询里调用。** 放在轮询里等于每 5 秒
    重算一次全表，而它一天真正起作用的机会只有进程刚重启那一次。

    ``older_than_seconds`` 传 ``None`` 用有效阈值；传 ``<=0`` 表示**关闭回收**
    （返回空列表）。测试里若要"刚认领的就当卡死"，传一个极小数（如 ``0.001``）
    而不是 ``0`` —— ``0`` 是"关闭"，不是"全部"。
    """
    seconds = effective_stale_seconds() if older_than_seconds is None else float(older_than_seconds)
    if seconds <= 0:
        logger.info("卡死任务回收已关闭（WORKER_TASK_TIMEOUT 未设上限或阈值配成 0），"
                    "跳过；running 超时的任务只能人工处理")
        return []

    cutoff = datetime.now() - timedelta(seconds=seconds)
    try:
        with session_scope() as db:
            rows = db.execute(
                select(TaskRecord).where(TaskRecord.status == "running")
            ).scalars().all()
            stale = [r for r in rows
                     if r.updated_at is None or r.updated_at < cutoff]
            ids: list[str] = []
            for r in stale:
                state = r.state if isinstance(r.state, dict) else {}
                if not state.get(RUN_REQUEST_KEY):
                    # 没有请求快照 = 放回 ``pending`` 也没人能执行它
                    # （``peek`` 不认这种行）。这类是"内联执行被中断"留下的，
                    # 它们本来就不该被 worker 认领，只标 failed 收尾，
                    # 免得 ``running`` 里堆一堆永远不动的行。
                    r.status = "failed"
                    r.error = (r.error or "") or "执行进程中断且无队列快照，无法重试"
                    r.finished_at = datetime.now().isoformat(timespec="seconds")
                    logger.warning("清理卡死的内联任务（无队列快照）：%s", r.task_id)
                    continue
                r.status = "pending"
                r.progress = 0
                r.error = ""     # 上一轮的 worker 标记不该留给律师看
                ids.append(r.task_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("回收卡死任务失败：%s: %s", type(exc).__name__, exc)
        return []

    if ids:
        logger.warning("已把 %d 条卡在 running 的任务放回队列：%s", len(ids), "、".join(ids))
    return ids


def stale_running(older_than_seconds: float | None = None) -> list[dict[str, Any]]:
    """列出卡死在 ``running`` 的任务（**只读，不改状态**）。

    给 ``/health`` 用。"队列里有没有东西卡住"必须能一眼看到 ——
    否则唯一的发现途径是"律师反馈任务一直转圈"，那时已经过了很久。
    """
    seconds = effective_stale_seconds() if older_than_seconds is None else float(older_than_seconds)
    if seconds <= 0:
        # 阈值关闭时**仍然列出所有 running**，只是不叫它们"卡死"。
        # 直接返回空会让人以为"没有卡住的任务"，而实际上只是判据关掉了 ——
        # 这两件事在界面上必须能分开。
        #
        # 用 ``datetime.max`` 而不是 ``datetime.min``：下面判的是
        # ``updated_at < cutoff``，"全部算老"要的是 cutoff 大于所有时间戳。
        # 写成 min 的话恒为 False —— 结果是"列出全部"变成"列出零条"，
        # 而这个 bug 不会报错，只会让界面永远显示"没有卡住的任务"。
        cutoff = datetime.max
    else:
        cutoff = datetime.now() - timedelta(seconds=seconds)
    try:
        with session_scope() as db:
            rows = db.execute(
                select(TaskRecord).where(TaskRecord.status == "running")
                .limit(200)
            ).scalars().all()
    except Exception as exc:  # noqa: BLE001
        logger.debug("查询卡死任务失败：%s", exc)
        return []
    return [
        {
            "task_id": r.task_id,
            "kind": r.kind,
            "progress": r.progress,
            "updated_at": r.updated_at.isoformat(timespec="seconds") if r.updated_at else "",
            "has_request": bool((r.state or {}).get(RUN_REQUEST_KEY)
                                if isinstance(r.state, dict) else False),
        }
        for r in rows
        if r.updated_at is None or r.updated_at < cutoff
    ]


def queue_stats() -> dict[str, Any]:
    """队列概览。给 ``/health`` 和运维用 —— "积压了多少"必须能一眼看到。"""
    from sqlalchemy import func

    try:
        with session_scope() as db:
            rows = db.execute(
                select(TaskRecord.status, func.count())
                .group_by(TaskRecord.status)
            ).all()
            by_status = {str(k or "unknown"): int(v) for k, v in rows}
            pending_rows = db.execute(
                select(TaskRecord.state).where(TaskRecord.status == "pending").limit(500)
            ).scalars().all()
        queued = sum(
            1 for s in pending_rows
            if isinstance(s, dict) and s.get(RUN_REQUEST_KEY)
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}

    pending = by_status.get("pending", 0)
    # ``pending`` 与 ``queued`` 的差 = **没带请求快照的 pending 行**。
    # 那是一种异常：它们永远不会被 worker 认领（``peek`` 不认），
    # 却和正常排队一起被算进 ``pending``。不单独报出来的话，
    # "积压 20 条"里可能有 18 条永远不会被执行。
    orphans = max(0, pending - queued)
    stale = stale_running()
    threshold = effective_stale_seconds()
    out: dict[str, Any] = {
        "by_status": by_status,
        "pending": pending,
        "queued": queued,
        "running": by_status.get("running", 0),
        "stale_running": stale,
        "stale_running_count": len(stale),
        "stale_threshold_seconds": threshold,
        "broker": {
            "enabled": settings.RABBITMQ_ENABLED,
            "url": settings.RABBITMQ_URL,
            "queue": settings.RABBITMQ_QUEUE,
            "note": "未启用时 worker 靠轮询数据库领取任务",
        },
        "caliber": "queued = pending 且 state.run_request 非空（= 通过 /chat?enqueue=true 入队的）",
    }
    if orphans:
        out["pending_without_request"] = orphans
        out["warning"] = (f"{orphans} 条 pending 任务没有请求快照，worker 认领不到；"
                          f"跑一次 worker --requeue-stale 会把它们标成失败并清理")
    if stale:
        out["warning_stale"] = (
            f"{len(stale)} 条任务卡在 running 超过 "
            f"{'阈值已关闭' if threshold <= 0 else f'{threshold:.0f} 秒'}"
            f"；worker 启动时会自动回收（--requeue-stale）"
        )
    if threshold <= 0:
        out["stale_note"] = ("卡死判定已关闭（WORKER_TASK_TIMEOUT 未设上限）："
                             "上面的 stale_running 列出的是全部 running 任务，不代表都卡住了")
    return out



def request_to_json(payload: dict[str, Any]) -> str:
    """给日志用的紧凑表示（不打印材料正文）。"""
    slim = {k: v for k, v in payload.items() if k not in ("material_text",)}
    if payload.get("material_text"):
        slim["material_text"] = f"<{len(payload['material_text'])} 字>"
    return json.dumps(slim, ensure_ascii=False, default=str)


__all__ = ["DEFAULT_BATCH", "DEFAULT_STALE_SECONDS", "QUEUED_KEY", "RUN_REQUEST_KEY",
           "claim", "effective_stale_seconds", "enqueue", "peek", "publish",
           "queue_stats", "release", "request_to_json", "requeue_stale",
           "stale_running"]
