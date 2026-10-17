"""智能问答面板。

与审查面板共用同一套流式机制，区别只在**呈现什么**：
这里关心的是"报告出来了没有"，所以只把 trace 压成一行状态、
把 done 里的 report 完整显示。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from client.app.workers import StreamWorker
from client.panels.base import err_msg


class ChatPanel(QWidget):
    """问答：上方对话与报告，下方输入。"""

    # 一次问答收尾后发出（成功或失败都发）。主窗口用它刷新侧栏的"最近任务"——
    # 不刷新的话，刚跑完的任务要等下次启动才出现在侧栏里。
    taskSettled = Signal()  # noqa: N815 - 与 Qt 的 camelCase 信号命名一致

    def __init__(self, client: Any, state: Any) -> None:
        super().__init__()
        self.client = client
        self.state = state
        self._worker: StreamWorker | None = None
        self._task_id = ""
        # 工作台首页带过来的上下文（意图 / 案件 / 材料 id）。留空时行为与原来
        # 完全一样 —— 服务端自己判定意图，不带材料。
        self._extra: dict[str, Any] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        root.addWidget(self.log, 1)

        bottom = QHBoxLayout()
        self.input = QTextEdit()
        self.input.setPlaceholderText("输入法律问题，Ctrl+Enter 发送")
        self.input.setFixedHeight(90)
        self.btn_send = QPushButton("发送")
        self.btn_stop = QPushButton("停止接收")
        self.btn_stop.setEnabled(False)
        bottom.addWidget(self.input, 1)
        bottom.addWidget(self.btn_send)
        bottom.addWidget(self.btn_stop)
        root.addLayout(bottom)

        self.status = QLabel("就绪")
        root.addWidget(self.status)

        self.btn_send.clicked.connect(self.send)
        self.btn_stop.clicked.connect(self.stop_receiving)

    def keyPressEvent(self, event: Any) -> None:
        ctrl = bool(event.modifiers() & 0x04000000)  # Qt.ControlModifier
        if ctrl and event.key() in (0x01000004, 0x01000005):  # Return / Enter
            self.send()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------ 发送
    def send_query(self, query: str, extra: dict[str, Any] | None = None) -> None:
        """由工作台首页触发的发送：内容放进输入框，再走同一条发送路径。

        **刻意复用它自己的 ``send()``**，而不是另写一条调用链 ——
        两条链的按钮禁用、状态文案、错误处理迟早会分叉，
        然后"从首页发的问题卡住了，从问答页发的没事"。
        """
        self._extra = {k: v for k, v in dict(extra or {}).items() if v}
        self.input.setPlainText(query)
        self.send()

    def send(self) -> None:
        query = self.input.toPlainText().strip()
        if not query:
            return
        if self._worker is not None and self._worker.isRunning():
            self.status.setText("上一条还在跑")
            return
        self.log.append(f"\n【问】{query}")
        self.input.clear()
        self.btn_send.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.status.setText("分析中…")

        payload: dict[str, Any] = {"query": query}
        payload.update(self._extra)
        self._worker = StreamWorker(self.client.stream_chat, payload)
        self._worker.chunk.connect(self._on_chunk)
        self._worker.finished_with.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def stop_receiving(self) -> None:
        if self._worker is not None:
            self._worker.abort()
        self.status.setText(f"已停止接收（服务端仍在跑，任务 {self._task_id}）")

    def _on_chunk(self, item: Any) -> None:
        event = item[0] if isinstance(item, (tuple, list)) else ""
        data = item[1] if isinstance(item, (tuple, list)) and len(item) > 1 else {}
        if event == "meta":
            self._task_id = str(data.get("task_id") or "")
            self.status.setText(f"分析中…（任务 {self._task_id}）")
        elif event == "trace":
            detail = str(data.get("detail") or "")
            if detail:
                self.status.setText(detail[:120])
        elif event == "done":
            report = str((data or {}).get("report") or "（无报告正文）")
            self.log.append(f"\n【答】\n{report}")
            gaps = (data or {}).get("evidence_gap") or []
            if gaps:
                self.log.append(f"\n（证据缺口 {len(gaps)} 项，详见任务 {self._task_id}）")
        elif event == "error":
            self.log.append(f"\n【错】{data.get('error', '')}")

    def _on_done(self, _: Any) -> None:
        self.btn_send.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.status.setText(f"完成（任务 {self._task_id}）")
        self.taskSettled.emit()

    def _on_failed(self, exc: Exception) -> None:
        self.btn_send.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.log.append(f"\n【错】{err_msg(exc)}")
        self.status.setText("请求失败")
        # 失败也要刷新：服务端可能已经把任务建了但判定失败，
        # 那种情况恰恰是用户去任务中心找原因的时候
        self.taskSettled.emit()
