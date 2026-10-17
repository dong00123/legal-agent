"""能力与降级面板：这套系统"现在到底能做到什么"的自述页。

Web 端只在页面上放了一张「AI 能力状态」小卡（``web/index.html``），而这里做成独立一页，
理由同样
成立：本系统大量依赖可降级的子系统 —— 向量库回落本地 numpy、BM25 不可用、
reranker 降级、Redis / RabbitMQ 没开。**它们都不会让接口失败，只会让结果
质量悄悄变差**；律师看到的是"一份像模像样的报告"，而报告背后少了两路召回。

所以排序是刻意的：**降级项在最前面**，其次才是后端矩阵。把降级藏在最后或者
做成一个小图标，等于没有这页。

桌面端只做 Web 端五个 tab 里的「降级与后端」这一块：工具清单 / Agent 目录 /
交付判据 / 意图路由是开发排查视图，留在 Web 端；桌面端要回答的是
"我这份报告能信到什么程度"。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QLabel, QPushButton, QSplitter,
    QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from client.app import theme
from client.app.workers import Worker
from client.panels.base import err_msg

COLUMNS = ["子系统", "状态", "当前目标", "降级到"]


def _esc(v: Any) -> str:
    from html import escape
    return escape(str(v if v is not None else "—"))


def _fmt_duration(sec: Any) -> str:
    v = int(sec or 0)
    if v < 60:
        return f"{v} 秒"
    if v < 3600:
        return f"{v // 60} 分 {v % 60} 秒"
    return f"{v // 3600} 小时 {(v % 3600) // 60} 分"


class CapabilitiesPanel(QWidget):
    """/health 的可读版：降级横幅 + 生效后端矩阵。"""

    def __init__(self, client: Any, state: Any) -> None:
        super().__init__()
        self.client = client
        self.state = state
        self._worker: Worker | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        btn = QPushButton("刷新")
        btn.clicked.connect(self.refresh)
        bar.addWidget(btn)
        self.status = QLabel("就绪")
        self.status.setProperty("role", "hint")
        bar.addWidget(self.status, 1)
        root.addLayout(bar)

        # 横幅区（降级 / 问题 / 运行态摘要在上），矩阵在下 —— 顺序即优先级
        self.banners = QTextBrowser()
        self.banners.setPlaceholderText("点「刷新」读取系统健康状态。")
        self.matrix = QTableWidget(0, len(COLUMNS))
        self.matrix.setHorizontalHeaderLabels(COLUMNS)
        self.matrix.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.matrix.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.matrix.verticalHeader().setVisible(False)

        split = QSplitter(Qt.Vertical)
        split.addWidget(self.banners)
        split.addWidget(self.matrix)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        root.addWidget(split, 1)

    def refresh(self) -> None:
        self.status.setText("读取中…")
        self._worker = Worker(self.client.request, "GET", "/health")
        self._worker.finished_with.connect(self._fill)
        self._worker.failed.connect(self._fail)
        self._worker.start()

    def _fail(self, exc: Exception) -> None:
        self.status.setText(f"读取失败：{err_msg(exc)}")
        self.banners.setHtml(
            f"<p style='color:{theme.DANGER};'>健康检查取不到：{_esc(err_msg(exc))}</p>"
            "<p style='color:%s;'>这一页的价值全在「子系统现在是哪个后端」上，"
            "拿不到健康检查就什么都说明不了。请先确认后端服务可访问。</p>"
            % theme.TEXT_DIM)

    # ------------------------------------------------------------------ 渲染
    def _fill(self, health: dict[str, Any]) -> None:
        self.status.setText("就绪")
        self.banners.setHtml(self._banners_html(health))
        self._fill_matrix(health)

    def _banners_html(self, health: dict[str, Any]) -> str:
        warn, ok, danger, dim, faint = (theme.WARN, theme.OK, theme.DANGER,
                                        theme.TEXT_DIM, theme.TEXT_FAINT)
        parts: list[str] = []

        status = str(health.get("status") or "unknown")
        label = {"ok": "全部按设计运行", "degraded": "降级运行（业务可用）",
                 "error": "关键路径不可用"}.get(status, status)
        color = {"ok": ok, "degraded": warn, "error": danger}.get(status, dim)
        head = (f"<p style='margin:0 0 6px 0;'><b style='color:{color};'>{label}</b>"
                f" <span style='color:{faint};font-size:11px;'>"
                f"v{_esc(health.get('version', '—'))}"
                f" · 已运行 {_esc(_fmt_duration(health.get('uptime_seconds')))}</span></p>")
        parts.append(head)

        backends = health.get("backends") or {}
        off = [(k, v) for k, v in backends.items() if not v.get("enabled")]

        # ---- 最前面：谁降级了
        if off:
            items = "".join(
                f"<li><b>{_esc(name)}</b> → {_esc(v.get('fallback') or '（未声明回落方案）')}</li>"
                for name, v in off)
            parts.append(
                f"<p style='color:{warn};background:{theme.INFO_SOFT};padding:8px 10px;"
                f"border-radius:6px;margin:0 0 8px 0;'><b>{len(off)} 个子系统当前没启用，"
                f"系统在回落方案上运行：</b></p><ul style='margin:0 0 8px 18px;'>{items}</ul>"
                f"<p style='color:{dim};margin:0 0 8px 0;'>降级<b>不是故障</b>：报告照样"
                "出得来。但召回质量与功能覆盖会低于设计值，引用核验与多模态识别这类能力"
                "可能整段缺失。</p>")
        else:
            parts.append(f"<p style='color:{ok};margin:0 0 8px 0;'>"
                         "<b>没有子系统处于降级状态。</b></p>")

        problems = list(health.get("problems") or [])
        if problems:
            items = "".join(f"<li>{_esc(p)}</li>" for p in problems)
            parts.append(
                f"<p style='color:{danger};background:{theme.DANGER_SOFT};padding:8px 10px;"
                f"border-radius:6px;margin:0 0 8px 0;'><b>关键路径有 {len(problems)} 个问题：</b></p>"
                f"<ul style='margin:0 0 8px 18px;color:{danger};'>{items}</ul>")

        degraded = list(health.get("degraded") or [])
        if degraded:
            items = "".join(f"<li>{_esc(d)}</li>" for d in degraded)
            parts.append(
                f"<p style='color:{dim};margin:8px 0 2px 0;'><b>当前处于降级运行</b> —— "
                "以下子系统未启用或不可用，系统仍能出报告，但相应能力会被削弱。"
                "报告会照常生成，不代表这些能力已经生效：</p>"
                f"<ul style='margin:2px 0 8px 18px;color:{dim};'>{items}</ul>")

        default_note = ("本内容由 AI 辅助生成，仅供律师工作参考，不构成正式法律意见。")
        parts.append(
            f"<p style='color:{faint};font-size:11px;margin:10px 0 0 0;'>"
            f"{_esc(health.get('disclaimer') or default_note)}</p>")
        return "".join(parts)

    def _fill_matrix(self, health: dict[str, Any]) -> None:
        backends = health.get("backends") or {}
        # 未启用的排前面 —— 这一页的优先级就是"哪里在降级"
        rows = sorted(backends.items(), key=lambda kv: (kv[1].get("enabled"), kv[0]))
        self.matrix.setRowCount(len(rows))
        for i, (name, b) in enumerate(rows):
            enabled = bool(b.get("enabled"))
            cells = [
                str(name),
                "已启用" if enabled else "未启用",
                str(b.get("target") or "—"),
                str(b.get("fallback") or "—"),
            ]
            for j, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if j == 1:
                    from PySide6.QtGui import QColor  # 局部导入：只在有矩阵要染色时用
                    item.setForeground(QColor(theme.OK if enabled else theme.WARN))
                self.matrix.setItem(i, j, item)
        self.matrix.resizeColumnToContents(0)
