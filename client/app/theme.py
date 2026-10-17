"""桌面端的颜色常量与样式表。

**QSS 不支持 CSS 变量**，颜色只能在 Python 里写一次、再拼进样式串。
比起在二十来个选择器里各抄一遍十六进制，至少改一处能全改。

取值**刻意与 Web 端 light 主题逐条对齐**。2026-09-20 那次换版后，Web 端把样式
内联进了 ``web/index.html`` / ``web/login.html`` 的 ``<style>``，变量名也换成了
``--tp`` / ``--ts`` / ``--bg`` / ``--br`` / ``--ac`` 这套短名 —— 每个常量后面
都注明了它对应哪个变量。同一个产品，网页端与桌面端不该出现两种蓝：改这里时
记得同步那边，改那边时也记得回来（守卫见 ``tests/test_client_ui.py``）。

桌面端**不做暗色主题**。Qt 应用跟随系统窗口配色，硬加一套会和用户自己的
Windows 主题打架（标题栏、滚动条、系统对话框都是系统管的，改一半更难看）；
Web 端跑在浏览器里没有这个问题，所以那边才有必要自己切。
"""

from __future__ import annotations

from string import Template

# ---------------------------------------------------------------- 颜色
# 对应 Web 端（web/index.html / web/login.html 的内联 <style>）里的 :root 变量。
# 只有以 " #" 开头的模块级常量会被 _sub() 替换进样式串，其余常量（半径、字号）
# 不受影响。注释里的 --xx 就是那边对应的变量名。
BG = "#ffffff"             # --bg
PANEL = "#ffffff"          # 面板与底色同为白（新版是白底 + 细边框的扁平风）
PANEL_2 = "#f5f5f5"        # --mu
BORDER = "#e6e6e6"         # --br
BORDER_STRONG = "#d4d4d4"  # Web 端无此变量，在 --br 上加深一档
TEXT = "#1a1a1a"           # --tp
TEXT_DIM = "#4d4d4d"       # --ts
TEXT_FAINT = "#8a8a8a"     # --tt

ACCENT = "#2f6bff"         # --ac
ACCENT_HOVER = "#1f57e0"   # Web 端无此变量，主色加深约 12%
ACCENT_PRESSED = "#1746b8"  # 同上，再深一档
ACCENT_SOFT = "#eef3ff"    # 同上，主色的极淡底

OK = "#1d9e57"             # --ps
OK_SOFT = "#e9f7f0"        # Web 端无此变量，--ps 的极淡底
WARN = "#c07d10"           # --wn
DANGER = "#dc3b3b"         # --dg
DANGER_SOFT = "#fdecec"    # Web 端无此变量，--dg 的极淡底
INFO_SOFT = "#f5f5f5"      # Web 端无此变量，用 --mu 当中性信息底

# 登录页品牌区。Web 端那边是 ``.left{background:var(--tp)}`` 的**纯色**（近黑），
# 不再是旧版的深蓝渐变 —— 所以这两个常量现在同值，原来的渐变自然消失。
BRAND_DEEP = "#1a1a1a"     # --tp
BRAND_MID = "#1a1a1a"      # 同 --tp


def _sub(tpl: str) -> str:
    """把样式串里的 ``$NAME`` 换成上面的颜色常量。

    **不用 f-string**：QSS 里全是 ``{}``，f-string 会把每个花括号都当成占位符，
    于是整段样式要写成 ``{{`` ``}}`` —— 改一次样式就得数一遍括号，
    比抄颜色更容易出错。``string.Template`` 只认 ``$``，与 CSS 语法不冲突。
    """
    colors = {k: v for k, v in globals().items()
              if k.isupper() and isinstance(v, str) and v.startswith("#")}
    return Template(tpl).substitute(colors)


