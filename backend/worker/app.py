"""任务 worker：从队列里认领任务并执行。

**为什么要把执行从 API 进程里搬出来。** 现状是 ``POST /chat`` 在请求里
直接 ``await orchestrator.execute``：一次合同审查要跑 6~9 个节点、几十秒到几分钟。
于是

1. 网关按超时掐断连接时，任务**还在跑但已无人接收结果**；
2. API 进程重启 = 所有在跑的任务消失，库里永远停在 ``running``；
3. 想提高吞吐只能整体多开 API 副本 —— 每个副本都自带一套路由与静态文件服务。

本模块是一个**独立进程**：认领 → 执行 → 落库。启动方式见 ``python -m backend.worker.app``。

--------------------------------------------------------------------------
四条刻意的设计

**一、认领用带条件的 UPDATE，不是"先查后改"。**
:func:`task_queue.claim` 把 ``UPDATE ... WHERE status='pending'`` 的 ``rowcount``
当唯一裁决依据。多 worker 同时看到同一条任务是常态（它们各自 ``peek``），
"先看状态再改"会让两个 worker 都认为自己该跑 —— 两次 LLM 调用、两次归档写入，
而表现只是"慢"，日志里没有任何异常。

**二、数据库是唯一真源，broker 只是快通道。**
RabbitMQ 启用时它只负责"让 worker 立刻醒过来"，不负责"任务不能丢"：
消息发丢了、worker 重启期间消息过期了，轮询都会在后一个间隔把它捞回来。
所以下面 :func:`_wait_for_work` 的两种返回（"收到通知" / "睡够了"）
走的是**同一条**处理路径 —— 没有"只有 broker 那条路才会正确"的分支。

**三、跑不动的任务放回 ``pending``，不标 failed。**
worker 缺依赖、请求快照读不出来、超时被掐 —— 这些都不是"这个案子分析失败了"。
标 failed 会让律师在任务中心看到一次假的失败，还可能据此重新发起、
把队列搞得更堵。:func:`task_queue.release` 把它们放回队列，下一轮重试。

**四、超时掐断后**立即**放回队列，不等下一轮。**
``WORKER_TASK_TIMEOUT`` 命中时任务已经被 ``claim`` 成 ``running``，
不主动放回的话它会一直卡在 ``running``：既不会被重试（:func:`peek` 只捞
``pending``），又没有任何报错 —— 表现为队列吞吐莫名下降。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import socket
import time
from typing import Any

from backend.app.config import settings
from backend.app.logging_conf import setup_logging
from backend.app.services import contract_parse_service, task_queue
from backend.app.services.orchestrator import RunRequest, orchestrator

logger = logging.getLogger("legal_agent.worker")


# --------------------------------------------------------------------------- 身份
def default_worker_id() -> str:
    """``主机名:pid``。

    **必须带上主机名。** 多机部署时只写 pid 会撞号（两台机器都有 pid 12345），
    而 worker_id 会写进任务日志用于追溯"这条任务是谁跑的" ——
    撞号会让排查直接走错机器。
    """
    return f"{socket.gethostname()}:{os.getpid()}"


# --------------------------------------------------------------------------- 执行
async def run_one(job: dict[str, Any], worker_id: str) -> str:
    """执行一条已认领的任务，返回结果字符串。

    返回值：``succeeded`` / ``degraded`` / ``failed`` / ``released`` ——
    前三者与 ``la_task.status`` 一一对应（``degraded`` 见
    :func:`_run_contract_parse`），``released`` 表示"已放回队列待重跑"。

    调用前必须已经 ``claim`` 成功 —— 本函数不做二次校验，
    因为"再查一次状态"就是"先查后改"，而那正是要避免的写法。

    **按 ``kind`` 分流。** 解析任务（``kind=contract_parse``）不是编排任务：
    它没有意图、不跑节点、不落证据，快照也不是 ``RunRequest``。
    所以分流必须在这里、在 ``RunRequest.from_payload`` **之前** ——
    否则解析任务会被判成"请求快照无法重建"直接标 failed，
    而真正的原因是这个函数只认识一种任务。
    """
    task_id = str(job.get("task_id") or "")
    if str(job.get("kind") or "") == contract_parse_service.KIND:
        return await _run_contract_parse(job, worker_id)

    try:
        req = RunRequest.from_payload(job.get("request"))
    except ValueError as exc:
        # 快照坏了（常见于：入队时 task_id 记错、或 JSON 列被外部改过）。
        # **标 failed 而不是放回队列**：放回去下一轮还是一样的结果，
        # 只会把同一条坏数据反复捞起来。这里要让它显式失败并留下原因。
        logger.error("任务 %s 的请求快照无法重建：%s", task_id, exc)
        _fail(task_id, f"请求快照无法重建：{exc}")
        return "failed"

    logger.info("开始执行任务 %s：intent=%s kind=%s client=%s query=%s",
                task_id, req.intent or "auto", job.get("kind") or "",
                req.client, (req.query or "")[:60])

    started = time.monotonic()
    try:
        # ``persist=True``：落库、沉淀引用与草稿、跑归档节点都由编排层做。
        # worker **不重复实现**这条路 —— 两份实现迟早分叉，
        # 而分叉后的表现是"队列跑出来的任务少了一段收尾"，报表上看不出来。
        await _execute_with_timeout(req)
    except asyncio.TimeoutError:
        # 见模块 docstring 第 4 条：**立刻**放回，不等下一轮。
        elapsed = int((time.monotonic() - started) * 1000)
        logger.warning("任务 %s 超过 %.0f 秒仍未结束，已放回队列（已跑 %.1f 秒）",
                       task_id, settings.WORKER_TASK_TIMEOUT, elapsed / 1000)
        task_queue.release(task_id, f"执行超时（>{settings.WORKER_TASK_TIMEOUT:.0f}s）")
        return "released"
    except Exception as exc:  # noqa: BLE001
        # 走到这里说明 ``orchestrator.execute`` 自己抛了 —— 它内部已经把
        # 节点异常收敛成证据缺口并落库了，能抛到这一层的多半是环境问题
        # （磁盘满、解析器崩、DB 断了）。**放回队列**而不是标 failed：
        # 环境问题重试一次往往就好了，而失败结论会直接呈现给律师。
        logger.exception("任务 %s 执行时异常：%s", task_id, exc)
        task_queue.release(task_id, f"执行异常：{type(exc).__name__}: {exc}")
        return "released"

    elapsed = int((time.monotonic() - started) * 1000)
    logger.info("任务 %s 执行结束，耗时 %.1f 秒", task_id, elapsed / 1000)
    return "succeeded"


async def _execute_with_timeout(req: RunRequest) -> None:
    """带超时地跑一次。``WORKER_TASK_TIMEOUT<=0`` 表示不限。"""
    if settings.WORKER_TASK_TIMEOUT <= 0:
        await orchestrator.execute(req)
        return
    await asyncio.wait_for(
        orchestrator.execute(req),
        timeout=float(settings.WORKER_TASK_TIMEOUT),
    )


async def _run_contract_parse(job: dict[str, Any], worker_id: str) -> str:
    """执行一条 ``kind=contract_parse`` 的解析任务。

    超时 / 异常的处理与编排任务**同一套纪律**（照抄 :func:`run_one` 的取舍，
    不另立一套）：超时立刻放回队列；异常也放回（环境问题重试往往就好），
    只有"这份快照重试一百次也没用"才标 failed。

    **超时放回之后重跑是安全且便宜的**：已解析完的材料按文件内容哈希命中缓存，
    真正重做的只有那几份还没跑完的（见 ``contract_parse_service`` docstring 第二条）。
    没有这条保证，"超时重跑"就等于把已经花掉的三分钟再花一遍，
    而且会把已经落盘的正文改掉（多模态识别本身有随机性）。
    """
    task_id = str(job.get("task_id") or "")
    payload = job.get("request")
    # 只认**已入库**的材料：受理时被拒收的（.exe 之类）不参与解析，
    # 也不该让"整批都被拒"被看成"有活可干"。受理层已保证至少一份入库。
    entries = [e for e in ((payload or {}).get("entries") or [])
               if isinstance(e, dict) and e.get("stored")] if isinstance(payload, dict) else []
    if not entries:
        # 快照里没有材料清单 = 这条任务没法执行。**标 failed 而不是放回**：
        # 放回去下一轮捞到的还是同一份坏快照，只会被反复认领
        # （``peek`` 只捞带 ``run_request`` 的 pending 行，所以结构没问题，
        #  问题是内容——重试改变不了这一点）。
        logger.error("解析任务 %s 的快照里没有可解析的材料，无法执行", task_id)
        _fail(task_id, "解析快照缺少可解析的材料（entries 为空或全部未入库）")
        return "failed"

    logger.info("开始执行解析任务 %s：材料 %d 份（worker=%s）",
                task_id, len(entries), worker_id)
    started = time.monotonic()
    try:
        summary = await _parse_with_timeout(task_id, payload)
    except asyncio.TimeoutError:
        elapsed = int((time.monotonic() - started) * 1000)
        logger.warning("解析任务 %s 超过 %.0f 秒仍未结束，已放回队列（已跑 %.1f 秒）",
                       task_id, settings.WORKER_TASK_TIMEOUT, elapsed / 1000)
        task_queue.release(task_id, f"解析超时（>{settings.WORKER_TASK_TIMEOUT:.0f}s）")
        return "released"
    except Exception as exc:  # noqa: BLE001
        logger.exception("解析任务 %s 执行时异常：%s", task_id, exc)
        task_queue.release(task_id, f"解析异常：{type(exc).__name__}: {exc}")
        return "released"

    # 返回值与 ``la_task.status`` 对齐（degraded = 部分材料读不出字）。
    # 对齐的意义是"worker 日志里的计数"与"律师在任务中心看到的状态"一致，
    # 两处口径不同时，排查会先去怀疑统计而不是数据。
    return str(summary.get("status") or "succeeded")


async def _parse_with_timeout(task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """带超时地跑一次材料解析（与 :func:`_execute_with_timeout` 同一取舍）。"""
    if settings.WORKER_TASK_TIMEOUT <= 0:
        return await contract_parse_service.run_job(task_id, payload)
    return await asyncio.wait_for(
        contract_parse_service.run_job(task_id, payload),
        timeout=float(settings.WORKER_TASK_TIMEOUT),
    )


def _fail(task_id: str, reason: str) -> None:
    """把任务标成失败（只用于"重试也没用"的情况，见 :func:`run_one`）。"""
    try:
        from backend.app.services.task_service import task_service

        task_service.finish(task_id, status="failed", error=reason)
    except Exception as exc:  # noqa: BLE001
        logger.error("标记任务失败时出错：%s | %s", task_id, exc)


# --------------------------------------------------------------------------- 唤醒
class _Broker:
    """RabbitMQ 的**可选**唤醒通道。

    **它不承载任务本身**，消息体只有一个 ``task_id``。这样即使消息丢了、
    或者队列被 purge 了，任务也不会丢 —— 它们全在数据库里，轮询兜底。
    反过来（把请求体塞进消息、以消息为准）就会出现"消息到了但库里没有这行"，
    任务执行完却无法落库。
    """

    def __init__(self) -> None:
        self.connected = False
        # 只留最后一条通知。存成列表在常驻进程里就是一处缓慢的内存增长 ——
        # 而它唯一的作用是打一行 debug 日志，值不上。
        self.last_signal = ""
        self._conn: Any = None
        self._channel: Any = None
        self._queue: Any = None

    async def connect(self) -> bool:
        if not settings.RABBITMQ_ENABLED:
            # **如实说没连**，不要假装已启用。静默降级会让人以为
            # "任务靠消息即时唤醒"，实际是 5 秒一轮的轮询 —— 排查延迟问题时会找不到原因。
            logger.info("未启用消息队列（RABBITMQ_ENABLED=false），worker 按 %.1f 秒轮询数据库",
                        settings.WORKER_POLL_SECONDS)
            return False
        try:
            import aio_pika  # type: ignore[import-not-found]
        except ImportError:
            logger.warning("已设置 RABBITMQ_ENABLED=true 但未安装 aio-pika，回落为轮询"
                           "（pip install aio-pika，或把 RABBITMQ_ENABLED 改回 false 消除这条警告）")
            return False
        try:
            self._conn = await aio_pika.connect_robust(settings.RABBITMQ_URL)
            self._channel = await self._conn.channel()
            await self._channel.set_qos(prefetch_count=1)
            self._queue = await self._channel.declare_queue(
                settings.RABBITMQ_QUEUE, durable=True)
            self.connected = True
            logger.info("已连接消息队列：%s（队列 %s）",
                        settings.RABBITMQ_URL, settings.RABBITMQ_QUEUE)
            return True
        except Exception as exc:  # noqa: BLE001
            # 连不上 broker **不应该让 worker 退出**：它的职责是执行任务，
            # 而任务是能从数据库里轮询到的。broker 只是让延迟从 5 秒变成瞬时。
            logger.warning("连接消息队列失败：%s: %s；继续按轮询模式运行",
                           type(exc).__name__, exc)
            return False

    async def wait(self, timeout: float) -> str:
        """等一条通知或等到超时。返回 ``"notified"`` / ``"timeout"``。

        用 ``asyncio.wait_for`` 包 ``get()``，而不是用 aio-pika 的 ``fail``/``timeout``
        参数：那两个 kwarg 在不同大版本间改过名，而这条路径**没装 aio-pika
        就完全跑不到**（本机 RABBITMQ_ENABLED=false），靠参数名写对是赌运气。
        空队列在 ``get()`` 里表现为抛异常（``QueueEmpty``），一并当超时处理。
        """
        if not self.connected or self._queue is None:
            await asyncio.sleep(timeout)
            return "timeout"
        try:
            message = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            await message.ack()
            self.last_signal = str(message.body.decode("utf-8", "replace"))
            return "notified"
        except asyncio.TimeoutError:
            return "timeout"
        except Exception as exc:  # noqa: BLE001
            # QueueEmpty 也走这里（等到了超时但队列是空的）。区分它没有意义：
            # 两种情况下一轮都要靠 ``peek`` 兜底，而 ``peek`` 才是真源。
            # **连接类错误要断开标记**，否则下一轮还会立刻再撞一次。
            if not _is_empty(exc):
                logger.warning("等待队列通知失败：%s: %s；本轮回落到轮询",
                               type(exc).__name__, exc)
                self.connected = False
            await asyncio.sleep(timeout)
            return "timeout"

    async def close(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()
        self.connected = False


def _is_empty(exc: BaseException) -> bool:
    """是不是"队列空"这类正常情况（而非连接故障）。

    按类名判而不是 ``isinstance``：``aio_pika.exceptions.QueueEmpty``
    要 import aio_pika 才拿得到，而没装它的时候这个函数也必须是安全的。

    只认 ``QueueEmpty``。看起来相关的 ``ChannelInvalidStateError`` **不算**：
    那是通道已失效，属于要记警告并断开标记的真故障 —— 把它当"队列空"
    会让 broker 掉线后日志里一片安静，而任务悄悄退回 5 秒轮询。
    """
    return type(exc).__name__ == "QueueEmpty"


# --------------------------------------------------------------------------- 主循环
async def serve(
    *,
    worker_id: str = "",
    interval: float | None = None,
    batch: int | None = None,
    once: bool = False,
    max_tasks: int = 0,
    use_broker: bool = True,
    requeue_stale: bool = True,
    requeue_all: bool = False,
) -> dict[str, int]:
    """主循环。返回本次运行的计数（``claimed`` / ``succeeded`` / ``failed`` / ``released``）。

    ``once=True`` 时只扫一轮就返回 —— 端到端测试与 cron 式调度都用它，
    不需要起一个常驻进程再想办法杀掉。
    """
    worker_id = worker_id or default_worker_id()
    interval = float(settings.WORKER_POLL_SECONDS if interval is None else interval)
    batch = int(settings.WORKER_BATCH if batch is None else batch)

    logger.info("worker 启动：id=%s 轮询间隔=%.1fs 批量=%d%s",
                worker_id, interval, batch,
                "" if settings.WORKER_TASK_TIMEOUT > 0
                else "（单任务超时已关闭，卡住的任务会一直占住名额）")

    # 回收卡死的任务是**启动时**的一次性动作，不进轮询（见 ``requeue_stale``）。
    if requeue_stale:
        try:
            # ``requeue_all`` 用极小的阈值（而不是 0）来表达"连刚认领的也算"：
            # ``requeue_stale(0)`` 的语义是**关闭回收**，不是"全部回收"。
            recovered = task_queue.requeue_stale(
                older_than_seconds=0.001 if requeue_all else None)
            if recovered:
                logger.warning("上次进程被中断留下的 %d 条任务已放回队列：%s",
                               len(recovered), "、".join(recovered))
        except Exception as exc:  # noqa: BLE001
            logger.warning("回收卡死任务失败（不影响新任务执行）：%s: %s",
                           type(exc).__name__, exc)
    elif requeue_all:
        logger.warning("--requeue-all 与 --no-requeue-stale 同时给出，回收被跳过"
                       "（--no-requeue-stale 优先）")

    broker = _Broker()
    if use_broker:
        await broker.connect()

    counts = {"claimed": 0, "succeeded": 0, "failed": 0, "released": 0,
              # 解析任务的"部分成功"（见 ``_run_contract_parse``）。
              # 预置成 0 是为了让退出时的日志**每次都印出这一项** ——
              # 只在出现时才动态长出来的键，事后没人想得起来它代表什么。
              "degraded": 0}
    stop = _StopFlag()
    stop.install()

    try:
        while not stop.stopped:
            jobs = task_queue.peek(limit=batch)
            did_work = False

            for job in jobs:
                if stop.stopped:
                    break
                if not task_queue.claim(str(job.get("task_id") or ""), worker_id):
                    # 没抢到 = 另一个 worker 先改了状态。**这是正常情况，不记 warning**：
                    # 多 worker 下每轮都会发生，记成警告只会把真问题淹没。
                    continue
                counts["claimed"] += 1
                did_work = True
                outcome = await run_one(job, worker_id)
                counts[outcome] = counts.get(outcome, 0) + 1
                if max_tasks and counts["claimed"] >= max_tasks:
                    logger.info("已达本次上限 %d 条，退出", max_tasks)
                    return counts

            if once:
                break

            # 本轮有活干 → 立刻再查一次（队列里可能还有）；空转 → 等唤醒或睡到下一轮。
            # 这个分支的意义是"积压时吞吐不被轮询间隔卡住"：
            # 10 条任务不会变成 10×5 秒。
            if not did_work:
                mode = await broker.wait(interval)
                if mode == "notified":
                    logger.debug("收到队列通知：%s", broker.last_signal)
                elif stop.stopped:
                    break
    finally:
        await broker.close()
        logger.info("worker 退出：%s", counts)

    return counts


class _StopFlag:
    """SIGINT/SIGTERM 的优雅退出标记。

    要接这两个信号而不是等 ``KeyboardInterrupt``：容器里 ``docker stop``
    发的是 SIGTERM，不接的话进程直接被 kill —— 正在跑的任务会停在 ``running``，
    而它已经消耗过 LLM 调用了。接住之后至少能把"当前这条跑完再走"做完。

    **Windows 上挂不上信号处理器**（``add_signal_handler`` 抛
    ``NotImplementedError``），所以 :meth:`install` 全部失败也不报错 ——
    那里靠 ``KeyboardInterrupt``，效果是"立刻停"而不是"跑完当前这条"。
    这个差别要记在日志里，否则 Windows 上"优雅退出没生效"会被当成 bug 查。
    """

    def __init__(self) -> None:
        self.stopped = False

    def install(self) -> None:
        loop = asyncio.get_running_loop()
        hooked = 0
        for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, self._on_signal)
                hooked += 1
            except (NotImplementedError, ValueError, RuntimeError):
                continue
        if hooked == 0:
            logger.info("当前平台不支持挂信号处理器，用 Ctrl+C 中断（当前任务不会等它跑完）")

    def _on_signal(self) -> None:
        logger.info("收到退出信号，等当前任务跑完就停（不会再认领新任务）")
        self.stopped = True


# --------------------------------------------------------------------------- 入口
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.worker.app",
        description="法律智能体任务 worker：从队列认领任务并执行",
    )
    parser.add_argument("--once", action="store_true",
                        help="只扫一轮就退出（端到端测试 / cron 调度用）")
    parser.add_argument("--interval", type=float, default=None,
                        help=f"轮询间隔秒（默认 {settings.WORKER_POLL_SECONDS}）")
    parser.add_argument("--batch", type=int, default=None,
                        help=f"一次认领条数（默认 {settings.WORKER_BATCH}）")
    parser.add_argument("--max-tasks", type=int, default=0,
                        help="处理满 N 条后退出，0 表示不限")
    parser.add_argument("--worker-id", default="", help="worker 标识，默认 主机名:pid")
    parser.add_argument("--no-broker", action="store_true",
                        help="即使配置了 RabbitMQ 也只用轮询（排查 broker 问题时用）")
    parser.add_argument("--no-requeue-stale", action="store_true",
                        help="启动时不回收卡在 running 的任务（默认会回收）")
    parser.add_argument("--requeue-all", action="store_true",
                        help="回收**所有** running 任务（含刚刚认领、可能真的在跑的）"
                             "；用于清理已知的死任务，会与在跑的 worker 抢同一条")
    parser.add_argument("--no-init-db", action="store_true",
                        help="跳过启动时的建表/修复外键（库已就绪时可加快启动）")
    args = parser.parse_args(argv)

    setup_logging()

    # 建表与外键修复放在 worker 启动时做一次。**不做的话**：worker 先于 API 启动时，
    # ``peek()`` 会一直查一个不存在的表，日志里只有每条轮询一行 warning，
    # 看起来像"数据库连不上"，实际是表还没建。
    if not args.no_init_db:
        try:
            from backend.app.db import session as db_session

            report = db_session.init_db()
            logger.info("数据库就绪：backend=%s 外键修复=%s",
                        db_session.db_status().get("backend"),
                        len(getattr(report, "fixed_fks", None) or []))
        except Exception as exc:  # noqa: BLE001
            # 不能让建表失败阻止 worker 启动：库可能是 API 那边稍后建的，
            # 而 ``peek`` 自己会吞掉查询异常并重试。
            logger.warning("启动时初始化数据库失败（将继续尝试轮询）：%s: %s",
                           type(exc).__name__, exc)

    try:
        counts = asyncio.run(serve(
            worker_id=args.worker_id,
            interval=args.interval,
            batch=args.batch,
            once=args.once,
            max_tasks=args.max_tasks,
            use_broker=not args.no_broker,
            requeue_stale=not args.no_requeue_stale,
            requeue_all=args.requeue_all,
        ))
    except KeyboardInterrupt:
        logger.info("已中断（KeyboardInterrupt）")
        return 130
    return 0 if counts.get("failed", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
