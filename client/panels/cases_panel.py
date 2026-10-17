"""案件管理面板。

与 Web 端（``web/index.html`` 的 ``#pg-case``）同一套约定，两条最重要的照搬：

**列表是"本人可见范围"，不是"全所"。** 过滤在服务端 SQL 上做，前端拿到的
就是已经收口过的结果；客户端绝不自己再 filter —— 那样一旦服务端漏了条件，
界面上看起来还是"只有我的案子"，问题就被完美地藏起来了。

**案号（``case_id``）由用户填。** 律所本来就有自己的案号规则（年份 + 类型 +
序号），系统生成的编号在归档时没用，所以新建对话框把它做成必填首项。

桌面端刻意**不做删除**：删除案件是低频且危险的动作，值得为它开一次浏览器，
不值得为它在这里做一套二次确认 —— 误删的代价不对称。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFormLayout, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from client.app import theme
from client.app.workers import Worker
from client.panels.base import err_msg

# 与 Web 端 cases.js 的三张常量表逐项一致 —— 两端的"诉讼/非诉"必须说同一种话
CASE_TYPES = [("litigation", "诉讼"), ("contract", "非诉/合同"), ("consult", "咨询")]
STAGES = [("intake", "接案"), ("evidence", "举证"), ("hearing", "开庭"),
          ("judgment", "判决"), ("closed", "结案")]
CASE_STATUS = [("active", "在办"), ("suspended", "中止"),
               ("closed", "已结"), ("archived", "归档")]

COLUMNS = ["案号", "案件名称", "类型", "阶段", "状态", "委托人", "最近更新"]


def _label(pairs: list[tuple[str, str]], value: Any) -> str:
    value = str(value or "")
    for k, v in pairs:
        if k == value:
            return v
    return value or "—"


class CasesPanel(QWidget):
    """案件列表 + 筛选 + 新建；双击行看详情。"""

    def __init__(self, client: Any, state: Any) -> None:
        super().__init__()
        self.client = client
        self.state = state
        self._rows: list[dict[str, Any]] = []
        self._worker: Worker | None = None
        self._build_ui()

    # ------------------------------------------------------------------ 界面
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.keyword = QLineEdit()
        self.keyword.setPlaceholderText("按案号 / 名称 / 案由 / 委托人搜索")
        self.keyword.returnPressed.connect(self.refresh)
        bar.addWidget(self.keyword, 1)

        self.type_combo = self._combo(CASE_TYPES, "全部类型")
        self.status_combo = self._combo(CASE_STATUS, "全部状态")
        self.stage_combo = self._combo(STAGES, "全部阶段")
        for combo in (self.type_combo, self.status_combo, self.stage_combo):
            bar.addWidget(combo)

        btn_new = QPushButton("＋ 新建案件")
        btn_new.clicked.connect(self._create_dialog)
        bar.addWidget(btn_new)

        btn_refresh = QPushButton("刷新")
        btn_refresh.clicked.connect(self.refresh)
        bar.addWidget(btn_refresh)
        root.addLayout(bar)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.doubleClicked.connect(self._on_open)
        root.addWidget(self.table, 1)

        self.status = QLabel("就绪")
        self.status.setProperty("role", "hint")
        root.addWidget(self.status)

    def _combo(self, pairs: list[tuple[str, str]], all_label: str) -> QComboBox:
        combo = QComboBox()
        combo.addItem(all_label, "")
        for k, v in pairs:
            combo.addItem(v, k)
        combo.currentIndexChanged.connect(self.refresh)
        return combo

    # ------------------------------------------------------------------ 列表
    def refresh(self) -> None:
        self.status.setText("加载中…")
        self._worker = Worker(
            self.client.list_cases,
            keyword=self.keyword.text().strip(),
            case_type=str(self.type_combo.currentData() or ""),
            status=str(self.status_combo.currentData() or ""),
            stage=str(self.stage_combo.currentData() or ""),
            limit=20,
        )
        self._worker.finished_with.connect(self._fill)
        self._worker.failed.connect(self._fail)
        self._worker.start()

    def _fill(self, data: dict[str, Any]) -> None:
        self._rows = list((data or {}).get("items") or [])
        self.table.setRowCount(len(self._rows))
        for i, r in enumerate(self._rows):
            cells = [
                str(r.get("case_id") or ""),
                str(r.get("title") or "（未命名）"),
                _label(CASE_TYPES, r.get("case_type")),
                _label(STAGES, r.get("stage")),
                _label(CASE_STATUS, r.get("status")),
                str(r.get("client_name") or "—"),
                str(r.get("updated_at") or "")[:19],
            ]
            for j, text in enumerate(cells):
                self.table.setItem(i, j, QTableWidgetItem(text))
        self.table.resizeColumnToContents(1)
        self.status.setText(f"共 {len(self._rows)} 条（列表只含你有权限看到的案件）")

    def _fail(self, exc: Exception) -> None:
        self.status.setText(f"加载失败：{err_msg(exc)}")

    # ------------------------------------------------------------------ 详情
    def _on_open(self, index) -> None:  # noqa: ANN001 - QModelIndex
        row = index.row()
        if row < 0 or row >= len(self._rows):
            return
        case_id = str(self._rows[row].get("case_id") or "")
        if not case_id:
            return
        self.status.setText(f"加载 {case_id} …")
        self._worker = Worker(self._load_detail, case_id)
        self._worker.finished_with.connect(self._show_detail)
        self._worker.failed.connect(self._fail)
        self._worker.start()

    def _load_detail(self, case_id: str) -> dict[str, Any]:
        """详情与材料一起取：分两次取的话，中间态会让人以为材料丢了。"""
        detail = self.client.get_case(case_id)
        try:
            materials = self.client.request(
                "GET", f"/cases/{case_id}/materials")
        except Exception:                     # noqa: BLE001 - 材料列表失败不挡详情
            materials = {"items": []}
        return {"detail": detail, "materials": materials}

    def _show_detail(self, data: dict[str, Any]) -> None:
        self.status.setText("就绪")
        d = data.get("detail") or {}
        rows = list(((data.get("materials") or {}).get("items")) or [])

        def esc(v: Any) -> str:
            from html import escape
            return escape(str(v if v is not None else "—"))

        parts = [
            f"<h3 style='margin:0 0 6px 0;'>{esc(d.get('title') or d.get('case_id'))}</h3>",
            f"<p style='color:{theme.TEXT_DIM};margin:0 0 10px 0;'>"
            f"案号 {esc(d.get('case_id'))} · "
            f"{_label(CASE_TYPES, d.get('case_type'))} · "
            f"{_label(STAGES, d.get('stage'))} · "
            f"{_label(CASE_STATUS, d.get('status'))} · "
            f"委托人 {esc(d.get('client_name') or '未填')}</p>",
        ]
        if d.get("cause_of_action"):
            parts.append(f"<p style='margin:0 0 6px 0;'>案由：{esc(d['cause_of_action'])}</p>")
        if d.get("summary"):
            parts.append(f"<p style='margin:0 0 6px 0;white-space:pre-wrap;'>{esc(d['summary'])}</p>")
        parts.append(f"<hr><p style='color:{theme.TEXT_DIM};margin:6px 0;'>"
                     f"材料 {len(rows)} 份</p>")
        if rows:
            for m in rows:
                parts.append(
                    f"<p style='margin:2px 0;'>· {esc(m.get('filename') or m.get('id'))}"
                    f" <span style='color:{theme.TEXT_FAINT};font-size:11px;'>"
                    f"{esc((str(m.get('uploaded_at') or ''))[:19])}</span></p>")
        else:
            parts.append(f"<p style='color:{theme.TEXT_FAINT};margin:4px 0;'>"
                         "还没有材料。上传在 Web 端的案件详情页进行。</p>")
        parts.append(f"<p style='color:{theme.TEXT_FAINT};font-size:11px;"
                     "margin:10px 0 0 0;'>删除案件、上传与解析材料等写操作"
                     "在 Web 端进行，避免误触。</p>")

        box = QTextBrowser()
        box.setHtml("".join(parts))
        lay = QVBoxLayout()
        lay.addWidget(box)

        dlg = QDialog(self)
        dlg.setWindowTitle(f"案件详情 · {d.get('case_id') or ''}")
        dlg.resize(560, 420)
        dlg.setLayout(lay)
        dlg.exec()

    # ------------------------------------------------------------------ 新建
    def _create_dialog(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("新建案件")
        dlg.setMinimumWidth(420)
        form = QFormLayout(dlg)

        edit_id = QLineEdit()
        edit_id.setPlaceholderText("(2026)京0105民初1234号")
        edit_title = QLineEdit()
        edit_title.setPlaceholderText("如：张某诉某公司劳动争议")
        combo_type = QComboBox()
        for k, v in CASE_TYPES:
            combo_type.addItem(v, k)
        combo_stage = QComboBox()
        for k, v in STAGES:
            combo_stage.addItem(v, k)
        edit_cause = QLineEdit()
        edit_cause.setPlaceholderText("如：劳动合同纠纷")
        edit_client = QLineEdit()
        edit_client.setPlaceholderText("存入时会脱敏")
        edit_opponent = QLineEdit()
        edit_summary = QLineEdit()

        form.addRow("案号 *", edit_id)
        form.addRow("案件名称", edit_title)
        form.addRow("案件类型", combo_type)
        form.addRow("当前阶段", combo_stage)
        form.addRow("案由", edit_cause)
        form.addRow("委托人", edit_client)
        form.addRow("对方当事人", edit_opponent)
        form.addRow("案情摘要", edit_summary)

        note = QLabel("案号请按本所规则填写；系统不自动生成 —— 生成的编号在归档时"
                      "对不上本所的案卷号。归属默认「本人 + 本人团队」。")
        note.setWordWrap(True)
        form.addRow(note)

        btns = QHBoxLayout()
        btn_cancel = QPushButton("取消")
        btn_ok = QPushButton("创建")
        btn_ok.setDefault(True)
        btns.addStretch(1)
        btns.addWidget(btn_cancel)
        btns.addWidget(btn_ok)
        form.addRow(btns)

        def _cancel() -> None:
            dlg.reject()

        def _ok() -> None:
            case_id = edit_id.text().strip()
            if not case_id:
                QMessageBox.warning(dlg, "新建案件", "案号是必填的。")
                return
            btn_ok.setEnabled(False)
            btn_ok.setText("创建中…")
            self._worker = Worker(
                self.client.create_case,
                edit_title.text().strip(),
                case_id=case_id,
                case_type=str(combo_type.currentData() or "litigation"),
                stage=str(combo_stage.currentData() or "intake"),
                cause_of_action=edit_cause.text().strip(),
                client_name=edit_client.text().strip(),
                opponent_name=edit_opponent.text().strip(),
                summary=edit_summary.text().strip(),
            )
            self._worker.finished_with.connect(lambda _rec: dlg.accept())
            self._worker.failed.connect(lambda exc: _create_failed(exc, btn_ok))
            self._worker.start()

        def _create_failed(exc: Exception, btn: QPushButton) -> None:
            btn.setEnabled(True)
            btn.setText("创建")
            QMessageBox.warning(dlg, "新建案件", err_msg(exc))

        btn_cancel.clicked.connect(_cancel)
        btn_ok.clicked.connect(_ok)
        dlg.exec()
        self.refresh()
