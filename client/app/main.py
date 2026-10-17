"""桌面端入口：登录 → 主窗口。

跑法：
    python -m client.app.main
打包后直接双击 EXE（见 ``client/legal_agent.spec``）。

**登录必须走工作线程。** 在主线程里点"登录"时界面会卡住，
如果这时服务器地址写错了，用户看到的是"程序死了"而不是"连不上"，
于是第一反应是强杀进程 —— 而真正的提示就在半秒后的响应里。

**``QApplication`` 只在 ``main()`` 里创建一次。** 在模块层建的话，
测试进程一 import 就想要一个显示环境，离屏测试会直接崩。

**主窗口是「侧栏 + 内容区」，不是页签。** 页签的问题是：所有工作面平铺在
一条横线上，功能一多就得靠左右滚动去找，而且"我现在在哪、下一步去哪"
全靠读页签文字。侧栏能放下分组与状态（最近任务、当前账号、连接地址），
这是办案时真正需要一直看见的东西。
"""

from __future__ import annotations

import sys
from typing import Any


def _fatal(msg: str) -> None:
    """把致命错误**真的显示出来**。

    打包后的 EXE 是 GUI 子系统，**没有控制台** —— 往 stderr 打印等于写进
    黑洞，用户看到的现象是"双击没反应"。所以 Windows 上额外走原生
    MessageBox（用 ctypes 直接调 user32，**不依赖 Qt** —— 走到这里时
    Qt 本来就起不来，用 QMessageBox 只会再抛一次同样的错）。
    """
    print(msg, file=sys.stderr)
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, msg, "法律智能体", 0x10)  # MB_ICONERROR
        except Exception:  # noqa: BLE001
            pass  # 连 MessageBox 都调不动时，至少 stderr 那行还在


try:
    from PySide6.QtCore import QSize, Qt
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import (
        QApplication, QButtonGroup, QDialog, QFrame, QHBoxLayout, QLabel,
        QLineEdit, QMainWindow, QMessageBox, QPushButton, QStackedWidget,
        QVBoxLayout, QWidget,
    )
except ImportError as exc:  # pragma: no cover - 只在缺 Qt 的环境里触发
    _fatal(
        f"没能启动界面：缺少 Qt 界面库（{exc}）。\n\n"
        "源码运行时装它：\n"
        "    .venv/Scripts/python.exe -m pip install --only-binary :all: PySide6\n\n"
        "**--only-binary :all: 不能省**：不带它 pip 会退回去做源码构建，\n"
        "实测卡 47 分钟才失败（PySide6 有兼容 Python 3.13 的 cp310-abi3 轮子，\n"
        "只是 pip 默认不会优先选它）。\n\n"
        "如果这里已经是打包出来的 EXE，说明打包时漏了 Qt —— 重跑\n"
        "scripts/build_exe.py，并确认它在同一套 Python 里执行。"
    )
    raise SystemExit(1) from None

from client.app import theme
from client.app.api_client import ApiClient
from client.app.icons import Icon, icon_pixmap
from client.app.state import AppState
from client.app.widgets import EmptyHint, IconTextButton, SearchBox
from client.app.workers import Worker
from client.panels import (
    AdminPanel, CapabilitiesPanel, CasesPanel, ChatPanel, DraftsPanel,
    HomePanel, ReviewPanel, SearchPanel, TasksPanel,
)
from client.panels.base import err_msg

# 侧栏导航。顺序与名称**跟随 Web 端侧栏**（web/index.html 的 <nav class="side">）——
# 同一个产品，两端导航不一致时，用户要在两边各记一套位置；这正是"界面统一"
# 要消灭的那种不一致。顺序即 QStackedWidget 的页序。
# ⚠️ 2026-09-20 Web 端整体换版后两端已不是逐项相同（那边 7 项、这里 9 项）：
# 差异逐条登记在 tests/test_client_ui.py 顶部的 _NAV_PAIRS / *_ONLY_NAV 里 ——
# 改导航时先看那张表，别凭记忆"补齐"另一端没有的页。
NAV_ITEMS: list[tuple[str, str]] = [
    ("工作台", "home"),
    ("AI 助手", "chat"),
    ("合同审查", "doc"),
    ("文书中心", "pen"),
    ("案件管理", "folder"),
    ("日程任务", "tasks"),
    ("法规检索", "search"),
    ("能力与降级", "layers"),
    ("管理后台", "shield"),
]
# 名字的顺序**必须**与 NAV_ITEMS 逐项对应（`NAV_ITEMS[index]` 是渲染时的取法）。
PAGE_HOME, PAGE_CHAT, PAGE_REVIEW, PAGE_DRAFTS, PAGE_CASES, PAGE_TASKS, \
    PAGE_SEARCH, PAGE_CAPS, PAGE_ADMIN = range(9)

