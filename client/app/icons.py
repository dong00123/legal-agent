"""矢量图标：用 ``QPainter`` 现画，不加载图片、不依赖 QtSvg。

三个实测理由，不是洁癖：

1. **字符图标造型随系统字体走。** 天平 ``⚖``、房子 ``⌂``、放大镜 ``⌕``
   这类字符落在不同的字体里，装了什么字体就长什么样；其中 ``⌂ ⌕ ◷ ◈ ❐``
   的字体覆盖率很低，换台机器（尤其是容器与精简系统）直接变豆腐块 ——
   而开发者机器上永远看不出这个问题。
2. **emoji 会被系统 emoji 字体接管**（``⚖ ⚙ ✎``），在部分环境渲染成黑白方块。
3. **不引 QtSvg。** 只为加载几个图标就多带一个 Qt 子模块，等于多一处打包
   失败点；而这些图形每个只有几笔。``QPainter`` 在 QtGui 里，PyInstaller
   一定带得上。

坐标系统一 24×24，按 ``size / 24`` 缩放 —— 改尺寸不会走形。笔画线宽同样
按缩放比例给（写死像素的话，16px 的图标看上去会比 32px 的粗一倍）。
一律圆头圆角、只描边不填充，与 Web 端的 SVG sprite（``web/index.html`` 里的
``<symbol id="i-*">``）是同一套造型语言。
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QWidget

GRID = 24.0          # 设计网格边长
DEFAULT_STROKE = 1.7  # 网格坐标系下的线宽

# 绘制函数的签名：(painter, s)，s = 实际边长 / 24
Drawer = Callable[[QPainter, float], None]


# ------------------------------------------------------------------ 画图小工具
def _line(p: QPainter, s: float, x1: float, y1: float, x2: float, y2: float) -> None:
    p.drawLine(QPointF(x1 * s, y1 * s), QPointF(x2 * s, y2 * s))


def _circle(p: QPainter, s: float, cx: float, cy: float, r: float) -> None:
    p.drawEllipse(QPointF(cx * s, cy * s), r * s, r * s)


def _dot(p: QPainter, s: float, cx: float, cy: float, r: float) -> None:
    """实心小圆。用当前画笔色填充 —— 需要盖住底下的线时才用。"""
    p.setBrush(QBrush(p.pen().color()))
    p.drawEllipse(QPointF(cx * s, cy * s), r * s, r * s)
    p.setBrush(Qt.BrushStyle.NoBrush)


def _poly(p: QPainter, s: float, pts: list[tuple[float, float]],
          close: bool = False) -> None:
    path = QPainterPath()
    path.moveTo(QPointF(pts[0][0] * s, pts[0][1] * s))
    for x, y in pts[1:]:
        path.lineTo(QPointF(x * s, y * s))
    if close:
        path.closeSubpath()
    p.drawPath(path)


# ------------------------------------------------------------------ 图形定义
def _plus(p: QPainter, s: float) -> None:
    _line(p, s, 12, 5.5, 12, 18.5)
    _line(p, s, 5.5, 12, 18.5, 12)


def _search(p: QPainter, s: float) -> None:
    _circle(p, s, 10.8, 10.8, 6.4)
    _line(p, s, 15.6, 15.6, 20.0, 20.0)


def _home(p: QPainter, s: float) -> None:
    _poly(p, s, [(3.4, 10.6), (12, 3.8), (20.6, 10.6)])
    _poly(p, s, [(6.1, 9.8), (6.1, 19.8), (17.9, 19.8), (17.9, 9.8)])
    _poly(p, s, [(10.2, 19.8), (10.2, 14.4), (13.8, 14.4), (13.8, 19.8)])


def _scale(p: QPainter, s: float) -> None:
    """天平。七笔，与登录页品牌标记同一个图形。"""
    _line(p, s, 12, 5, 12, 19)       # 立柱
    _line(p, s, 8, 21, 16, 21)       # 底座
    _line(p, s, 4, 7.5, 20, 7.5)     # 横梁
    _line(p, s, 4, 7.5, 4, 10.5)     # 左悬线
    _line(p, s, 20, 7.5, 20, 10.5)   # 右悬线
    for cx in (4.0, 20.0):           # 秤盘：顶横线 + 向下凸的半弧
        r = 2.2
        path = QPainterPath()
        path.moveTo(QPointF((cx - r) * s, 10.5 * s))
        path.lineTo(QPointF((cx + r) * s, 10.5 * s))
        # arcTo 会先补一条直线到弧起点，正好把秤盘两侧接上
        path.arcTo(QRectF((cx - r) * s, 10.5 * s, 2 * r * s, 2 * r * s), 0, -180)
        path.closeSubpath()
        p.drawPath(path)
    _dot(p, s, 12, 5, 1.3)           # 支点


def _doc(p: QPainter, s: float) -> None:
    """文档（带折角），用于「合同审查」。"""
    _poly(p, s, [(6.4, 3.4), (14.2, 3.4), (18.6, 7.8), (18.6, 20.6), (6.4, 20.6)],
          close=True)
    _poly(p, s, [(14.2, 3.4), (14.2, 7.8), (18.6, 7.8)])
    _line(p, s, 9.6, 12.4, 15.4, 12.4)
    _line(p, s, 9.6, 16.0, 15.4, 16.0)


def _book(p: QPainter, s: float) -> None:
    """摊开的书，用于「法规查询」。"""
    _poly(p, s, [(12, 6.9), (4.4, 4.7), (4.4, 17.3), (12, 19.5)], close=True)
    _poly(p, s, [(12, 6.9), (19.6, 4.7), (19.6, 17.3), (12, 19.5)], close=True)


def _chart(p: QPainter, s: float) -> None:
    """坐标轴 + 折线，用于「案件分析」。"""
    _poly(p, s, [(4.2, 4.4), (4.2, 19.8), (19.8, 19.8)])
    _poly(p, s, [(7.2, 15.6), (10.8, 11.0), (14.0, 13.6), (18.2, 7.4)])
    _dot(p, s, 18.2, 7.4, 1.05)


def _pen(p: QPainter, s: float) -> None:
    """铅笔，用于「文书起草」。"""
    _poly(p, s, [(5.0, 19.6), (5.9, 15.4), (16.6, 4.7), (19.3, 7.4), (8.6, 18.1)],
          close=True)
    _line(p, s, 15.3, 6.0, 18.0, 8.7)


def _chat(p: QPainter, s: float) -> None:
    """对话气泡，用于「法律咨询」。"""
    _poly(p, s, [(3.6, 5.4), (20.4, 5.4), (20.4, 16.6), (11.6, 16.6),
                 (7.6, 20.4), (7.6, 16.6), (3.6, 16.6)], close=True)


def _tasks(p: QPainter, s: float) -> None:
    for cy in (7.4, 12.0, 16.6):
        _dot(p, s, 5.2, cy, 1.15)
    _line(p, s, 9.0, 7.4, 19.8, 7.4)
    _line(p, s, 9.0, 12.0, 19.8, 12.0)
    _line(p, s, 9.0, 16.6, 16.6, 16.6)


def _folder(p: QPainter, s: float) -> None:
    _poly(p, s, [(3.4, 6.9), (9.6, 6.9), (11.5, 9.5), (20.6, 9.5),
                 (20.6, 18.9), (3.4, 18.9)], close=True)


def _settings(p: QPainter, s: float) -> None:
    """三条带滑块的横线（调音台式），比齿轮好画也好认。"""
    for cy, cx in ((7.4, 15.4), (12.0, 8.4), (16.6, 13.4)):
        _line(p, s, 3.4, cy, 20.6, cy)
        _dot(p, s, cx, cy, 2.0)


def _send(p: QPainter, s: float) -> None:
    _line(p, s, 12, 19.6, 12, 5.4)
    _poly(p, s, [(6.6, 10.8), (12, 5.4), (17.4, 10.8)])


def _arrow_up_right(p: QPainter, s: float) -> None:
    _line(p, s, 7.0, 17.0, 17.0, 7.0)
    _poly(p, s, [(9.4, 7.0), (17.0, 7.0), (17.0, 14.6)])


def _clock(p: QPainter, s: float) -> None:
    _circle(p, s, 12, 12, 8.2)
    _poly(p, s, [(12, 6.8), (12, 12.4), (16.4, 14.6)])


def _user(p: QPainter, s: float) -> None:
    _circle(p, s, 12, 8.6, 3.6)
    _poly(p, s, [(5.2, 20.2), (7.4, 16.6), (16.6, 16.6), (18.8, 20.2)])


def _logout(p: QPainter, s: float) -> None:
    _poly(p, s, [(10.6, 4.4), (4.4, 4.4), (4.4, 19.6), (10.6, 19.6)])
    _line(p, s, 9.6, 12.0, 19.6, 12.0)
    _poly(p, s, [(16.4, 8.8), (19.6, 12.0), (16.4, 15.2)])


def _menu(p: QPainter, s: float) -> None:
    for cy in (7.5, 12.0, 16.5):
        _line(p, s, 4.5, cy, 19.5, cy)


def _layers(p: QPainter, s: float) -> None:
    """三层叠片，用于「能力与降级」。与 Web 端 sprite 的造型语言同款。"""
    _poly(p, s, [(12, 3.5), (20.5, 8.0), (12, 12.5), (3.5, 8.0)], close=True)
    _poly(p, s, [(3.5, 12.4), (12, 16.9), (20.5, 12.4)])
    _poly(p, s, [(3.5, 16.4), (12, 20.9), (20.5, 16.4)])


def _shield(p: QPainter, s: float) -> None:
    """盾牌 + 对勾，用于「管理后台」—— 管理动作要让人一眼看出"这是管控面"。"""
    _poly(p, s, [(12, 3.4), (5.2, 5.8), (5.2, 11.4), (12, 20.6),
                 (18.8, 11.4), (18.8, 5.8)], close=True)
    _poly(p, s, [(8.8, 11.6), (11.2, 14.0), (15.4, 9.4)])


def _refresh(p: QPainter, s: float) -> None:
    """环形箭头。弧 + 箭头，用来刷新列表。"""
    path = QPainterPath()
    path.arcMoveTo(QRectF(4.2 * s, 4.2 * s, 15.6 * s, 15.6 * s), 60)
    path.arcTo(QRectF(4.2 * s, 4.2 * s, 15.6 * s, 15.6 * s), 60, 265)
    p.drawPath(path)
    _poly(p, s, [(12.6, 3.2), (15.6, 6.4), (12.0, 8.6)])


DRAWERS: dict[str, Drawer] = {
    "plus": _plus,
    "search": _search,
    "home": _home,
    "scale": _scale,
    "doc": _doc,
    "book": _book,
    "chart": _chart,
    "pen": _pen,
    "chat": _chat,
    "tasks": _tasks,
    "folder": _folder,
    "settings": _settings,
    "send": _send,
    "arrow-up-right": _arrow_up_right,
    "clock": _clock,
    "user": _user,
    "logout": _logout,
    "menu": _menu,
    "layers": _layers,
    "shield": _shield,
    "refresh": _refresh,
}


# ------------------------------------------------------------------ 对外接口
def paint_icon(painter: QPainter, name: str, size: float, color: str,
               stroke: float = DEFAULT_STROKE) -> None:
    """在已有 painter 上画一个图标（左上角对齐当前坐标原点）。"""
    drawer = DRAWERS.get(name)
    if drawer is None:
        # 不静默画个空白：那样界面上只是"少了个图标"，没人知道名字写错了
        raise KeyError(f"没有名为 {name!r} 的图标；可用：{', '.join(sorted(DRAWERS))}")
    s = size / GRID
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(color), max(stroke * s, 0.8))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    drawer(painter, s)
    painter.restore()


def icon_pixmap(name: str, size: int = 18, color: str = "#6b7480",
                stroke: float = DEFAULT_STROKE, ratio: float = 2.0) -> QPixmap:
    """渲染成 ``QPixmap``（供 ``QPushButton.setIcon`` 这类接口使用）。

    按 ``ratio`` 倍分辨率绘制并设 ``devicePixelRatio`` —— 高分屏上不做这一步
    的图标是糊的，而糊在缩小后的界面里看不出来，只有放到 200% 缩放下才明显。
    """
    px = QPixmap(int(size * ratio), int(size * ratio))
    px.setDevicePixelRatio(ratio)
    px.fill(Qt.GlobalColor.transparent)
    painter = QPainter(px)
    try:
        # **异常路径也必须 end()**：名字写错抛 KeyError 时若不关 painter，
        # 一个"仍处于活动状态"的 QPainter 会随异常的 traceback 活着，
        # 下一次再画（尤其是再抛一次）时 Qt 直接 ``abort()`` —— 整个进程
        # 无提示地消失，Python 层什么都捕获不到。实测即如此。
        paint_icon(painter, name, size, color, stroke)
    finally:
        painter.end()
    return px


class Icon(QWidget):
    """一个只负责画图标的轻量控件。

    **鼠标事件穿透**（``WA_TransparentForMouseEvents``）：图标常常被放在
    按钮或卡片的内部，如果它自己吃掉点击，用户点在图标上就会发现"按钮没反应"。
    """

    def __init__(self, name: str, size: int = 18, color: str = "#6b7480",
                 stroke: float = DEFAULT_STROKE, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._name = name
        self._color = QColor(color)
        self._stroke = stroke
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    # 便于按状态改色（如侧栏项选中）——只改颜色不该重建控件
    def setColor(self, color: str) -> None:  # noqa: N802 - 与 Qt 命名一致
        new = QColor(color)
        if new != self._color:
            self._color = new
            self.update()

    def color(self) -> QColor:
        return QColor(self._color)

    def setName(self, name: str) -> None:  # noqa: N802
        self._name = name
        self.update()

    def paintEvent(self, _event: object) -> None:  # noqa: N802
        painter = QPainter(self)
        paint_icon(painter, self._name, self.width(), self._color.name(),
                   self._stroke)
        painter.end()
