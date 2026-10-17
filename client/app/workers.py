"""Qt 工作线程：**网络请求一律不在主线程**。

在主线程里发请求的表现是"界面卡死，鼠标转圈"，而且**没有任何报错** ——
用户会以为程序挂了并强行退出，而那时任务其实还在服务器上跑着。

两个类：
- :class:`Worker` —— 一次性调用，跑完给结果；
- :class:`StreamWorker` —— 逐条产出（SSE），每来一条就发信号。

**异常必须走信号带回主线程**，不能在子线程里弹 QMessageBox：
Qt 的界面类只能在主线程操作，跨线程弹窗的表现是随机崩溃，
而不是"弹不出来"。
"""

from __future__ import annotations

from typing import Any, Callable, Iterator

from PySide6.QtCore import QThread, Signal


class Worker(QThread):
    """跑一个阻塞函数，结果通过信号回主线程。"""

    finished_with = Signal(object)   # 正常结果
    failed = Signal(object)          # Exception

    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def run(self) -> None:  # noqa: D102 - QThread 接口
        try:
            self.finished_with.emit(self._fn(*self._args, **self._kwargs))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(exc)


class StreamWorker(QThread):
    """跑一个生成器（SSE 流），**每来一条就发一次信号**。

    ``chunk`` 发的是原始 ``(事件名, 数据)``，让界面自己决定怎么呈现 ——
    线程层不该知道"trace 事件要显示成一行灰字"这种事。
    """

    chunk = Signal(object)
    finished_with = Signal(object)
    failed = Signal(object)

    def __init__(self, factory: Callable[..., Iterator[Any]], *args: Any,
                 **kwargs: Any) -> None:
        super().__init__()
        self._factory = factory
        self._args = args
        self._kwargs = kwargs
        self._aborted = False

    def abort(self) -> None:
        """请求停止。**只是"停止接收"，不是"取消任务"。**

        服务器上的任务会继续跑完（它的结果已经进库，在任务中心能看到）。
        把这写成"取消"会让用户以为服务器停了 —— 而实际上算力已经花掉了。
        """
        self._aborted = True

    def run(self) -> None:  # noqa: D102 - QThread 接口
        try:
            for item in self._factory(*self._args, **self._kwargs):
                if self._aborted:
                    break
                self.chunk.emit(item)
            self.finished_with.emit(None)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(exc)