# (分组标题, 该组包含的页下标)。Web 端侧栏 2026-09-27 起也有分组标题了，
# 但两边的分法不同（Web 是 AI 法律检索/审查/文书，这里是工作台/办案/系统），
# 所以分组仍不参与两端的逐项比对，只比对**共同条目的相对顺序**
# （见 tests/test_client_ui.py::test_nav_order_matches_between_clients）。
# 组内顺序 = 渲染顺序，要跟 NAV_ITEMS 的顺序一致。
NAV_GROUPS: list[tuple[str, list[int]]] = [
    ("工作台", [PAGE_HOME, PAGE_CHAT, PAGE_REVIEW]),
    ("办案", [PAGE_DRAFTS, PAGE_CASES, PAGE_TASKS, PAGE_SEARCH]),
    ("系统", [PAGE_CAPS, PAGE_ADMIN]),
]
RECENT_LIMIT = 12       # 侧栏最多拉多少条历史（再多请看任务中心）
RECENT_ROWS = 6         # 侧栏实际渲染多少条：导航从 5 项变 9 项后，再渲染 8 条
                        # 历史会把「设置」挤出一屏，砍到 6 条保住底部入口


class LoginDialog(QDialog):
    """登录。**只收集三样东西**，其余由服务端决定。

    视觉与 Web 端登录页对齐：上面一段深蓝品牌区，下面白底表单。
    之所以保留"服务器地址"输入框而 Web 端没有 —— 桌面端要连的可能是
    内网任一部署实例，那个地址用户自己知道，写死在配置里反而束手束脚。
    """

    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state
        self.client: ApiClient | None = None
        self._worker: Worker | None = None
        self.setWindowTitle("登录 · 法律智能体")
        self.setFixedWidth(430)
        self.setStyleSheet(theme.LOGIN_QSS)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- 品牌区 ----
        brand = QWidget()
        brand.setObjectName("brandBar")
        bl = QVBoxLayout(brand)
        bl.setContentsMargins(28, 24, 28, 22)
        bl.setSpacing(0)

        head = QHBoxLayout()
        head.setSpacing(10)
        # 对齐交给布局默认的垂直居中 —— 显式写 AlignTop 会让 34px 的图标
        # 比 20px 高的文字标签高出一截，看着像歪的。
        head.addWidget(Icon("scale", 36, "#ffffff"))
        head.addWidget(QLabel("LEGAL AGENT", objectName="brandWord"))
        head.addStretch(1)
        bl.addLayout(head)

        title = QLabel("律师专用多 Agent 法律咨询助手", objectName="brandTitle")
        title.setWordWrap(True)
        bl.addSpacing(12)
        bl.addWidget(title)
        sub = QLabel("律所内网私有化部署 · 桌面端与网页同一账号",
                     objectName="brandSub")
        sub.setWordWrap(True)
        bl.addSpacing(6)
        bl.addWidget(sub)
        root.addWidget(brand)

        # ---- 表单区 ----
        area = QWidget(objectName="formArea")
        fl = QVBoxLayout(area)
        fl.setContentsMargins(28, 22, 28, 24)
        fl.setSpacing(0)

        fl.addWidget(QLabel("登录工作台", objectName="formHead"))
        hint = QLabel("请使用律所统一分配的账号，并确认服务器地址。",
                      objectName="formHint")
        hint.setWordWrap(True)
        fl.addSpacing(5)
        fl.addWidget(hint)
        fl.addSpacing(18)

        def labeled(text: str, edit: QLineEdit) -> None:
            fl.addWidget(QLabel(text, objectName="fieldLabel"))
            fl.addSpacing(5)
            fl.addWidget(edit)
            edit.setMinimumHeight(34)

        self.url_edit = QLineEdit(state.settings.base_url)
        self.url_edit.setPlaceholderText("http://127.0.0.1:8001")
        labeled("服务器", self.url_edit)
        fl.addSpacing(12)

        self.user_edit = QLineEdit(state.settings.username)
        self.user_edit.setPlaceholderText("律所分配的登录名")
        labeled("登录名", self.user_edit)
        fl.addSpacing(12)

        self.pwd_edit = QLineEdit()
        self.pwd_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pwd_edit.setPlaceholderText("口令")
        labeled("口令", self.pwd_edit)

        self.tip = QLabel("", objectName="loginTip")
        self.tip.setWordWrap(True)
        self.tip.hide()
        fl.addSpacing(4)
        fl.addWidget(self.tip)
        fl.addSpacing(10)

        self.btn = QPushButton("登录", objectName="loginBtn")
        self.btn.setMinimumHeight(38)
        self.btn.setCursor(Qt.CursorShape.PointingHandCursor)
        fl.addWidget(self.btn)

        self.foot = QLabel(
            "会话令牌仅保存在内存中，退出后即清除，不写入本机配置文件。",
            objectName="loginFoot")
        self.foot.setWordWrap(True)
        self.foot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        fl.addSpacing(12)
        fl.addWidget(self.foot)

        root.addWidget(area)

        self.btn.clicked.connect(self._login)
        # 回车即可登录，不必每次都去点按钮 —— 三个输入框里任一处按回车都行
        for edit in (self.url_edit, self.user_edit, self.pwd_edit):
            edit.returnPressed.connect(self._login)
        if not self.user_edit.text():
            self.user_edit.setFocus()
        else:
            self.pwd_edit.setFocus()

    def _login(self) -> None:
        self.tip.hide()          # 有内容的 QLabel 会占高度，用 hide 而不是清空
        self.btn.setEnabled(False)
        self.btn.setText("登录中…")
        self.client = ApiClient(self.url_edit.text().strip(),
                                token_provider=self.state)
        self._worker = Worker(self.client.login,
                              self.user_edit.text().strip(),
                              self.pwd_edit.text())
        self._worker.finished_with.connect(self._ok)
        self._worker.failed.connect(self._fail)
        self._worker.start()

    def _ok(self, data: dict[str, Any]) -> None:
        self.state.user = dict(data.get("user") or {})
        self.state.settings.base_url = self.url_edit.text().strip()
        self.state.remember()   # 只存地址与用户名，不存凭证
        self.accept()

    def _fail(self, exc: Exception) -> None:
        self.btn.setEnabled(True)
        self.btn.setText("登录")
        # 直接显示服务端/客户端给的人话，不要自己再翻译成"登录失败"。
        self.tip.setText(err_msg(exc))
        self.tip.show()
        self.pwd_edit.selectAll()


