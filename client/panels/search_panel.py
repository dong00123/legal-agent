"""法规检索面板。

⚠️ 换版后的 Web 端**没有这一页**（那边只有「类案检索」#pg-search，按案情找裁判
文书），按法条检索目前只有桌面端有。原 Web 实现的说明在这里同样成立，其中最重要的
一条被原样搬过来：**降级声明必须放在结果之前**。向量库回落本地 numpy、BM25
不可用、reranker 降级 —— 这些都不会让接口失败，只会让命中质量悄悄变差。
降级了却看起来像正常结果，是法律场景最不该发生的事。

四类检索各走各的工具通道（``statute_search`` / ``case_search`` /
``knowledge_search`` / ``material_search``），不跑完整 Agent 流程：要的是
"给我几条依据"，不是"给我一份报告"。

**材料检索必须选案件。** 服务端强制要求 ``case_id`` 并校验案件权限 ——
否则材料检索就成了"用检索接口绕过案件隔离"的读取通道。这个约束在界面上
提前说（未选案件时按钮旁直接提示），而不是等 422 回来再让人猜。
"""

from __future__ import annotations

import html
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTextBrowser,
    QVBoxLayout, QWidget,
)

from client.app import theme
from client.app.workers import Worker
from client.panels.base import err_msg

# (接口路径段, 界面文案)。与 Web 端 renderSearch 的 KINDS 逐项一致。
KINDS: list[tuple[str, str]] = [
    ("statute", "法规"),
    ("case", "类案"),
    ("knowledge", "知识库"),
    ("material", "案件材料"),
]

# 响应里各类命中的字段名不同（法规是 statutes、材料是 items ……）
HIT_KEYS = {"statute": "statutes", "case": "cases",
            "knowledge": "knowledges", "material": "items"}

TOP_K_CHOICES = (3, 6, 10, 20)


def _esc(text: Any) -> str:
    """命中片段按**纯文本**转义后渲染。片段直接来自卷宗与法规原文，
    里面完全可能有恶意构造 —— Web 端一律 textContent，这里一律 escape。"""
    return html.escape(str(text if text is not None else ""))


