"""工作台首页：品牌 + 大标题 + 任务卡片 + 输入区。

四件事值得说明：

**卡片数据来自服务端，不是写死的几张。** ``GET /chat/intents`` 返回的就是
Router 的路由表本身（意图、中文标签、节点路线）。界面照着它渲染，将来后端
新增一个意图，首页自动多一张卡；写死的话界面显示的与真正能跑的一定会分叉 ——
而这种分叉在界面上完全看不出来，只有用户选了那个意图才发现跑的不是一回事。

**点卡片只填模板、不直接发送。** 与参考图底部那行小字一致："模板填入后仍可
自由修改"。律师手上的材料和模板首句往往对不上，直接发出去等于逼他在一个
错误的提问上改，不如先给一段能改的草稿。

**提问长度上限取自服务端真实限制（10000 字）。** 计数器存在的意义就是让人在
超限**之前**看到自己快超了；写一个比服务端宽松的数字等于没有这个功能 ——
用户看着 6000/10000 觉得没事，发出去被拒，然后不知道该删多少。

**上传材料时顺手建一个案件。** 材料在服务端是挂在案件下的。不这么做的话，
用户得先去合同审查页建个案件才能回来传材料 —— 那是把内部数据模型摊给用户看。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QScrollArea, QTextEdit, QVBoxLayout, QWidget,
)

from client.app import theme
from client.app.icons import Icon
from client.app.widgets import IconTextButton, TaskCard
from client.app.workers import Worker
from client.panels.base import err_msg

# 与服务端 ``backend/app/api/v1/chat.py`` 的 MAX_QUERY_CHARS 一致。
# 不动态去取，是因为首页要在后端不可达时也能正常显示与计数。
MAX_QUERY_CHARS = 10000

# 意图 → 卡片图标（图标名来自 client/app/icons.py 的 DRAWERS）
INTENT_ICON = {
    "contract_review": "doc",
    "case_search": "book",
    "statute_lookup": "search",
    "case_analysis": "chart",
    "doc_draft": "pen",
    "consult": "chat",
}

# 意图 → 卡片说明。服务端 INTENT_ROUTES 里那句 "why" 是写给开发看的
# （"按方案第六章合同审查 Skill 的 9 步路径执行"），不适合直接给律师，
# 所以说明文案在客户端维护；标签（label）仍取服务端，那是同一套术语。
INTENT_DESC = {
    "contract_review": "识别风险条款并给出修改建议",
    "case_search": "梳理有效依据与参考案例",
    "case_analysis": "提取事实、证据和争议焦点",
    "doc_draft": "按场景生成可编辑文书初稿",
    "statute_lookup": "查证法条与适用条件",
    "consult": "多轮追问，给出可落地意见",
}

# 卡片顺序：先常用、后长尾。不在此列的意图排在最后。
INTENT_ORDER = ["contract_review", "case_search", "case_analysis",
                "doc_draft", "statute_lookup", "consult"]

# 点卡片后填进输入框的模板首句
INTENT_TEMPLATE = {
    "contract_review": "请审查这份合同，逐条指出风险条款、法律依据与修改建议。",
    "case_search": "请检索与下列争议焦点相关的类案，说明裁判要旨与可参考之处：",
    "statute_lookup": "请查证下列问题涉及的法条，并说明现行有效性与适用条件：",
    "case_analysis": "请分析这份案件材料，梳理事实、证据与争议焦点：",
    "doc_draft": "请按下列要求起草法律文书：",
    "consult": "请就下列问题给出法律意见：",
}

# 后端不可达时的兜底。只影响"卡片显示什么"，不影响能不能提问 ——
# 意图本来就可以留空，由服务端自己判定。
FALLBACK_INTENTS = [
    ("contract_review", "合同审查"),
    ("case_search", "类案检索"),
    ("case_analysis", "案件分析"),
    ("doc_draft", "文书起草"),
    ("statute_lookup", "法规查询"),
    ("consult", "法律咨询"),
]

SUPPORTED_SUFFIX = {".pdf", ".docx", ".doc", ".txt", ".md"}
CARD_COLUMNS = 2


class HomePanel(QWidget):
    """工作台首页。发送时发 ``submitted(query, extra)``，由主窗口交给问答面板。"""

    submitted = Signal(str, dict)

    def __init__(self, client: Any, state: Any) -> None:
        super().__init__()
        self.client = client
        self.state = state
        self._intent = ""
        self._case_id = ""
        self._materials: list[dict[str, Any]] = []
        self._worker: Worker | None = None
        self._cards: list[TaskCard] = []
        self._build_ui()
        self.load_intents()

    # ------------------------------------------------------------------ 界面
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # 窗口被拉矮时靠滚动条兜底，而不是把标题和输入框压成一团
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 默认 viewport 会填自己的背景色（白），套在白卡片上就是一块突兀的白
        scroll.viewport().setAutoFillBackground(False)
        outer.addWidget(scroll)

        body = QWidget()
        body.setAutoFillBackground(False)
        scroll.setWidget(body)

        wrap = QHBoxLayout(body)
        wrap.setContentsMargins(30, 26, 30, 16)

        # 限宽：主区被拉到 1400 宽时，卡片不该跟着变成两条细长横条
        inner = QWidget()
        inner.setMaximumWidth(820)
        wrap.addStretch(1)
        wrap.addWidget(inner, 10)
        wrap.addStretch(1)

        col = QVBoxLayout(inner)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        col.addStretch(1)

        brand = QHBoxLayout()
        brand.setSpacing(9)
        brand.addWidget(Icon("scale", 22, theme.ACCENT))
        brand.addWidget(QLabel("LEGAL AGENT", objectName="homeWord"))
        brand.addStretch(1)
        col.addLayout(brand)
        col.addSpacing(16)

        col.addWidget(QLabel("今天要处理什么法律工作？", objectName="homeTitle"))
        col.addSpacing(8)
        sub = QLabel("选择一个常见任务，我们会把可编辑的工作模板放入输入框。",
                     objectName="homeSub")
        sub.setWordWrap(True)
        col.addWidget(sub)
        col.addSpacing(20)

        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(14)
        self.grid.setContentsMargins(0, 0, 0, 0)
        col.addLayout(self.grid)

        col.addSpacing(12)
        card_hint = QLabel("模板填入后仍可自由修改，确认内容后再发送。",
                           objectName="homeHint")
        card_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(card_hint)

        col.addStretch(2)
        col.addWidget(self._build_composer())
        col.addSpacing(10)
        # 免责声明行已按需求隐藏（原文案："本地化运行，内容由 AI 生成，请仔细甄别"）

    def _build_composer(self) -> QWidget:
        box = QFrame(objectName="composer")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.input = QTextEdit(objectName="homeInput")
        self.input.setPlaceholderText(
            "描述你的任务，或点「添加材料」选入合同、案卷、判决书……")
        self.input.setAcceptRichText(False)
        # 固定高度：交给布局分配的话，QTextEdit 会按自己的 sizeHint 一路吃到
        # 上限（实测占满 160px，把工具行挤到贴底），而参考图里的输入框就是
        # 一个固定高的框，写长了在框内滚动
        self.input.setFixedHeight(84)
        self.input.installEventFilter(self)
        self.input.textChanged.connect(self._on_text)
        lay.addWidget(self.input)

        # 材料标签行（没有材料时整行不显示）
        self.chip_row = QWidget()
        chip_lay = QHBoxLayout(self.chip_row)
        chip_lay.setContentsMargins(14, 0, 14, 2)
        chip_lay.setSpacing(6)
        chip_lay.addStretch(1)
        self.chip_layout = chip_lay
        self.chip_row.hide()
        lay.addWidget(self.chip_row)

        # 状态提示行（上传进度 / 超限提醒 / 上传失败原因），有话说时才出现
        self.hint = QLabel("", objectName="homeHint")
        self.hint.setWordWrap(True)
        self.hint.setContentsMargins(14, 2, 14, 0)
        self.hint.hide()
        lay.addWidget(self.hint)

        tools = QWidget()
        tl = QHBoxLayout(tools)
        tl.setContentsMargins(10, 8, 10, 10)
        tl.setSpacing(4)

        self.btn_material = IconTextButton(
            "folder", "添加材料", role="tool", icon_size=14, height=28,
            checkable=False, margins=(8, 0, 8, 0), spacing=6)
        self.btn_material.clicked.connect(self._pick_materials)
        tl.addWidget(self.btn_material)

        intent_label = QLabel("意图", objectName="charCount")
        intent_label.setContentsMargins(6, 0, 0, 0)
        tl.addWidget(intent_label)

        self.intent_combo = QComboBox()
        self.intent_combo.setProperty("role", "picker")
        self.intent_combo.setMinimumWidth(104)
        self.intent_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.intent_combo.currentIndexChanged.connect(self._on_intent)
        tl.addWidget(self.intent_combo)

        tl.addStretch(1)

        self.count = QLabel(f"0/{MAX_QUERY_CHARS}", objectName="charCount")
        tl.addWidget(self.count)

        self.btn_send = IconTextButton(
            "send", "", role="send", icon_size=16, height=30, checkable=False,
            color="#ffffff", margins=(7, 0, 7, 0))
        self.btn_send.setObjectName("sendBtn")
        self.btn_send.setToolTip("发送（Ctrl+Enter）")
        self.btn_send.setEnabled(False)
        self.btn_send.clicked.connect(self._submit)
        tl.addWidget(self.btn_send)

        lay.addWidget(tools)
        return box

    # ------------------------------------------------------------------ 提示
    def _say(self, text: str, *, danger: bool = False) -> None:
        """状态提示行。空文本即隐藏 —— 有内容的 QLabel 会一直占着高度。"""
        if not text:
            self.hint.hide()
            return
        self.hint.setText(text)
        self.hint.setStyleSheet(
            f"color: {theme.DANGER};" if danger else f"color: {theme.TEXT_DIM};")
        self.hint.show()

    # ------------------------------------------------------------------ 意图
    def load_intents(self) -> None:
        self._render_intents(None)       # 先按兜底渲染，避免首屏空白
        self._worker = Worker(self.client.request, "GET", "/chat/intents")
        self._worker.finished_with.connect(self._on_intents)
        # 拉不到就保持兜底渲染。**不在这里报错** —— 意图留空本来就能跑
        # （服务端自己判定），为此在首页顶一条红字只会让人以为出了故障。
        self._worker.failed.connect(lambda _exc: None)
        self._worker.start()

    def _on_intents(self, data: dict[str, Any]) -> None:
        items = list((data or {}).get("items") or [])
        if items:
            self._render_intents(items)

    def _render_intents(self, items: list[dict[str, Any]] | None) -> None:
        if items:
            known = {str(it.get("intent") or ""): str(it.get("label") or "")
                     for it in items}
            order = [k for k in INTENT_ORDER if k in known]
            order += [k for k in known if k not in INTENT_ORDER]
            rows = [(k, known[k]) for k in order if k]
        else:
            rows = list(FALLBACK_INTENTS)

        for card in self._cards:
            card.setParent(None)
            card.deleteLater()           # 只 setParent(None) 不释放，重建几次就积一堆
        self._cards = []

        for i, (intent, label) in enumerate(rows):
            card = TaskCard(INTENT_ICON.get(intent, "chat"), label,
                            INTENT_DESC.get(intent, ""))
            card.setToolTip(INTENT_TEMPLATE.get(intent, label))
            card.clicked.connect(
                lambda k=intent, t=label: self._use_template(k, t))
            self.grid.addWidget(card, i // CARD_COLUMNS, i % CARD_COLUMNS)
            self._cards.append(card)

        # 意图下拉与卡片同源，避免两处各列一份
        self.intent_combo.blockSignals(True)
        self.intent_combo.clear()
        self.intent_combo.addItem("自动判定", "")
        for intent, label in rows:
            self.intent_combo.addItem(label, intent)
        self.intent_combo.setCurrentIndex(0)
        self.intent_combo.blockSignals(False)
        self._intent = ""

    def _on_intent(self, _index: int) -> None:
        self._intent = str(self.intent_combo.currentData() or "")

    def _use_template(self, intent: str, label: str) -> None:
        """点卡片：模板放进输入框、光标落到末尾，**不发送**。"""
        text = INTENT_TEMPLATE.get(intent) or f"请处理下列{label}任务："
        old = self.input.toPlainText()
        if old.strip() and old.strip() != text:
            # 不静默覆盖：律师已经写了一半的内容被一张误点的卡片顶掉，
            # 是那种"看起来是我自己删的"的丢失
            self._say("已用模板替换原内容；原内容可用 Ctrl+Z 撤销。")
        self.input.setPlainText(text)
        cursor = self.input.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.input.setTextCursor(cursor)
        self.input.setFocus()

    # ------------------------------------------------------------------ 材料
    def _pick_materials(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择材料", "",
            "文档 (*.pdf *.docx *.doc *.txt *.md);;所有文件 (*)")
        if not files:
            return
        good = [p for p in files if Path(p).suffix.lower() in SUPPORTED_SUFFIX]
        skipped = [Path(p).name for p in files
                   if Path(p).suffix.lower() not in SUPPORTED_SUFFIX]
        if skipped:
            # 跳过哪些必须说清楚：静默跳过的表现是"审查时少了材料"，
            # 而用户以为已经传进去了
            self._say(f"跳过 {len(skipped)} 个不支持的格式："
                      f"{'、'.join(skipped[:3])}", danger=True)
        if not good:
            return
        self.btn_material.setEnabled(False)
        self._say(f"上传 {len(good)} 份材料…")
        self._worker = Worker(self._upload, good)
        self._worker.finished_with.connect(self._uploaded)
        self._worker.failed.connect(self._upload_failed)
        self._worker.start()

    def _upload(self, paths: list[str]) -> dict[str, Any]:
        """在工作线程里跑（网络调用不能占主线程）。"""
        from client.app.api_client import new_case_id

        case_id = self._case_id
        if not case_id:
            case_id = new_case_id()
            rec = self.client.create_case(f"桌面端案件 {case_id}", case_id=case_id)
            case_id = str(rec.get("case_id") or case_id)
        items = [self.client.upload_material(case_id, p) for p in paths]
        return {"case_id": case_id, "items": items}

    def _uploaded(self, data: dict[str, Any]) -> None:
        self.btn_material.setEnabled(True)
        self._case_id = str(data.get("case_id") or self._case_id)
        added = 0
        for rec in list(data.get("items") or []):
            if not rec.get("id"):
                continue
            self._materials.append(rec)
            added += 1
            self.chip_layout.insertWidget(self.chip_layout.count() - 1,
                                          self._make_chip(rec))
        if added:
            self.chip_row.show()
        self._say(f"已上传 {added} 份材料，发送时会一并交给智能体。")

    def _make_chip(self, rec: dict[str, Any]) -> QLabel:
        name = str(rec.get("filename") or "材料")
        chip = QLabel(f"{name}  ×")
        chip.setProperty("role", "chip")
        chip.setCursor(Qt.CursorShape.PointingHandCursor)
        chip.setToolTip("点击移除")

        def _remove(_event: Any) -> None:
            self._materials = [m for m in self._materials if m is not rec]
            chip.setParent(None)
            chip.deleteLater()
            if not self._materials:
                self.chip_row.hide()

        chip.mousePressEvent = _remove  # type: ignore[method-assign]
        return chip

    def _upload_failed(self, exc: Exception) -> None:
        self.btn_material.setEnabled(True)
        self._say(f"上传失败：{err_msg(exc)}", danger=True)

    # ------------------------------------------------------------------ 发送
    def _on_text(self) -> None:
        text = self.input.toPlainText()
        n = len(text)
        over = n > MAX_QUERY_CHARS
        self.count.setText(f"{n}/{MAX_QUERY_CHARS}")
        self.count.setStyleSheet(f"color: {theme.DANGER};" if over else "")
        self.btn_send.setEnabled(bool(text.strip()) and not over)
        if over:
            self._say(f"超出服务端上限 {n - MAX_QUERY_CHARS} 字，"
                      "请精简后再发送。", danger=True)

    def eventFilter(self, obj: Any, event: Any) -> bool:  # noqa: N802
        if obj is self.input:
            kind = event.type()
            if kind in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
                box = self.findChild(QFrame, "composer")
                if box is not None:
                    on = kind == QEvent.Type.FocusIn
                    box.setProperty("focused", "true" if on else "false")
                    box.style().unpolish(box)
                    box.style().polish(box)
            elif kind == QEvent.Type.KeyPress:
                ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
                if ctrl and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    self._submit()
                    return True
        return super().eventFilter(obj, event)

    def _submit(self) -> None:
        query = self.input.toPlainText().strip()
        if not query or len(query) > MAX_QUERY_CHARS:
            return
        self.submitted.emit(query, {
            "intent": self._intent,
            "case_id": self._case_id,
            "material_ids": [int(m["id"]) for m in self._materials
                             if m.get("id") is not None],
        })

    def payload_extra(self) -> dict[str, Any]:
        """当前上下文（意图 / 案件 / 材料）。供主窗口在发送前取用。"""
        return {
            "intent": self._intent,
            "case_id": self._case_id,
            "material_ids": [int(m["id"]) for m in self._materials
                             if m.get("id") is not None],
        }

    def clear(self) -> None:
        """发送后清空输入，但**保留材料** —— 材料是这次会话的上下文，
        连同问题一起清掉会让人以为白传了。"""
        self.input.clear()
