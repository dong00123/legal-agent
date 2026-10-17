"""合同审查面板：拖拽导入 → 边审边看 → 逐条高亮。

这是桌面端相对 Web 端**唯一不可替代**的部分：律师手上有的是本地文件，
要的是"拖进来、看着原文一条条对"。Web 端做不到拖拽，也不该做到
（浏览器读不了本地任意路径，这是对的）。

三个实现要点：

- **高亮用 ``ExtraSelection`` 而不是改文本**。直接改 QTextEdit 的内容去上色
  会把原文弄脏（复制出来带格式、保存时串味）。ExtraSelection 是"覆盖层"，
  原文始终干净。
- **停止按钮叫"停止接收"，不叫"取消任务"**。服务端会继续跑完并落库。
  叫"取消"会让人以为算力省下来了 —— 而实际上任务还在跑，
  只是这边不再显示。这一点在 :class:`~client.app.workers.StreamWorker`
  里也写了。
- **断流后给任务号**。``meta`` 第一条就有 task_id，拿到它就能去任务中心
  找回结果，而不是让这次审查凭空消失。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QProgressBar, QPushButton, QSplitter,
    QTextEdit, QVBoxLayout, QWidget,
)

from client.app.workers import StreamWorker
from client.panels.base import LEVEL_COLOR, LEVEL_LABEL, err_msg

SUPPORTED_SUFFIX = {".pdf", ".docx", ".doc", ".txt", ".md"}


class ReviewPanel(QWidget):
    """合同审查。左：案件与材料；中：原文；右：风险点与建议。"""

    def __init__(self, client: Any, state: Any) -> None:
        super().__init__()
        self.client = client
        self.state = state
        self._materials: list[dict[str, Any]] = []
        self._risks: list[dict[str, Any]] = []
        self._worker: StreamWorker | None = None
        self._task_id = ""
        self._build_ui()
        self.setAcceptDrops(True)

    # ------------------------------------------------------------------ 界面
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # ---- 顶部：案件 + 按钮
        top = QHBoxLayout()
        top.addWidget(QLabel("案件："))
        self.case_combo = QComboBox()
        self.case_combo.setEditable(True)
        self.case_combo.setMinimumWidth(220)
        top.addWidget(self.case_combo, 1)
        self.btn_new_case = QPushButton("新建案件")
        self.btn_import = QPushButton("导入材料…")
        self.btn_review = QPushButton("开始审查")
        self.btn_stop = QPushButton("停止接收")
        self.btn_stop.setEnabled(False)
        top.addWidget(self.btn_new_case)
        top.addWidget(self.btn_import)
        top.addWidget(self.btn_review)
        top.addWidget(self.btn_stop)
        root.addLayout(top)

        # ---- 主体三栏
        self.material_list = QListWidget()
        self.material_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.text_edit = QTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.setPlaceholderText(
            "把合同 PDF / Word 拖到这里，或点「导入材料」。\n"
            "原文只在内存里，不会写进本机磁盘（案件正文的留存一律走服务端）。")
        self.risk_list = QListWidget()

        splitter = QSplitter(Qt.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.addWidget(QLabel("材料"))
        ll.addWidget(self.material_list)
        mid = QWidget()
        ml = QVBoxLayout(mid)
        ml.addWidget(QLabel("原文（高亮处为风险条款）"))
        ml.addWidget(self.text_edit)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.addWidget(QLabel("风险点与建议（点击定位原文）"))
        rl.addWidget(self.risk_list)
        self.risk_detail = QTextEdit()
        self.risk_detail.setReadOnly(True)
        rl.addWidget(self.risk_detail)
        for w in (left, mid, right):
            splitter.addWidget(w)
        splitter.setStretchFactor(1, 3)
        root.addWidget(splitter, 1)

        # ---- 底部：进度
        bottom = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.status = QLabel("就绪")
        bottom.addWidget(self.progress, 1)
        bottom.addWidget(self.status)
        root.addLayout(bottom)

        self.btn_new_case.clicked.connect(self._new_case)
        self.btn_import.clicked.connect(self._pick_files)
        self.btn_review.clicked.connect(self.start_review)
        self.btn_stop.clicked.connect(self.stop_receiving)
        self.risk_list.currentRowChanged.connect(self._show_risk)
        self.material_list.itemSelectionChanged.connect(self._load_selected_material)

    # ------------------------------------------------------------------ 拖拽
    def dragEnterEvent(self, event: Any) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: Any) -> None:
        paths = [u.toLocalFile() for u in event.mimeData().urls()]
        self._import_files([p for p in paths if p])
        event.acceptProposedAction()

    def _pick_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择合同材料", "",
            "文档 (*.pdf *.docx *.doc *.txt *.md);;所有文件 (*)")
        if files:
            self._import_files(files)

    def _import_files(self, paths: list[str]) -> None:
        """上传材料。**只存 id 与元数据，不把原文写进本机磁盘。**"""
        ok, skipped = 0, []
        for p in paths:
            if Path(p).suffix.lower() not in SUPPORTED_SUFFIX:
                skipped.append(Path(p).name)
                continue
            case_id = self._ensure_case()
            try:
                rec = self.client.upload_material(case_id, p)
            except Exception as exc:  # noqa: BLE001
                self.status.setText(f"上传失败：{err_msg(exc)}")
                continue
            ok += 1
            self._materials.append(rec)
            self.material_list.addItem(
                f"{rec.get('filename')}（{rec.get('size_bytes', 0)} 字节，id={rec.get('id')}）")
        msg = f"已上传 {ok} 份"
        if skipped:
            # **必须说清楚哪些没被导入。** 静默跳过的表现是
            # "审查出来什么都没有"，而用户以为材料已经进去了。
            msg += f"；跳过 {len(skipped)} 份（格式不支持）：{'、'.join(skipped[:3])}"
        self.status.setText(msg)

    def _ensure_case(self) -> str:
        """拿一个案件号：下拉里有的就用，没有就按输入内容新建一个。"""
        case_id = self.case_combo.currentText().strip()
        data = self.case_combo.currentData()
        if data:
            return str(data)
        if not case_id:
            return self._new_case()
        try:
            self.client.create_case(case_id, case_id=case_id)
        except Exception:  # noqa: BLE001
            pass  # 已存在时会报错，无所谓 —— 我们要的只是它存在
        self.case_combo.setCurrentText(case_id)
        return case_id

    def _new_case(self) -> str:
        from client.app.api_client import new_case_id

        cid = new_case_id()
        title = f"桌面端案件 {cid}"
        try:
            rec = self.client.create_case(title, case_id=cid)
            cid = str(rec.get("case_id") or cid)
        except Exception as exc:  # noqa: BLE001
            self.status.setText(f"建案件失败：{err_msg(exc)}")
        self.case_combo.addItem(cid, cid)
        self.case_combo.setCurrentText(cid)
        return cid

    # ------------------------------------------------------------------ 原文
    def _load_selected_material(self) -> None:
        """把选中材料的正文加载到内存并展示。

        这里用 ``material_text`` 直接取正文。注意：**不写盘**，
        只放在 ``state`` 的内存字典里（见 ``client/app/state.py``）。
        """
        rows = self.material_list.selectedIndexes()
        if not rows:
            return
        idx = rows[0].row()
        if idx >= len(self._materials):
            return
        rec = self._materials[idx]
        try:
            detail = self.client.request("GET", f"/cases/{rec.get('case_id')}/materials")
        except Exception as exc:  # noqa: BLE001
            self.status.setText(f"读取材料失败：{err_msg(exc)}")
            return
        items = detail.get("items") or []
        text = ""
        for it in items:
            if it.get("id") == rec.get("id"):
                text = str(it.get("text") or it.get("content") or "")
        if not text:
            self.text_edit.setPlainText(
                "（服务端还没有这份材料的解析正文。审查时它会按 material_ids 直接读取，"
                "不影响审查结果 —— 这里的预览只是辅助。）")
            return
        self.text_edit.setPlainText(text)
        self.state.set_material(str(rec.get("case_id")), text)

    # ------------------------------------------------------------------ 审查
    def start_review(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self.status.setText("正在审查中")
            return
        case_id = self._ensure_case()
        ids = [int(m["id"]) for m in self._materials if m.get("id") is not None]
        if not ids:
            self.status.setText("先导入至少一份材料")
            return
        payload = self.client.material_ids_payload(case_id, ids,
                                                   query="请审查这份合同的条款风险")
        self._risks = []
        self.risk_list.clear()
        self.progress.setValue(0)
        self.btn_review.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.status.setText("审查中…")

        self._worker = StreamWorker(self.client.stream_review, payload)
        self._worker.chunk.connect(self._on_chunk)
        self._worker.finished_with.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def stop_receiving(self) -> None:
        """停止接收。**不是取消任务** —— 服务端会跑完，结果去任务中心看。"""
        if self._worker is not None:
            self._worker.abort()
        self.status.setText(f"已停止接收。任务仍在服务端执行，可到任务中心查看 {self._task_id}")

    @staticmethod
    def _event_name(item: Any) -> str:
        return str(item[0]) if isinstance(item, (tuple, list)) and item else ""

    def _on_chunk(self, item: Any) -> None:
        event = self._event_name(item)
        data = item[1] if isinstance(item, (tuple, list)) and len(item) > 1 else {}
        if event == "meta":
            self._task_id = str(data.get("task_id") or "")
            self.status.setText(f"审查中…（任务 {self._task_id}）")
        elif event == "trace":
            detail = str(data.get("detail") or data.get("node") or "")
            if detail:
                self.status.setText(detail[:120])
            pct = data.get("progress") or {}
            if isinstance(pct, dict) and pct.get("percent") is not None:
                self.progress.setValue(int(pct["percent"]))
        elif event == "done":
            self._render_result(data or {})
        elif event == "error":
            self.status.setText(f"审查出错：{data.get('error', '')}")

    def _render_result(self, data: dict[str, Any]) -> None:
        self._risks = list(data.get("risk_points") or [])
        self.risk_list.clear()
        for r in self._risks:
            level = str(r.get("level") or "low")
            item = QListWidgetItem(
                f"[{LEVEL_LABEL.get(level, level)}] {str(r.get('clause_no') or '—')} · "
                f"{str(r.get('risk') or '')[:60]}")
            item.setForeground(QColor(LEVEL_COLOR.get(level, "#5d6d7e")))
            self.risk_list.addItem(item)
        missing = data.get("missing_clauses") or []
        self.progress.setValue(100)
        self.status.setText(
            f"完成：风险 {len(self._risks)} 条，缺失条款 {len(missing)} 项，"
            f"引用 {len(data.get('citations') or [])} 条；任务 {self._task_id}")
        self._highlight_all()

    def _on_done(self, _: Any) -> None:
        self.btn_review.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def _on_failed(self, exc: Exception) -> None:
        self.btn_review.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.status.setText(f"请求失败：{err_msg(exc)}")

    # ------------------------------------------------------------------ 高亮
    def _highlight_all(self) -> None:
        """把所有风险条款在原文里标出来。**用 ExtraSelection，不改原文。**"""
        doc = self.text_edit.document()
        selections = []
        fmt = QTextCharFormat()
        fmt.setBackground(QColor("#ffe08a"))
        for r in self._risks:
            needle = str(r.get("clause_text") or "").strip()
            if len(needle) < 4:
                continue      # 太短的串会在全文里命中几十处，标了等于没标
            cursor = QTextCursor(doc)
            while True:
                found = doc.find(needle, cursor)
                if found.isNull():
                    break
                sel = QTextEdit.ExtraSelection()
                sel.cursor = found
                sel.format = fmt
                selections.append(sel)
                cursor = found
        self.text_edit.setExtraSelections(selections)

    def _show_risk(self, row: int) -> None:
        if row < 0 or row >= len(self._risks):
            return
        r = self._risks[row]
        basis = "；".join(str(b) for b in (r.get("basis") or []))
        self.risk_detail.setPlainText(
            f"条款：{r.get('clause_no') or '—'}　风险等级：{LEVEL_LABEL.get(r.get('level'), r.get('level'))}\n"
            f"问题：{r.get('risk') or ''}\n\n"
            f"依据：{basis or '（无）'}\n\n"
            f"建议：{r.get('suggestion') or '（无）'}")
        # 定位到原文
        needle = str(r.get("clause_text") or "").strip()
        if len(needle) >= 4 and not self.text_edit.find(needle):
            self.status.setText("原文里没找到这段条款文本（可能是摘要性描述）")