class MainWindow(QMainWindow):
    """主窗口：左侧栏（新建 / 搜索 / 功能 / 最近任务）+ 右侧内容区。"""

    def __init__(self, client: ApiClient, state: AppState) -> None:
        super().__init__()
        self.client = client
        self.state = state
        self._recent: list[dict[str, Any]] = []
        self._worker: Worker | None = None
        self.setWindowTitle("法律智能体 · 律师工作台")
        self.resize(state.settings.window_width, state.settings.window_height)
        self.setMinimumSize(980, 620)
        self._build_ui()
        self._load_recent()

    # ------------------------------------------------------------------ 外壳
    def _build_ui(self) -> None:
        # 自定义容器要 QSS 背景必须开 WA_StyledBackground：不开的话
        # background 被静默忽略，界面看着像"样式没生效"却不报任何错。
        shell = QWidget(objectName="shell")
        shell.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCentralWidget(shell)
        row = QHBoxLayout(shell)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self._build_sidebar())
        row.addWidget(self._build_main(), 1)

    def _build_sidebar(self) -> QWidget:
        side = QWidget(objectName="sidebar")
        side.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        side.setFixedWidth(236)
        col = QVBoxLayout(side)
        col.setContentsMargins(12, 14, 12, 12)
        col.setSpacing(9)

        # ---- 新建任务 ----
        self.btn_new = QPushButton("  新建任务")
        self.btn_new.setObjectName("newTaskBtn")
        self.btn_new.setIcon(QIcon(icon_pixmap("plus", 15, "#ffffff")))
        self.btn_new.setIconSize(QSize(15, 15))
        self.btn_new.setMinimumHeight(36)
        self.btn_new.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_new.clicked.connect(self._new_task)
        col.addWidget(self.btn_new)

        # ---- 搜索（过滤下面的最近任务）----
        self.search = SearchBox("搜索任务和会话")
        self.search.edit.textChanged.connect(self._render_recent)
        col.addWidget(self.search)
        col.addSpacing(2)

        # ---- 功能（三组，与 Web 端 NAV 表逐项一致）----
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        added = 0
        for group_title, indexes in NAV_GROUPS:
            section, _ = self._section(group_title, None)
            col.addWidget(section)
            for index in indexes:
                if index == PAGE_ADMIN and not self.state.is_admin:
                    # 管理后台只对管理员渲染。与 Web 端同一口径：这只控制
                    # "少显示"，不控制"能不能调"—— 服务端对每个接口独立校验。
                    continue
                label, icon_name = NAV_ITEMS[index]
                btn = IconTextButton(icon_name, label, role="nav", height=32)
                self.nav_group.addButton(btn, index)
                col.addWidget(btn)
                added += 1
        self.nav_group.idClicked.connect(self._switch)
        if added == 0:      # 理论上到不了（工作台永远在），守一手
            col.addStretch(1)

        # ---- 最近任务 ----
        col.addSpacing(4)
        self.recent_section, self.recent_count = self._section("最近任务", 0)
        col.addWidget(self.recent_section)
        self.recent_box = QVBoxLayout()
        self.recent_box.setContentsMargins(0, 0, 0, 0)
        self.recent_box.setSpacing(2)
        col.addLayout(self.recent_box)

        col.addStretch(1)

        divider = QFrame(objectName="sideDivider")
        divider.setFixedHeight(1)
        # 自定义 QFrame 想用 QSS 背景必须开这个（见 widgets.py 模块说明）
        divider.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        col.addWidget(divider)

        self.btn_foot = IconTextButton("settings", "设置与高级功能", role="nav",
                                       height=32, checkable=False, icon_size=16)
        self.btn_foot.clicked.connect(self._show_settings)
        col.addWidget(self.btn_foot)

        who = self._display_name()
        info = QWidget()
        info_lay = QVBoxLayout(info)
        info_lay.setContentsMargins(10, 2, 6, 0)
        info_lay.setSpacing(1)
        info_lay.addWidget(QLabel(who, objectName="sideUser"))
        info_lay.addWidget(QLabel(self.state.settings.base_url,
                                  objectName="sideServer"))
        col.addWidget(info)

        return side

    def _section(self, title: str, count: int | None) -> tuple[QWidget, Any]:
        """分组标题行。返回 (容器, 计数标签)；不需要计数时第二个是 ``None``。"""
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(10, 0, 8, 0)
        lay.setSpacing(4)
        label = QLabel(title)
        label.setProperty("role", "group")
        lay.addWidget(label)
        lay.addStretch(1)
        if count is None:
            return box, None
        counter = QLabel(str(count))
        counter.setProperty("role", "count")
        lay.addWidget(counter)
        return box, counter

    def _build_main(self) -> QWidget:
        area = QWidget(objectName="mainArea")
        area.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        outer = QVBoxLayout(area)
        outer.setContentsMargins(10, 10, 10, 10)

        card = QWidget(objectName="mainCard")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        outer.addWidget(card)

        col = QVBoxLayout(card)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        col.addWidget(self._build_topbar())

        self.stack = QStackedWidget()
        self.home = HomePanel(self.client, self.state)
        self.home.submitted.connect(self._from_home)
        self.chat = ChatPanel(self.client, self.state)
        # 问答跑完就刷新侧栏历史：不刷的话，刚从首页发出去的任务要等下次
        # 启动才出现在"最近任务"里，而那个列表存在的理由正是"我刚跑的那个去哪了"
        self.chat.taskSettled.connect(self._load_recent)
        self.review = ReviewPanel(self.client, self.state)
        self.search_panel = SearchPanel(self.client, self.state)
        self.cases = CasesPanel(self.client, self.state)
        self.tasks = TasksPanel(self.client, self.state)
        self.drafts = DraftsPanel(self.client, self.state)
        self.caps = CapabilitiesPanel(self.client, self.state)
        self.admin = AdminPanel(self.client, self.state)
        # 页序必须与 NAV_ITEMS 一致 —— _switch 的标题、nav_group 的 id 全按这套下标走
        for page in (self.home, self.chat, self.review, self.cases,
                     self.drafts, self.tasks, self.search_panel, self.caps, self.admin):
            self.stack.addWidget(page)
        col.addWidget(self.stack, 1)

        self._switch(PAGE_HOME)
        self.tasks.refresh()
        self.drafts.refresh()
        return area

    def _build_topbar(self) -> QWidget:
        bar = QWidget(objectName="topBar")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        bar.setFixedHeight(52)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(20, 0, 14, 0)
        lay.setSpacing(10)

        self.page_title = QLabel(NAV_ITEMS[PAGE_HOME][0], objectName="pageTitle")
        lay.addWidget(self.page_title)
        lay.addStretch(1)
        lay.addWidget(QLabel(self.state.settings.base_url, objectName="topServer"))
        lay.addWidget(QLabel(self._display_name(), objectName="topUser"))

        btn_logout = QPushButton("退出登录")
        btn_logout.setProperty("role", "ghost")
        btn_logout.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_logout.clicked.connect(self.close)
        lay.addWidget(btn_logout)
        return bar

    def _display_name(self) -> str:
        return (self.state.user.get("full_name")
                or self.state.user.get("username") or "—")

    # ------------------------------------------------------------------ 导航
    def _switch(self, index: int) -> None:
        index = max(0, min(int(index), self.stack.count() - 1))
        self.stack.setCurrentIndex(index)
        button = self.nav_group.button(index)
        if button is not None and not button.isChecked():
            button.setChecked(True)
        self.page_title.setText(NAV_ITEMS[index][0])
        if index == PAGE_TASKS:
            # 进任务中心就刷新：这里看到的是"最新一次跑完没有"，
            # 停留在上次进来时的旧列表上会误导判断
            self.tasks.refresh()
        elif index == PAGE_CASES:
            # 案件列表同理 —— 别人在 Web 端建的案子，不该等你重启程序才出现
            self.cases.refresh()
        elif index == PAGE_CAPS:
            # 能力与降级回答的是"现在"的状态，进来就该重新问一次
            self.caps.refresh()

    def _new_task(self) -> None:
        """新建任务 = 回工作台首页 + 清空输入 + 光标就位。

        不清材料：材料是一次会话的上下文，点"新建"就把它清掉的话，
        连着处理几份合同时每份都要重新上传。
        """
        self._switch(PAGE_HOME)
        self.home.clear()
        self.home.input.setFocus()

    def _from_home(self, query: str, extra: dict[str, Any]) -> None:
        self._switch(PAGE_CHAT)
        self.chat.send_query(query, extra)
        self.home.clear()

    # ------------------------------------------------------------------ 最近任务
    def _load_recent(self) -> None:
        self._worker = Worker(self.client.list_tasks, limit=RECENT_LIMIT)
        self._worker.finished_with.connect(self._on_recent)
        # 侧栏拉不到历史不该弹错误：任务中心里能看到失败原因，
        # 这里报错只会让人以为登录状态坏了
        self._worker.failed.connect(lambda _exc: None)
        self._worker.start()

    def _on_recent(self, data: dict[str, Any]) -> None:
        self._recent = list((data or {}).get("items") or [])
        self.recent_count.setText(str(len(self._recent)))
        self._render_recent()

    def _render_recent(self) -> None:
        while self.recent_box.count():
            item = self.recent_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        keyword = self.search.edit.text().strip().lower()
        rows = [r for r in self._recent
                if not keyword or keyword in str(r.get("query") or "").lower()]

        if not rows:
            self.recent_box.addWidget(EmptyHint(
                "新建任务后，可在其中持续添加多轮对话" if not self._recent
                else f"没有匹配「{self.search.edit.text().strip()}」的任务"))
            return

        for row in rows[:8]:
            text = str(row.get("query") or "（无提问）")
            short = text if len(text) <= 16 else text[:16] + "…"
            btn = IconTextButton("clock", short, role="recent", height=30,
                                 checkable=False, icon_size=14,
                                 margins=(10, 0, 8, 0), spacing=7)
            btn.setToolTip(text)
            btn.clicked.connect(lambda _checked=False, r=row: self._open_task(r))
            self.recent_box.addWidget(btn)

    def _open_task(self, row: dict[str, Any]) -> None:
        self._switch(PAGE_TASKS)

    # ------------------------------------------------------------------ 设置
    def _show_settings(self) -> None:
        # is_admin 是 property（与 Web 端同一口径）。之前写成 is_admin()，
        # 但那个分支只在点开"设置"对话框时才走到 —— 是个潜伏的运行时错误
        role = "管理员" if self.state.is_admin else "普通用户"
        QMessageBox.information(
            self, "设置与高级功能",
            f"服务器：{self.state.settings.base_url}\n"
            f"账号：{self._display_name()}\n"
            f"角色：{role}\n\n"
            "桌面端只保留办案需要的最小设置。用户管理、知识库与能力开关\n"
            "在 Web 端的管理后台里改 —— 两边是同一套账号与权限。")

    # ------------------------------------------------------------------ 退出
    def closeEvent(self, event: Any) -> None:  # noqa: N802
        """退出时**清空内存里的案件正文与凭证**（见 ``state.logout``）。

        不这么做的话，下一个人打开程序时登录框背后还挂着一个装满
        案件原文的字典。
        """
        self.state.logout()
        super().closeEvent(event)