class SearchPanel(QWidget):
    """依据检索：只做检索、不出报告；要完整分析请用「智能问答」。"""

    def __init__(self, client: Any, state: Any) -> None:
        super().__init__()
        self.client = client
        self.state = state
        self._cases: list[dict[str, Any]] = []
        self._worker: Worker | None = None
        self._build_ui()

    # ------------------------------------------------------------------ 界面
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        form = QHBoxLayout()
        form.setSpacing(8)

        self.kind = QComboBox()
        for key, label in KINDS:
            self.kind.addItem(label, key)
        self.kind.setMinimumWidth(92)
        self.kind.currentIndexChanged.connect(self._on_kind)
        form.addWidget(self.kind)

        self.query = QLineEdit()
        self.query.setPlaceholderText(
            "检索式，例如：未签订书面劳动合同二倍工资 / 《民法典》第 577 条")
        self.query.returnPressed.connect(self.search)
        form.addWidget(self.query, 1)

        self.topk = QComboBox()
        for n in TOP_K_CHOICES:
            self.topk.addItem(f"前 {n} 条", n)
        self.topk.setCurrentIndex(1)
        form.addWidget(self.topk)

        self.case_combo = QComboBox()
        self.case_combo.addItem("（材料检索需先选案件）", "")
        self.case_combo.setMinimumWidth(190)
        self.case_combo.hide()
        form.addWidget(self.case_combo)

        self.btn = QPushButton("检索")
        self.btn.clicked.connect(self.search)
        form.addWidget(self.btn)
        root.addLayout(form)

        tip = QLabel("只做检索、不出报告；要完整分析请用「智能问答」。\n"
                     "材料检索只在本案范围内进行 —— 需要先选一个案件。",
                     objectName="panelTip")
        root.addWidget(tip)

        self.status = QLabel("就绪")
        self.status.setProperty("role", "hint")
        root.addWidget(self.status)

        # 结果用 QTextBrowser 而不是 QTableWidget：四类命中的字段完全不同，
        # 表格要么留一堆空列，要么四套模型；富文本一段一段排，降级横幅也能顶到最前
        self.result = QTextBrowser()
        self.result.setOpenExternalLinks(False)
        self.result.setPlaceholderText("输入检索式后点「检索」。")
        root.addWidget(self.result, 1)

    # ------------------------------------------------------------------ 行为
    def _on_kind(self, index: int) -> None:
        is_material = str(self.kind.itemData(index)) == "material"
        self.case_combo.setVisible(is_material)
        if is_material and self.case_combo.count() <= 1:
            self._load_cases()

    def _load_cases(self) -> None:
        self._worker = Worker(self.client.list_cases, limit=100)
        self._worker.finished_with.connect(self._on_cases)
        self._worker.failed.connect(lambda _exc: None)   # 选不了案件时发送前会被拦
        self._worker.start()

    def _on_cases(self, data: dict[str, Any]) -> None:
        self._cases = list((data or {}).get("items") or [])
        current = self.case_combo.currentData()
        self.case_combo.blockSignals(True)
        self.case_combo.clear()
        self.case_combo.addItem("（材料检索需先选案件）", "")
        for c in self._cases:
            title = str(c.get("title") or c.get("cause_of_action") or "")
            self.case_combo.addItem(f"{c.get('case_id')} · {title[:20]}",
                                    str(c.get("case_id") or ""))
        if current:
            i = self.case_combo.findData(current)
            if i >= 0:
                self.case_combo.setCurrentIndex(i)
        self.case_combo.blockSignals(False)

    def search(self) -> None:
        q = self.query.text().strip()
        if not q:
            self.status.setText("请先输入检索式。")
            self.query.setFocus()
            return
        kind = str(self.kind.currentData() or "statute")
        body: dict[str, Any] = {"query": q, "top_k": int(self.topk.currentData() or 6)}
        if kind == "material":
            case_id = str(self.case_combo.currentData() or "")
            if not case_id:
                self.status.setText("材料检索需要先选择案件 —— 否则它就成了"
                                    "绕过案件隔离的读取通道。")
                return
            body["case_id"] = case_id

        self.btn.setEnabled(False)
        self.status.setText("正在检索…")
        self._worker = Worker(self.client.request, "POST", f"/search/{kind}", json=body)
        self._worker.finished_with.connect(lambda data: self._show(kind, data))
        self._worker.failed.connect(self._fail)
        self._worker.start()

    # ------------------------------------------------------------------ 结果
    def _show(self, kind: str, res: dict[str, Any]) -> None:
        self.btn.setEnabled(True)
        self.status.setText("就绪")
        self.result.setHtml(self._render(kind, res))

    def _fail(self, exc: Exception) -> None:
        self.btn.setEnabled(True)
        self.status.setText(f"检索失败：{err_msg(exc)}")
        self.result.setHtml("")

    def _render(self, kind: str, res: dict[str, Any]) -> str:
        accent = theme.ACCENT
        dim = theme.TEXT_DIM
        faint = theme.TEXT_FAINT
        warn = theme.WARN
        danger = theme.DANGER
        ok = theme.OK

        parts: list[str] = []

        # ---- 降级与统计。**降级声明必须在结果之前**（见模块 docstring）
        if res.get("degraded"):
            parts.append(
                f'<p style="color:{warn};background:{theme.INFO_SOFT};'
                f'padding:7px 10px;border-radius:6px;margin:0 0 8px 0;">'
                f"<b>本次检索降级</b> —— 命中质量可能下降。</p>")
        else:
            parts.append(
                f'<p style="color:{ok};margin:0 0 8px 0;">未降级</p>')
        if res.get("fallback_note"):
            parts.append(
                f'<p style="color:{dim};background:{theme.INFO_SOFT};'
                f'padding:7px 10px;border-radius:6px;margin:0 0 8px 0;">'
                f"<b>降级说明：</b>{_esc(res['fallback_note'])}</p>")
        if res.get("error"):
            parts.append(
                f'<p style="color:{danger};margin:0 0 8px 0;">'
                f"<b>工具错误：</b>{_esc(res['error'])}</p>")

        ret = res.get("retrieval") or {}
        if ret:
            parts.append(
                f'<p style="color:{faint};font-size:11px;margin:0 0 8px 0;">'
                f"向量命中 {_esc(ret.get('vector_hits', 0))} · "
                f"BM25 命中 {_esc(ret.get('bm25_hits', 0))} · "
                f"融合后 {_esc(ret.get('fused', 0))}</p>")

        items = list(res.get("items") or [])
        parts.append(
            f'<p style="color:{dim};margin:0 0 4px 0;">'
            f"共 {_esc(res.get('count', len(items)))} 条命中"
            f" · 耗时 {_esc(res.get('elapsed_ms', 0))} ms</p><hr>")

        if not items:
            parts.append(
                f'<p style="color:{dim};margin:10px 0 0 0;">没有命中。'
                "可以换用更通用的关键词，或确认相关法规/知识文件是否已入库并建索引。</p>")
            return "".join(parts)

        render = {"statute": self._statute, "case": self._case,
                  "knowledge": self._knowledge, "material": self._material}[kind]
        for it in items:
            parts.append(render(it))
            parts.append('<div style="border-top:1px solid '
                         f"{theme.BORDER};margin:8px 0;\"></div>")
        return "".join(parts)

    # ---- 四类命中。字段与 Web 端 search.js 的四个 render 一一对应
    def _statute(self, it: dict[str, Any]) -> str:
        warn, ok, danger = theme.WARN, theme.OK, theme.DANGER
        status = str(it.get("status") or "效力状态未知")
        color = ok if ("现行有效" in status or "有效" in status) else (
            danger if ("废止" in status or "失效" in status) else warn)
        head = (f"<b>{_esc(it.get('doc_name', '—'))}"
                f"{_esc(it.get('article_no') or '')}</b>"
                f' <span style="color:{color};">[{_esc(status)}]</span>')
        if it.get("verified") is False:
            head += ' <span style="color:%s;">[未通过核验]</span>' % warn
        if it.get("score") is not None:
            head += (f' <span style="color:{theme.TEXT_FAINT};font-size:11px;">'
                     f"score {float(it['score']):.3f}</span>")
        meta = " · ".join(str(x) for x in (
            it.get("publisher"),
            f"生效 {it['effective_date']}" if it.get("effective_date") else "",
            f"废止 {it['repealed_date']}" if it.get("repealed_date") else "",
            it.get("version"), it.get("retriever")) if x)
        return (f"<p style='margin:6px 0 2px 0;'>{head}</p>"
                f"<p style='margin:0 0 2px 0;white-space:pre-wrap;'>"
                f"{_esc(it.get('text') or '')}</p>"
                f"<p style='color:{theme.TEXT_FAINT};font-size:11px;margin:0;'>"
                f"{_esc(meta)}</p>")

    def _case(self, it: dict[str, Any]) -> str:
        warn, ok = theme.WARN, theme.OK
        head = f"<b>{_esc(it.get('case_name') or '（未命名案例）')}</b>"
        if it.get("verified") is True:
            head += f' <span style="color:{ok};">[已核验]</span>'
        elif it.get("verified") is False:
            head += f' <span style="color:{warn};">[未通过核验 · 不得正式引用]</span>'
        if it.get("score") is not None:
            head += (f' <span style="color:{theme.TEXT_FAINT};font-size:11px;">'
                     f"score {float(it['score']):.3f}</span>")
        meta = " · ".join(str(x) for x in (
            it.get("case_no") or "（无案号）", it.get("court"),
            it.get("judge_date"), it.get("cause_of_action"),
            it.get("source")) if x)
        body = ""
        if it.get("gist"):
            body += f"<p style='margin:4px 0 0 0;'>裁判要旨：{_esc(it['gist'])}</p>"
        if it.get("result"):
            body += f"<p style='margin:2px 0 0 0;'>裁判结果：{_esc(it['result'])}</p>"
        if it.get("reasoning"):
            text = _esc(str(it["reasoning"])[:220])
            body += (f"<p style='color:{theme.TEXT_DIM};margin:2px 0 0 0;'>"
                     f"裁判理由：{text}</p>")
        return (f"<p style='margin:6px 0 2px 0;'>{head}</p>"
                f"<p style='color:{theme.TEXT_FAINT};font-size:11px;margin:0;'>"
                f"{_esc(meta)}</p>{body}")

    def _knowledge(self, it: dict[str, Any]) -> str:
        warn, ok = theme.WARN, theme.OK
        head = f"<b>{_esc(it.get('title') or '（未命名文档）')}</b>"
        if it.get("version"):
            head += f' <span style="color:{ok};">[版本 {_esc(it["version"])}]</span>'
        else:
            # 指引必须标版本，否则无法判断作业标准是否已更新
            head += f' <span style="color:{warn};">[未标注版本 · 请确认是否最新]</span>'
        if it.get("doc_type"):
            head += (f' <span style="color:{theme.TEXT_FAINT};font-size:11px;">'
                     f"[{_esc(it['doc_type'])}]</span>")
        if it.get("score") is not None:
            head += (f' <span style="color:{theme.TEXT_FAINT};font-size:11px;">'
                     f"score {float(it['score']):.3f}</span>")
        meta = " · ".join(str(x) for x in (
            f"生效 {it['effective_date']}" if it.get("effective_date") else "",
            it.get("chunk_id")) if x)
        snippet = (f"<p style='margin:4px 0 0 0;white-space:pre-wrap;'>"
                   f"{_esc(it.get('snippet') or '')}</p>") if it.get("snippet") else ""
        return (f"<p style='margin:6px 0 2px 0;'>{head}</p>{snippet}"
                f"<p style='color:{theme.TEXT_FAINT};font-size:11px;margin:0;'>"
                f"{_esc(meta)}</p>")

    def _material(self, it: dict[str, Any]) -> str:
        meta = it.get("metadata") if isinstance(it.get("metadata"), dict) else it
        line = " · ".join(str(x) for x in (
            meta.get("filename") or meta.get("title") or "材料片段",
            f"第 {meta['page']} 页" if meta.get("page") else "",
            meta.get("section") or "",
            f"score {float(it['score']):.3f}" if it.get("score") is not None else "",
        ) if x)
        return (f"<p style='color:{theme.TEXT_DIM};font-size:11px;"
                f"margin:6px 0 2px 0;'>{_esc(line)}</p>"
                f"<p style='margin:0;white-space:pre-wrap;'>"
                f"{_esc(it.get('text') or it.get('snippet') or '')}</p>")
