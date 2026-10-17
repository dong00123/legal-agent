"""管理后台面板（管理员可见）。

桌面端定位是**只读速览**：概览数字、用户名册、当天审计 —— 这些是管理员
日常想"看一眼"的东西。建用户、改配置、知识库与法规库维护这一类**写操作**
留在 Web 端管理后台：它们的表单复杂（枚举、校验、二次确认），在桌面端重做
一套必然与 Web 端行为分叉，而两端共用同一套账号与权限，跳过去是最便宜的
"保持一致"。

这不是偷懒的借口，是范围决策 —— 与 ``main.py`` 里
「设置与高级功能」对话框的说法保持同一口径。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (
    QAbstractItemView, QDateEdit, QHBoxLayout, QLabel, QPushButton, QSplitter,
    QTabWidget, QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout,
    QWidget,
)

from client.app import theme
from client.app.widgets import EmptyHint
from client.app.workers import Worker
from client.panels.base import err_msg

# 审计表列。与 Web 端 admin.js 审计 tab 的列对齐，砍掉只对排查有用的
# IP 与关联跳转（桌面端没有路由可跳）。
AUDIT_COLUMNS = ["时间", "动作", "操作人", "对象", "详情", "结果", "来源端"]
USER_COLUMNS = ["用户名", "姓名", "角色", "团队", "状态"]
STAT_LABEL = {
    "tasks": "任务总量", "cases": "案件", "users": "用户", "statutes": "法规",
}
CLIENT_LABEL = {"web": "Web", "exe": "EXE", "api": "接口"}


def _esc(v: Any) -> str:
    from html import escape
    return escape(str(v if v is not None else "—"))


class AdminPanel(QWidget):
    """三个只读 tab：概览 / 用户 / 审计。"""

    def __init__(self, client: Any, state: Any) -> None:
        super().__init__()
        self.client = client
        self.state = state
        self._worker: Worker | None = None
        self._build_ui()

    # ------------------------------------------------------------------ 界面
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        if not self.state.is_admin:
            root.addWidget(EmptyHint(
                "该功能仅管理员可用。如需权限请联系管理员 —— \n"
                "两端共用同一套账号与权限。"))
            root.addStretch(1)
            self.tabs = None
            return

        self.tabs = QTabWidget()

        # ---- 概览
        ov = QWidget()
        ov_lay = QVBoxLayout(ov)
        ov_lay.setContentsMargins(10, 10, 10, 10)
        ov_bar = QHBoxLayout()
        self.btn_stats = QPushButton("刷新")
        self.btn_stats.clicked.connect(self.refresh_stats)
        ov_bar.addWidget(self.btn_stats)
        ov_bar.addStretch(1)
        ov_lay.addLayout(ov_bar)
        self.stats_box = QTextBrowser()
        self.stats_box.setPlaceholderText("点「刷新」读取全所概览。")
        ov_lay.addWidget(self.stats_box)
        self.tabs.addTab(ov, "概览")

        # ---- 用户
        us = QWidget()
        us_lay = QVBoxLayout(us)
        us_lay.setContentsMargins(10, 10, 10, 10)
        us_bar = QHBoxLayout()
        self.btn_users = QPushButton("刷新")
        self.btn_users.clicked.connect(self.refresh_users)
        us_bar.addWidget(self.btn_users)
        us_bar.addStretch(1)
        us_lay.addLayout(us_bar)
        self.user_table = QTableWidget(0, len(USER_COLUMNS))
        self.user_table.setHorizontalHeaderLabels(USER_COLUMNS)
        self.user_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.user_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.user_table.verticalHeader().setVisible(False)
        us_lay.addWidget(self.user_table)
        self.user_status = QLabel("就绪")
        self.user_status.setProperty("role", "hint")
        us_lay.addWidget(self.user_status)
        self.tabs.addTab(us, "用户")

        # ---- 审计
        au = QWidget()
        au_lay = QVBoxLayout(au)
        au_lay.setContentsMargins(10, 10, 10, 10)
        au_bar = QHBoxLayout()
        self.audit_date = QDateEdit(QDate.currentDate())
        self.audit_date.setCalendarPopup(True)
        self.audit_date.setDisplayFormat("yyyy-MM-dd")
        au_bar.addWidget(QLabel("日期"))
        au_bar.addWidget(self.audit_date)
        self.btn_audit = QPushButton("刷新")
        self.btn_audit.clicked.connect(self.refresh_audit)
        au_bar.addWidget(self.btn_audit)
        au_bar.addStretch(1)
        au_lay.addLayout(au_bar)
        self.audit_table = QTableWidget(0, len(AUDIT_COLUMNS))
        self.audit_table.setHorizontalHeaderLabels(AUDIT_COLUMNS)
        self.audit_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.audit_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.audit_table.verticalHeader().setVisible(False)
        self.audit_table.setColumnWidth(0, 150)
        self.audit_table.setColumnWidth(1, 170)
        self.audit_table.setColumnWidth(3, 190)
        au_lay.addWidget(self.audit_table)
        self.audit_status = QLabel("就绪")
        self.audit_status.setProperty("role", "hint")
        au_lay.addWidget(self.audit_status)
        self.tabs.addTab(au, "审计")

        root.addWidget(self.tabs, 1)

        note = QLabel("桌面端管理页只读。用户管理、系统配置、知识库与法规库维护"
                      "在 Web 端的管理后台进行 —— 两端共用同一套账号与权限。")
        note.setProperty("role", "hint")
        note.setWordWrap(True)
        root.addWidget(note)

    # ------------------------------------------------------------------ 概览
    def refresh_stats(self) -> None:
        self.btn_stats.setEnabled(False)
        self._worker = Worker(self.client.request, "GET", "/admin/stats/dashboard")
        self._worker.finished_with.connect(self._fill_stats)
        self._worker.failed.connect(self._stats_fail)
        self._worker.start()

    def _fill_stats(self, d: dict[str, Any]) -> None:
        self.btn_stats.setEnabled(True)
        faint, dim, accent = theme.TEXT_FAINT, theme.TEXT_DIM, theme.ACCENT
        parts = [
            f"<p style='color:{faint};font-size:11px;margin:0 0 8px 0;'>"
            f"全所概览（近 {_esc(d.get('days', '—'))} 天） · 统计时间 "
            f"{_esc(str(d.get('generated_at') or ''))[:19]}</p>",
        ]
        cards = [
            ("任务总量", (d.get("tasks") or {}).get("total"),
             f"近 {d.get('days', '—')} 天 {(d.get('tasks') or {}).get('recent', 0)}"),
            ("案件", (d.get("cases") or {}).get("cases"),
             f"材料 {(d.get('cases') or {}).get('materials', 0)} 份"),
            ("用户", (d.get("users") or {}).get("users"),
             f"启用 {(d.get('users') or {}).get('active_users', 0)} · "
             f"团队 {(d.get('users') or {}).get('teams', 0)}"),
            ("法规", (d.get("statutes") or {}).get("statutes"),
             f"条文 {(d.get('statutes') or {}).get('articles', 0)}"),
        ]
        for label, value, sub in cards:
            parts.append(
                f"<p style='margin:0 0 6px 0;'><b style='font-size:15px;color:{accent};'>"
                f"{_esc(value if value is not None else 0)}</b> "
                f"<b>{_esc(label)}</b> <span style='color:{dim};font-size:11px;'>"
                f"{_esc(sub)}</span></p>")
        by_status = (d.get("tasks") or {}).get("by_status") or {}
        if by_status:
            pills = " · ".join(f"{_esc(k)} {_esc(v)}" for k, v in by_status.items())
            parts.append(f"<p style='color:{dim};margin:8px 0 0 0;'>"
                         f"任务状态分布：{pills}</p>")
        cit = d.get("citations")
        if cit:
            parts.append(f"<p style='color:{dim};margin:2px 0 0 0;'>引用拦截率 "
                         f"{float(cit.get('intercept_rate') or 0) * 100:.1f}%</p>")
        self.stats_box.setHtml("".join(parts))

    def _stats_fail(self, exc: Exception) -> None:
        self.btn_stats.setEnabled(True)
        self.stats_box.setHtml(
            f"<p style='color:{theme.DANGER};'>读取失败：{_esc(err_msg(exc))}</p>")

    # ------------------------------------------------------------------ 用户
    def refresh_users(self) -> None:
        self.user_status.setText("加载中…")
        self._worker = Worker(self.client.request, "GET", "/admin/users",
                              params={"limit": 100})
        self._worker.finished_with.connect(self._fill_users)
        self._worker.failed.connect(self._users_fail)
        self._worker.start()

    def _fill_users(self, data: dict[str, Any]) -> None:
        rows = list((data or {}).get("items") or [])
        self.user_table.setRowCount(len(rows))
        for i, u in enumerate(rows):
            team = u.get("team_id")
            cells = [
                str(u.get("username") or ""),
                str(u.get("full_name") or "—"),
                "管理员" if u.get("role") == "admin" else "律师",
                f"#{team}" if team else "未分配",
                "启用" if u.get("is_active", True) else "停用",
            ]
            for j, text in enumerate(cells):
                self.user_table.setItem(i, j, QTableWidgetItem(text))
        self.user_table.resizeColumnToContents(0)
        self.user_status.setText(f"共 {len(rows)} 个用户（只读；增删改在 Web 端）")

    def _users_fail(self, exc: Exception) -> None:
        self.user_status.setText(f"加载失败：{err_msg(exc)}")

    # ------------------------------------------------------------------ 审计
    def refresh_audit(self) -> None:
        self.audit_status.setText("加载中…")
        day = self.audit_date.date().toString("yyyy-MM-dd") or str(date.today())
        self._worker = Worker(self.client.request, "GET", "/admin/audit",
                              params={"limit": 100, "date_from": day, "date_to": day})
        self._worker.finished_with.connect(self._fill_audit)
        self._worker.failed.connect(self._audit_fail)
        self._worker.start()

    def _fill_audit(self, data: dict[str, Any]) -> None:
        rows = list((data or {}).get("items") or [])
        self.audit_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            cells = [
                str(r.get("ts") or "")[:19],
                str(r.get("action") or ""),
                str(r.get("username") or (f"#{r['user_id']}" if r.get("user_id") else "—")),
                str(r.get("resource") or "—"),
                str(r.get("detail") or "—")[:80],
                str(r.get("result") or "ok"),
                CLIENT_LABEL.get(str(r.get("client") or ""), r.get("client") or "—"),
            ]
            for j, text in enumerate(cells):
                self.audit_table.setItem(i, j, QTableWidgetItem(text))
        self.audit_status.setText(
            f"当天 {len(rows)} 条（只读；审计不可删改，没有删除按钮是刻意的）")

    def _audit_fail(self, exc: Exception) -> None:
        self.audit_status.setText(f"加载失败：{err_msg(exc)}")