def main(argv: list[str] | None = None) -> int:
    try:
        app = QApplication(argv or sys.argv)
    except Exception as exc:  # pragma: no cover - 只在 Qt 环境损坏时触发
        # **装了 PySide6 但 Qt 起不来**，最常见是缺 Visual C++ 运行库。
        # 它在 GUI 程序里的表现同样是"双击没反应" —— 那句
        # "could not load the Qt platform plugin" 根本没地方显示。
        # （真正严重的情况 Qt 会直接 abort，Python 层捕获不到，
        #  这里至少覆盖能覆盖的那部分。）
        _fatal(
            f"Qt 界面库装了，但起不来（{exc}）。\n\n"
            "Windows 上最常见的原因是缺 Visual C++ 运行库，装完重试：\n"
            "    https://aka.ms/vs/17/release/vc_redist.x64.exe\n\n"
            "在没有显示器的环境（CI / SSH）里跑界面自检，请先设置：\n"
            "    QT_QPA_PLATFORM=offscreen"
        )
        return 1
    app.setApplicationName("LegalAgent")
    # 高分屏下不设这个，界面会糊成一团（Qt6 默认是整数缩放）
    if hasattr(Qt, "AA_EnableHighDpiScaling"):
        app.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    # 样式装在 QApplication 上（登录框另有自己的样式表覆盖）
    app.setStyleSheet(theme.app_qss())

    state = AppState()
    dlg = LoginDialog(state)
    if dlg.exec() != QDialog.Accepted:
        return 0
    client = dlg.client
    if client is None:
        return 1
    win = MainWindow(client, state)
    win.show()
    # **关掉主窗口就直接退出**，不循环回登录框。
    # 换人登录的价值抵不上它的代价：第二次调 app.exec() 时 QApplication
    # 已经 quit 过一轮，事件循环的状态和第一次不一样，
    # 容易出现"窗口闪一下就没了"这种查不动的问题。
    # 真要换人，重新启动程序即可 —— 反正凭证本来就不落盘。
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