# ---------------------------------------------------------------- 侧栏 / 外壳
SHELL_QSS = _sub("""
QWidget#shell { background: $BG; }

QWidget#sidebar {
    background: $PANEL;
    border-right: 1px solid $BORDER;
}

/* 「新建任务」用主色实心 —— 参考图这里是全屏唯一的实心红按钮，
   视觉重心落在它身上。本项目主色是深蓝，逻辑照搬。 */
QPushButton#newTaskBtn {
    background: $ACCENT;
    color: #ffffff;
    border: none;
    border-radius: 8px;
    padding: 10px 12px;
    font-size: 13.5px;
    font-weight: 600;
}
QPushButton#newTaskBtn:hover   { background: $ACCENT_HOVER; }
QPushButton#newTaskBtn:pressed { background: $ACCENT_PRESSED; }

/* 搜索框：外框负责边框与圆角，里面的 QLineEdit 自己不带框 ——
   这样放大镜图标能真正落在框内，而不是和一个带边框的输入框并排站。 */
QFrame#searchBox {
    background: $PANEL_2;
    border: 1px solid $BORDER;
    border-radius: 7px;
}
QFrame#searchBox[focused="true"] { border: 1px solid $ACCENT; }
QFrame#searchBox QLineEdit {
    border: none;
    background: transparent;
    padding: 0px;
    font-size: 12.5px;
    color: $TEXT;
}

QLabel[role="group"] {
    color: $TEXT_FAINT;
    font-size: 11px;
    font-weight: 600;
}
QLabel[role="count"] {
    color: $TEXT_FAINT;
    font-size: 11px;
}

/* 侧栏项。用 :checked 而不是自定义属性来控制选中态 —— 动态属性改了要
   手动 unpolish/polish 才生效，而 :checked 是 Qt 原生伪状态，不需要。 */
QPushButton[role="nav"] {
    background: transparent;
    border: none;
    border-radius: 6px;
    padding: 0px;
    text-align: left;
}
QPushButton[role="nav"]:hover   { background: $INFO_SOFT; }
QPushButton[role="nav"]:checked { background: $ACCENT_SOFT; }
QPushButton[role="nav"] QLabel         { color: $TEXT; font-size: 13px; }
QPushButton[role="nav"]:checked QLabel { color: $ACCENT; font-weight: 600; }

/* 最近任务列表项 */
QPushButton[role="recent"] {
    background: transparent;
    border: none;
    border-radius: 6px;
    padding: 0px;
    text-align: left;
}
QPushButton[role="recent"]:hover { background: $INFO_SOFT; }
QPushButton[role="recent"] QLabel { color: $TEXT_DIM; font-size: 12px; }

/* 侧栏底部入口复用 role="nav" 的样式，这里不再另写一份 ——
   曾经写过一个 #sideFootBtn 的版本，但那个 objectName 从来没被设过，
   于是整组规则静默失效（见 tests/test_client_ui.py 里的守卫）。 */

/* 主区：一张白卡浮在灰底上 */
QWidget#mainArea  { background: $BG; }
QWidget#mainCard  {
    background: $PANEL;
    border: 1px solid $BORDER;
    border-radius: 12px;
}
QWidget#topBar { background: transparent; border-bottom: 1px solid $BORDER; }
QLabel#pageTitle { color: $TEXT; font-size: 14px; font-weight: 600; }
QLabel#topUser   { color: $TEXT; font-size: 12.5px; font-weight: 600; }
QLabel#topServer { color: $TEXT_FAINT; font-size: 11px; }

QLabel#sideUser   { color: $TEXT; font-size: 12.5px; font-weight: 600; }
QLabel#sideServer { color: $TEXT_FAINT; font-size: 11px; }
QFrame#sideDivider { background: $BORDER; border: none; }

/* 承载各页面的堆叠容器与滚动区都该是"空玻璃"，背景由它背后的白卡负责。
   不设的话，它们的默认底色会把白卡盖成另一块色，看着像两个面板拼在一起。 */
QStackedWidget { background: transparent; border: none; }
QScrollArea    { background: transparent; border: none; }

QPushButton[role="ghost"] {
    background: $PANEL;
    color: $TEXT_DIM;
    border: 1px solid $BORDER;
    border-radius: 7px;
    padding: 6px 11px;
    font-size: 12px;
}
QPushButton[role="ghost"]:hover { border: 1px solid $BORDER_STRONG; color: $TEXT; }
""")


