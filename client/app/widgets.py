"""可复用的几个小控件。

这里只放**被多个地方用到**的东西。功能面板内部的布局一律留在各自面板里 ——
一个"通用面板基类"最后总是长成什么都管的怪物，改一个面板得先读一遍基类。

两个 Qt 的坑写在这里，因为它们都表现为"样式写了没效果"：

1. **自定义 ``QWidget`` / ``QFrame`` 子类要 QSS 背景，必须开
   ``WA_StyledBackground``。** 不开的话 ``background`` 被静默忽略：不报错、
   不警告，就是不画。``QLabel`` / ``QPushButton`` 这些 Qt 自带的控件没这个问题，
   所以这个坑专坑"我自己写了个卡片类"的场景。
2. **动态属性改完必须 ``unpolish`` + ``polish``。** QSS 里的
   ``[focused="true"]`` 这类属性选择器只在 polish 时求值一次，
   改完属性不重新 polish，界面纹丝不动，同样不报错。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from client.app import theme
from client.app.icons import Icon


class IconTextButton(QPushButton):
    """左边一个矢量图标、右边一行文字的按钮（侧栏项、最近任务、底部入口都用它）。

    **不用 ``setIcon``。** ``QIcon`` 与文字之间的间距由 style 决定，QSS 里没有
    对应属性可调，实测比设计稿窄一半；而且图标颜色在选中态该跟着变，
    ``QPixmap`` 是渲染死的，变一次得重画一次。自己摆两个子控件反而最少代码。
    """

    def __init__(self, icon_name: str, text: str, *, role: str = "nav",
                 icon_size: int = 17, height: int = 34, checkable: bool = True,
                 color: str = "", margins: tuple[int, int, int, int] = (10, 0, 10, 0),
                 spacing: int = 9, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._idle = color or theme.TEXT_DIM
        self.setProperty("role", role)
        self.setCheckable(checkable)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if height:
            self.setMinimumHeight(height)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(*margins)
        layout.setSpacing(spacing)
        self.icon = Icon(icon_name, icon_size, self._idle)
        layout.addWidget(self.icon)

        self.label = QLabel(text)
        # 文字与图标都不吃点击 —— 否则用户点在字上时按钮收不到事件，
        # 表现是"点空白处有反应、点文字没反应"，极难描述也不好查
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        if text:
            layout.addWidget(self.label, 1)

        if checkable:
            self.toggled.connect(self._sync_icon)

    def _sync_icon(self, checked: bool) -> None:
        self.icon.setColor(theme.ACCENT if checked else self._idle)

    def setText(self, text: str) -> None:  # noqa: N802 - 与 Qt 命名一致
        # 文字实际画在子 QLabel 上；直接 super().setText() 会同时驱动按钮
        # 自己的绘制，出现"两行同样的字叠在一起"
        self.label.setText(text)


class SearchBox(QFrame):
    """带放大镜的搜索框。外框负责边框与圆角，内部 QLineEdit 自己不带框。"""

    def __init__(self, placeholder: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("searchBox")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(34)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 10, 0)
        layout.setSpacing(7)

        self.icon = Icon("search", 15, theme.TEXT_FAINT)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        self.edit.installEventFilter(self)
        layout.addWidget(self.icon)
        layout.addWidget(self.edit, 1)

    def eventFilter(self, obj: Any, event: Any) -> bool:  # noqa: N802
        if obj is self.edit and event.type() in (QEvent.Type.FocusIn,
                                                 QEvent.Type.FocusOut):
            focused = event.type() == QEvent.Type.FocusIn
            self.setProperty("focused", "true" if focused else "false")
            # 动态属性不会自动触发重新求值，必须手工 repolish（见模块 docstring）
            self.style().unpolish(self)
            self.style().polish(self)
            self.icon.setColor(theme.ACCENT if focused else theme.TEXT_FAINT)
        return super().eventFilter(obj, event)


class TaskCard(QFrame):
    """首页的任务卡片：左图标方块、中间标题+说明、右上角箭头。整张卡可点。

    QSS 的 ``:hover`` 对自定义 ``QFrame`` 子类需要显式开 ``WA_Hover``，
    不开的话鼠标悬停没有任何反馈 —— 而"卡片能点"这件事全靠悬停提示。
    """

    clicked = Signal()

    def __init__(self, icon_name: str, title: str, desc: str,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "taskCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 13, 14, 13)
        layout.setSpacing(12)

        badge = QWidget()
        badge.setProperty("role", "cardIcon")
        badge.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        badge.setFixedSize(38, 38)
        badge_layout = QVBoxLayout(badge)
        badge_layout.setContentsMargins(0, 0, 0, 0)
        badge_layout.addWidget(Icon(icon_name, 19, theme.ACCENT), 0,
                               Qt.AlignmentFlag.AlignCenter)

        text_box = QVBoxLayout()
        text_box.setContentsMargins(0, 0, 0, 0)
        text_box.setSpacing(4)
        self.title = QLabel(title)
        self.title.setProperty("role", "cardTitle")
        self.desc = QLabel(desc)
        self.desc.setProperty("role", "cardDesc")
        self.desc.setWordWrap(True)
        for lbl in (self.title, self.desc):
            lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        text_box.addWidget(self.title)
        text_box.addWidget(self.desc)

        arrow = Icon("arrow-up-right", 15, theme.TEXT_FAINT)
        layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(text_box, 1)
        layout.addWidget(arrow, 0, Qt.AlignmentFlag.AlignTop)

    def mouseReleaseEvent(self, event: Any) -> None:  # noqa: N802
        # 只有"按下与松开都在卡片内"才算点击：否则按住卡片拖到外面松手
        # 也会触发，那是在做拖拽的用户被意外带进一个页面
        if (event.button() == Qt.MouseButton.LeftButton
                and self.rect().contains(event.position().toPoint())):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class EmptyHint(QLabel):
    """列表为空时的提示。虚线框 + 居中文字，与参考图的空状态卡片一致。"""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text)
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # 控件级样式表直接作用于自身，不写选择器 —— 带上属性选择器反而
        # 多一个"属性值写错了就不生效"的失配点
        self.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; font-size: 11.5px;"
            f" border: 1px dashed {theme.BORDER}; border-radius: 8px;"
            f" padding: 14px 10px; background: {theme.PANEL_2};")
