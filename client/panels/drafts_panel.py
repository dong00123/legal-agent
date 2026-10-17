"""文书草稿面板：打开 → 续编 → 导出 → 下载。

两个容易写错的点：

- **列表接口不带正文**（只有 ``content_chars``）。拿列表行直接填进编辑框再保存，
  等于把正文清空 —— 而保存接口只校验标题非空，会一路成功。
  所以打开草稿必须再调一次 ``GET /drafts/{id}``。
- **下载只能走 ``GET /drafts/{id}/download``**。接口不返回服务端绝对路径
  （刻意的），客户端也读不到服务器的文件系统。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QMessageBox, QPushButton, QSplitter, QTextEdit, QVBoxLayout, QWidget,
)

from client.app.workers import Worker
from client.panels.base import err_msg


class DraftsPanel(QWidget):
    """草稿列表 + 编辑器。"""

    def __init__(self, client: Any, state: Any) -> None:
        super().__init__()
        self.client = client
        self.state = state
        self._items: list[dict[str, Any]] = []
        self._current = ""
        self._worker: Worker | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        top = QHBoxLayout()
        self.btn_refresh = QPushButton("刷新")
        self.btn_save = QPushButton("保存为新版本")
        self.btn_export = QPushButton("导出 Word")
        self.btn_download = QPushButton("下载导出文件")
        self.status = QLabel("就绪")
        for w in (self.btn_refresh, self.btn_save, self.btn_export,
                  self.btn_download, self.status):
            top.addWidget(w)
        top.setStretch(4, 1)
        root.addLayout(top)

        self.list_widget = QListWidget()
        right = QWidget()
        rl = QVBoxLayout(right)
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("文书标题")
        self.body_edit = QTextEdit()
        rl.addWidget(self.title_edit)
        rl.addWidget(self.body_edit, 1)

        split = QSplitter()
        split.addWidget(self.list_widget)
        split.addWidget(right)
        split.setStretchFactor(1, 3)
        root.addWidget(split, 1)

        self.btn_refresh.clicked.connect(self.refresh)
        self.list_widget.currentRowChanged.connect(self._open)
        self.btn_save.clicked.connect(self._save)
        self.btn_export.clicked.connect(self._export)
        self.btn_download.clicked.connect(self._download)

    # ------------------------------------------------------------------ 列表
    def refresh(self) -> None:
        self.status.setText("加载中…")
        self._run(self.client.list_drafts, self._fill)

    def _fill(self, data: dict[str, Any]) -> None:
        self._items = list(data.get("items") or [])
        self.list_widget.clear()
        for d in self._items:
            self.list_widget.addItem(
                f"{d.get('title') or '（无标题）'}　v{d.get('version', 1)}　"
                f"{d.get('content_chars', 0)} 字")
        self.status.setText(f"共 {len(self._items)} 份")

    def _open(self, row: int) -> None:
        if row < 0 or row >= len(self._items):
            return
        did = str(self._items[row].get("draft_id") or "")
        if not did:
            return
        self._current = did
        self.status.setText("读取正文…")
        self._run(lambda: self.client.get_draft(did), self._show)

    def _show(self, d: dict[str, Any]) -> None:
        self.title_edit.setText(str(d.get("title") or ""))
        self.body_edit.setPlainText(str(d.get("content") or ""))
        self.status.setText(f"v{d.get('version', 1)}　可直接编辑")

    # ------------------------------------------------------------------ 操作
    def _save(self) -> None:
        if not self._current:
            self.status.setText("先选一份草稿")
            return
        self.status.setText("保存中…")
        self._run(
            lambda: self.client.save_draft(
                self._current, self.title_edit.text(), self.body_edit.toPlainText()),
            self._saved)

    def _saved(self, d: dict[str, Any]) -> None:
        self.status.setText(f"已保存为 v{d.get('version', '?')}")
        self.refresh()

    def _export(self) -> None:
        if not self._current:
            return
        self.status.setText("导出中…")
        self._run(lambda: self.client.export_draft(self._current, "docx"), self._exported)

    def _exported(self, res: dict[str, Any]) -> None:
        if res.get("ok"):
            self.status.setText(f"已导出：{res.get('filename')}（{res.get('size_bytes')} 字节）")
        else:
            # 501 通常是没有 python-docx。把服务端那句话原样给出来，
            # 而不是自己编一句"导出失败" —— 它已经写好了替代方案。
            QMessageBox.information(self, "导出未成功", str(res)[:400])

    def _download(self) -> None:
        if not self._current:
            return
        dest, _ = QFileDialog.getSaveFileName(
            self, "保存到", f"{self.title_edit.text() or 'draft'}.docx", "Word (*.docx)")
        if not dest:
            return
        self.status.setText("下载中…")
        self._run(lambda: str(self.client.download_draft(self._current, dest)),
                  lambda p: self.status.setText(f"已保存到 {p}"))

    # ------------------------------------------------------------------ 线程
    def _run(self, fn: Any, on_ok: Any) -> None:
        self._worker = Worker(fn)
        self._worker.finished_with.connect(on_ok)
        self._worker.failed.connect(self._fail)
        self._worker.start()

    def _fail(self, exc: Exception) -> None:
        self.status.setText(f"失败：{err_msg(exc)}")
