"""周期进化的进程内调度器。

**为什么需要它**：``state["scheduler"]``（``enabled`` / ``interval_days``）此前只是
一个配置标记 —— 全仓没有任何地方读它去触发 ``run_cycle``，于是监控中心上那句
「周级调度：开启」名不副实：不点「触发一轮周期」，飞轮永远不会自己前进。
本模块把那个标记变成真的会到点执行。

**边界（必须说清）**：

- 这是**进程内**调度，不是分布式调度。到期时间持久化在 state 里、且执行前会
  "先占位再执行"（见 ``EvolutionService.scheduler_tick``），所以单机多副本不会
  重复跑同一轮；跨机器的强一致要交给外部队列 / 分布式锁，不在本模块职责内。
- 有两级开关：``EVOLUTION_SCHEDULER_ENABLED=false`` → 进程内彻底不启动；
  state 里 ``scheduler.enabled=false`` → 调度器照常跑，但每次检查直接跳过。
  前者是运维的急停钮，后者是"暂停进化但不重启服务"。
- **pytest 下不启动**：测试不该被后台循环改状态，哪怕它只写一个 ``next_run_at``。
"""

from __future__ import annotations

import asyncio
import logging
import sys

from backend.app.services.evolution_service import evolution_service

logger = logging.getLogger("legal_agent.services.evolution_scheduler")


class EvolutionScheduler:
    """按 ``tick_seconds`` 周期醒来，问一次 "到点了没"，到点就交给服务层跑。"""

    def __init__(self, *, tick_seconds: float = 60.0) -> None:
        self._tick = max(1.0, float(tick_seconds))
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> bool:
        """在当前事件循环里启动。已在运行则幂等返回 ``False``。"""
        if self.running:
            return False
        try:
            self._task = asyncio.create_task(self._loop(), name="evolution-scheduler")
        except RuntimeError as exc:  # 没有运行中的事件循环
            logger.warning("周期进化调度器未启动（没有事件循环）：%s", exc)
            return False
        logger.info("周期进化调度器已启动：每 %.0f 秒检查一次到期", self._tick)
        return True

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        logger.info("周期进化调度器已停止")

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._tick)
            try:
                # ``scheduler_tick`` 是同步的（读写 state + 一次 run_cycle 要做 N 个
                # Skill 的健康诊断），必须丢线程池，否则会把事件循环整个卡住。
                result = await asyncio.to_thread(evolution_service.scheduler_tick)
            except Exception as exc:  # noqa: BLE001 - 单次失败绝不能让调度线程死掉
                logger.warning("周期进化调度失败（下一轮会重试）：%s", exc)
                continue
            if result.get("action") == "ran":
                cycle = result.get("cycle") or {}
                logger.info(
                    "周期进化已执行：新建 %d 个任务 / 跳过 %d 个；下次到期 %s",
                    len(cycle.get("tasks_created") or []),
                    len(cycle.get("tasks_skipped") or []),
                    result.get("next_run_at"),
                )


_scheduler: EvolutionScheduler | None = None


def start_evolution_scheduler(*, tick_seconds: float = 60.0) -> bool:
    """启动单例（lifespan 调用）。pytest 环境下直接跳过。"""
    global _scheduler
    if "pytest" in sys.modules:
        logger.info("检测到 pytest 环境，跳过周期进化调度器")
        return False
    if _scheduler is None:
        _scheduler = EvolutionScheduler(tick_seconds=tick_seconds)
    return _scheduler.start()


async def stop_evolution_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        await _scheduler.stop()