# ---------------------------------------------------------------- 工作台首页
HOME_QSS = _sub("""
QLabel#homeWord {
    color: $ACCENT;
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 1.2px;
}
QLabel#homeTitle { color: $TEXT; font-size: 27px; font-weight: 700; }
QLabel#homeSub   { color: $TEXT_DIM; font-size: 13px; }
QLabel#homeHint  { color: $TEXT_FAINT; font-size: 12px; }

/* 任务卡片。给自定义 QFrame 设背景必须同时开 WA_StyledBackground，
   否则 QSS 里的 background 会被**静默忽略** —— 不报错，就是不画。 */
QFrame[role="taskCard"] {
    background: $PANEL;
    border: 1px solid $BORDER;
    border-radius: 10px;
}
QFrame[role="taskCard"]:hover {
    border: 1px solid $ACCENT;
    background: $PANEL_2;
}
QFrame[role="taskCard"] QLabel[role="cardTitle"] {
    color: $TEXT; font-size: 14px; font-weight: 600;
}
QFrame[role="taskCard"] QLabel[role="cardDesc"] {
    color: $TEXT_DIM; font-size: 12px;
}
/* 卡片图标底色比 $ACCENT_SOFT 再深一档：$ACCENT_SOFT 是给"整块选中背景"
   用的，缩到 38×38 的小方块上在白卡片里几乎看不出来，方块这个造型就白做了。 */
QWidget[role="cardIcon"] {
    background: #dfe9f8;
    border-radius: 9px;
}

/* 底部输入区 */
QFrame#composer {
    background: $PANEL;
    border: 1px solid $BORDER;
    border-radius: 12px;
}
QFrame#composer[focused="true"] { border: 1px solid $ACCENT; }
QTextEdit#homeInput {
    border: none;
    background: transparent;
    color: $TEXT;
    font-size: 13px;
    padding: 12px 14px 0px 14px;
}
QLabel#charCount { color: $TEXT_FAINT; font-size: 11px; }
/* 这里原本还有一条 QLabel#homeFoot —— 工作台首页重构后那个脚注标签没了，
   规则留在 QSS 里永远匹配不到（守卫报的就是它）。已删除，别再抄回来。 */

/* 工具行按钮的内边距由控件内部的布局负责，QSS 这里必须是 0 ——
   两边都设的话会叠加：QSS 的 padding 会先缩掉内容区，布局留白再加一层，
   结果按钮里的图标被挤到贴着边框。 */
QPushButton[role="tool"] {
    background: transparent;
    color: $TEXT_DIM;
    border: none;
    border-radius: 6px;
    padding: 0px;
    font-size: 12px;
}
QPushButton[role="tool"]:hover { background: $INFO_SOFT; color: $TEXT; }

QPushButton#sendBtn {
    background: $ACCENT;
    color: #ffffff;
    border: none;
    border-radius: 15px;
    padding: 0px;
    font-size: 15px;
}
QPushButton#sendBtn:hover    { background: $ACCENT_HOVER; }
QPushButton#sendBtn:pressed  { background: $ACCENT_PRESSED; }
QPushButton#sendBtn:disabled { background: #c6d2e2; }

QComboBox[role="picker"] {
    background: transparent;
    border: none;
    color: $TEXT_DIM;
    font-size: 12px;
    padding: 4px 6px;
}
QComboBox[role="picker"]:hover { background: $INFO_SOFT; border-radius: 6px; }
QComboBox[role="picker"]::drop-down { border: none; width: 16px; }
QComboBox[role="picker"] QAbstractItemView {
    background: $PANEL;
    border: 1px solid $BORDER;
    selection-background-color: $ACCENT_SOFT;
    selection-color: $ACCENT;
    color: $TEXT;
    outline: none;
}

/* 材料标签 */
QLabel[role="chip"] {
    background: $INFO_SOFT;
    color: $TEXT_DIM;
    border: 1px solid $BORDER;
    border-radius: 10px;
    padding: 2px 8px;
    font-size: 11px;
}
""")


