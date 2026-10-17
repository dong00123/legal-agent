"""任务中心面板。

存在的理由很实际：**流式窗口关掉之后，结果去哪儿找**。
桌面端不像浏览器那样能靠"后退"回到上一次对话，
所以任务列表就是唯一的找回入口。

列表只显示"够用来认出是哪一次"的几列（时间 / 问题 / 状态），
完整的报告、引用、缺口在点开之后才加载 —— 一次性把几十条报告全拉下来
会让列表卡住，而用户绝大多数时候只是想找其中一条。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QLabel, QPushButton, QSplitter,
    QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from client.app.workers import Worker
from client.panels.base import err_msg

STATUS_LABEL = {
    "pending": "待执行", "running": "执行中", "succeeded": "完成",
    "degraded": "降级完成", "failed": "失败",
}


class TasksPanel(QWidget):
    """任务列表 + 详情。"""

    COLUMNS = ["时间", "问题", "状态", "任务号"]

    def __init__(self, client: Any, state: Any) -> None:
        super().__init__()
        self.client = client
        self.state = state
        self._rows: list[dict[str, Any]] = []
        self._worker: Worker | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        top = QHBoxLayout()
        self.btn_refresh = QPushButton("刷新")
        self.status = QLabel("就绪")
        top.addWidget(self.btn_refresh)
        top.addWidget(self.status, 1)
        root.addLayout(top)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        # 时间列要放下 "2026-09-18T11:30:45" 这 19 个等宽字符，
        # 130px 会把秒截掉 —— 而秒往往正是区分两次同名任务的那一位
        self.table.setColumnWidth(0, 155)
        self.table.setColumnWidth(1, 380)
        self.table.setColumnWidth(2, 90)

        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        # 不选中任何一行时下方是一大片纯白，看着像加载失败；
        # 给一句话说明这里该出现什么
        self.detail.setPlaceholderText(
            "点击上方任意一行，这里显示该次任务的报告正文、引用与证据缺口。")

        split = QSplitter(Qt.Vertical)
        split.addWidget(self.table)
        split.addWidget(self.detail)
        split.setStretchFactor(0, 2)
        root.addWidget(split, 1)

        self.btn_refresh.clicked.connect(self.refresh)
        self.table.currentCellChanged.connect(self._on_pick)

    # ------------------------------------------------------------------ 列表
    def refresh(self) -> None:
        self.status.setText("加载中…")
        self._worker = Worker(self.client.list_tasks, limit=50)
        self._worker.finished_with.connect(self._fill)
        self._worker.failed.connect(self._fail)
        self._worker.start()

    def _fill(self, data: dict[str, Any]) -> None:
        self._rows = list(data.get("items") or [])
        self.table.setRowCount(len(self._rows))
        for i, r in enumerate(self._rows):
            self.table.setItem(i, 0, QTableWidgetItem(str(r.get("created_at") or "")[:19]))
            self.table.setItem(i, 1, QTableWidgetItem(str(r.get("query") or "")[:80]))
            st = str(r.get("status") or "")
            self.table.setItem(i, 2, QTableWidgetItem(STATUS_LABEL.get(st, st)))
            self.table.setItem(i, 3, QTableWidgetItem(str(r.get("task_id") or "")))
        self.table.resizeColumnToContents(1)
        self.status.setText(f"共 {len(self._rows)} 条")

    def _fail(self, exc: Exception) -> None:
        self.status.setText(f"加载失败：{err_msg(exc)}")

    # ------------------------------------------------------------------ 详情
    def _on_pick(self, row: int, *_: Any) -> None:
        if row < 0 or row >= len(self._rows):
            return
        tid = str(self._rows[row].get("task_id") or "")
        if not tid:
            return
        self.status.setText(f"加载 {tid} …")
        self._worker = Worker(self.client.get_task, tid)
        self._worker.finished_with.connect(self._show_detail)
        self._worker.failed.connect(self._fail)
        self._worker.start()

    def _show_detail(self, task: dict[str, Any]) -> None:
        parts = [f"任务 {task.get('task_id')}　状态 {STATUS_LABEL.get(task.get('status'), task.get('status'))}",
                 ""]
        report = str(task.get("report") or "")
        if report:
            parts.append(report)
        cites = task.get("citations") or []
        if cites:
            parts.append(f"\n\n【引用】共 {len(cites)} 条")
            for c in cites[:20]:
                parts.append(f"  · {c}")
        gaps = task.get("evidence_gap") or []
        if gaps:
            parts.append(f"\n\n【证据缺口】{len(gaps)} 项")
            for g in gaps[:20]:
                parts.append(f"  · {g}")
        err = task.get("error")
        if err:
            parts.append(f"\n\n【失败原因】{err}")
        self.detail.setPlainText("\n".join(str(p) for p in parts))
        self.status.setText("就绪")
