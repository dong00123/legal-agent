"""任务 worker 进程。

与 API 进程的关系：

- **同一套业务代码**。worker 不重新实现编排，它调的是 API 用的那个
  ``orchestrator.execute``；两边共用 ``backend.app.services`` 全部服务。
  分成两个进程只是因为"跑几分钟"与"响应 HTTP"对进程的要求不同。
- **同一个数据库**。任务真源是 ``la_task``，不是内存队列，也不是 RabbitMQ。
  所以 worker 可以随时重启、可以多开、可以只在需要时启动。

启动：

.. code-block:: bash

    python -m backend.worker.app              # 常驻，5 秒一轮
    python -m backend.worker.app --once       # 只扫一轮（测试/cron）
    python -m backend.worker.app --interval 1 --max-tasks 1

``backend/worker/app.py`` 是入口；本模块**不**在这里 import 它，
否则 ``python -m backend.worker.app`` 会变成"先 import 包再执行同名子模块"，
多绕一层且让 ``--help`` 的 prog 名变难看。
"""

__all__: list[str] = []