# ---------------------------------------------------------------- 各功能面板
PANEL_QSS = _sub("""
QWidget { color: $TEXT; font-size: 13px; }

QPushButton {
    background: $PANEL;
    color: $TEXT;
    border: 1px solid $BORDER_STRONG;
    border-radius: 6px;
    padding: 6px 12px;
    font-size: 12.5px;
}
QPushButton:hover    { border: 1px solid $ACCENT; color: $ACCENT; }
QPushButton:pressed  { background: $ACCENT_SOFT; }
QPushButton:disabled { color: $TEXT_FAINT; border: 1px solid $BORDER; }

QLineEdit, QComboBox {
    background: $PANEL;
    color: $TEXT;
    border: 1px solid $BORDER_STRONG;
    border-radius: 6px;
    padding: 5px 9px;
    font-size: 12.5px;
    selection-background-color: $ACCENT;
    selection-color: #ffffff;
}
QLineEdit:focus, QComboBox:focus { border: 1px solid $ACCENT; }

QTextEdit, QPlainTextEdit {
    background: $PANEL;
    color: $TEXT;
    border: 1px solid $BORDER;
    border-radius: 8px;
    font-size: 12.5px;
    padding: 6px;
}

QListWidget, QTableWidget, QTreeWidget {
    background: $PANEL;
    color: $TEXT;
    border: 1px solid $BORDER;
    border-radius: 8px;
    font-size: 12.5px;
    outline: none;
}
QListWidget::item, QTableWidget::item { padding: 5px 6px; }
QListWidget::item:selected, QTableWidget::item:selected {
    background: $ACCENT_SOFT; color: $ACCENT;
}
QHeaderView::section {
    background: $PANEL_2;
    color: $TEXT_DIM;
    border: none;
    border-right: 1px solid $BORDER;
    border-bottom: 1px solid $BORDER;
    padding: 6px;
    font-size: 12px;
}
QTableWidget { gridline-color: $BORDER; }

/* 进度条槽用 $BORDER 而不是更浅的 $INFO_SOFT：在白色卡片上，$INFO_SOFT
   淡到看不出是控件，整条进度条会像一根分隔线。 */
QProgressBar {
    background: $BORDER;
    border: none;
    border-radius: 4px;
    height: 8px;
    text-align: center;
    color: $TEXT_DIM;
    font-size: 10px;
}
QProgressBar::chunk { background: $ACCENT; border-radius: 4px; }

QSplitter::handle { background: $BORDER; }
QSplitter::handle:hover { background: $BORDER_STRONG; }

/* 管理后台的三个 tab。tab 头用 PANEL_2 底 + 选中反白，与页签下的白底区分开 */
QTabWidget::pane { border: 1px solid $BORDER; border-radius: 8px; top: -1px; }
QTabBar::tab {
    background: $PANEL_2;
    color: $TEXT_DIM;
    border: 1px solid $BORDER;
    padding: 5px 14px;
    font-size: 12.5px;
}
QTabBar::tab:selected { background: $PANEL; color: $ACCENT; font-weight: 600; }

/* 面板里的次要说明行（"共 N 条"、"就绪"这一类）。角色化而不是逐个
   setStyleSheet：颜色集中在这里，改一处即全改。 */
QLabel[role="hint"] { color: $TEXT_FAINT; font-size: 11.5px; }
QLabel#panelTip { color: $TEXT_FAINT; font-size: 11.5px; }

QScrollBar:vertical   { background: transparent; width: 10px; margin: 2px; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
    background: $BORDER_STRONG; border-radius: 4px; min-height: 28px; min-width: 28px;
}
QScrollBar::handle:hover { background: $TEXT_FAINT; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0px; width: 0px; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
""")


# 登录页沿用原来的取值，只是挪到这里统一管理（原 _LOGIN_QSS 内容不变）
LOGIN_QSS = _sub("""
QWidget#brandBar {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 $BRAND_DEEP, stop:0.52 $ACCENT, stop:1 $BRAND_MID);
}
QLabel#brandWord  { color: #cfdff2; font-size: 11px; font-weight: 600;
                    letter-spacing: 1.6px; }
QLabel#brandTitle { color: #ffffff; font-size: 21px; font-weight: 700; }
QLabel#brandSub   { color: #b9cee8; font-size: 12px; }

QWidget#formArea { background: $PANEL; }
QLabel#formHead  { color: $TEXT; font-size: 15px; font-weight: 600; }
QLabel#formHint  { color: $TEXT_DIM; font-size: 12px; }
QLabel#fieldLabel { color: $TEXT_DIM; font-size: 12px; }

QLineEdit {
    border: 1px solid $BORDER_STRONG;
    border-radius: 5px;
    padding: 7px 10px;
    font-size: 13px;
    background: $PANEL;
    color: $TEXT;
    selection-background-color: $ACCENT;
}
QLineEdit:focus { border: 1px solid $ACCENT; }
QLineEdit:hover:!focus { border: 1px solid #b3bcc7; }

QPushButton#loginBtn {
    background: $ACCENT;
    color: #ffffff;
    border: none;
    border-radius: 5px;
    padding: 9px 14px;
    font-size: 13px;
    font-weight: 600;
}
QPushButton#loginBtn:hover   { background: $ACCENT_HOVER; }
QPushButton#loginBtn:pressed { background: $ACCENT_PRESSED; }
QPushButton#loginBtn:disabled { background: #9db6d4; }

QLabel#loginTip   { color: $DANGER; font-size: 12px; }
QLabel#loginFoot  { color: $TEXT_FAINT; font-size: 11px; }
""")


def app_qss() -> str:
    """登录页之外的全部样式，一次装到 QApplication 上。"""
    return SHELL_QSS + HOME_QSS + PANEL_QSS
