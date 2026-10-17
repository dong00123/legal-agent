"""桌面端界面的静态守卫。

这三类问题**都不会报错**，只会让界面看起来"有点不对"，然后在某次改样式时
被当成玄学：

1. QSS 里写了 ``#someName``，代码里却没有对应的 ``setObjectName`` ——
   样式静默不生效。
2. 代码里写了一个 ``icons.py`` 里不存在的图标名 —— 图标静默不画。
   （``paint_icon`` 会抛 ``KeyError``，但那是运行到那一步才抛，
   而这是个自绘控件，画不到时界面只是"少了个图标"，很难联想到名字写错。）
3. ``theme.py`` 的颜色与 Web 端 light 主题分叉 —— 同一个产品出现两种蓝，
   而且没人知道从哪一天开始不一致的。

因此每个守卫都**必须自检**：喂一组"注定违规"的输入，确认它真的会报，
否则"没扫到"与"没违规"在测试结果里长得一模一样。

⚠️ 2026-09-20 Web 端整体换版，本文件的抓手随之挪了地方（契约没变）：
- **配色**：``web/css/app.css`` 的 ``:root`` → ``web/index.html`` /
  ``web/login.html`` 内联 ``<style>`` 的 ``:root``；变量名也从长名
  （``--accent`` / ``--panel``）换成了短名（``--ac`` / ``--mu``）。
- **导航**：``web/js/main.js`` 的 ``const NAV`` → ``web/index.html`` 里
  ``<nav class="side">`` 的按钮文本。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "client"
WEB = ROOT / "web"
INDEX = WEB / "index.html"
LOGIN = WEB / "login.html"


# ===========================================================================
# 一、QSS 里的 #objectName 必须在代码里真实存在
# ===========================================================================
def _strip_css_comments(css: str) -> str:
    """剥掉 ``/* ... */``。

    **必须先剥再扫。** 否则注释里为了说明"某个选择器曾经存在过"而写的
    ``#sideFootBtn`` 会被当成真的选择器报出来 —— 实测就发生过，
    守卫报了一个并不存在的问题，而真正该修的是这个守卫。
    """
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _colors_stripped(css: str) -> str:
    """去掉颜色字面量，剩下的 ``#name`` 才是选择器。

    颜色是 3/4/6/8 位十六进制（``#1e5aa8``、``#fff``），而 objectName 是
    驼峰或下划线写法 —— 先按"十六进制"这个特征把颜色删掉，可以简单可靠地区分
    `#pageTitle`（选择器）与 `#dfe9f8`（颜色）。
    """
    return re.sub(r"#[0-9a-fA-F]{3,8}\b", "", _strip_css_comments(css))


def _qss_object_names(css: str) -> set[str]:
    """从 QSS 里取出所有 ``#id`` 选择器名（不含注释里的）。"""
    return set(re.findall(r"#([A-Za-z_][A-Za-z0-9_]*)", _colors_stripped(css)))


def _declared_object_names(py_text: str) -> set[str]:
    """从 Python 代码里取出所有 ``setObjectName("x")`` 与 ``objectName="x"``。"""
    found = set(re.findall(r"""setObjectName\(\s*["']([^"']+)["']""", py_text))
    found |= set(re.findall(r"""objectName\s*=\s*["']([^"']+)["']""", py_text))
    return found


def _client_sources() -> dict[Path, str]:
    return {p: p.read_text(encoding="utf-8")
            for p in CLIENT.rglob("*.py")
            if "__pycache__" not in p.parts}


def test_qss_name_detector_actually_detects() -> None:
    """检波器自检：必须能分开"选择器"与"颜色值"，且对注释免疫。"""
    css = """
    QWidget#mainCard { background: #ffffff; border: 1px solid #e2e6ec; }
    QLabel#pageTitle { color: #12305c; }
    """
    assert _qss_object_names(css) == {"mainCard", "pageTitle"}, _qss_object_names(css)
    # 颜色型写法一个都不该被当成选择器
    assert _qss_object_names("x { color: #fff; background: #123a66; }") == set()
    # 注释里提到的名字不算真的用了它 —— 否则守卫会报一个并不存在的问题，
    # 而人会去改注释了事，守卫也就废了
    assert _qss_object_names("/* 曾经有个 #ghostBtn */ x { color: #fff; }") == set()
    assert _qss_object_names("#real /* #fake */ { color: #fff; }") == {"real"}


def test_qss_object_names_all_declared() -> None:
    """theme.py 里每一个 #id 选择器都要有对应的 setObjectName。

    ``setObjectName`` 拼错一个字母，QSS 匹配不到 —— 不报错、不警告，
    界面只是"这块样式没生效"。
    """
    theme_text = (CLIENT / "app" / "theme.py").read_text(encoding="utf-8")
    wanted = _qss_object_names(theme_text)
    assert wanted, "没从 theme.py 里解析出任何 #id 选择器，检查解析逻辑"

    declared: set[str] = set()
    for text in _client_sources().values():
        declared |= _declared_object_names(text)

    missing = sorted(wanted - declared)
    assert not missing, (
        f"QSS 里写了但代码里没有对应 setObjectName 的选择器：{missing}。"
        "这类失配是静默的 —— 样式不生效而已，不会抛错。")


# ===========================================================================
# 二、用到的图标名必须在 DRAWERS 里
# ===========================================================================
ICON_CALL = re.compile(
    r"""(?:Icon|IconTextButton|TaskCard)\(\s*["']([^"']+)["']"""
    r"""|icon_pixmap\(\s*["']([^"']+)["']""")


def _used_icon_names(text: str) -> set[str]:
    out: set[str] = set()
    for match in ICON_CALL.finditer(text):
        name = match.group(1) or match.group(2)
        if name:
            out.add(name)
    return out


def test_icon_detector_actually_detects() -> None:
    """检波器自检：三种调用写法都要抓得到，且不该抓到位置参数以外的东西。"""
    text = """
    Icon("scale", 22, theme.ACCENT)
    IconTextButton("folder", "添加材料", role="tool")
    TaskCard("chat", "咨询", "说明")
    icon_pixmap("plus", 15, "#ffffff")
    other("not-an-icon")
    """
    assert _used_icon_names(text) == {"scale", "folder", "chat", "plus"}, \
        _used_icon_names(text)


def test_icon_names_are_defined() -> None:
    """所有被引用的图标名都必须在 ``icons.DRAWERS`` 里有定义。

    ``icons.py`` 依赖 PySide6（QPainter 自绘），**桌面端是现役的一部分，
    依赖必须装得上** —— 装不上就让这条守卫红，而不是静默跳过：
    跳过等于把"图标名写错"这类静默故障放行。
    """
    sys.path.insert(0, str(ROOT))
    from client.app.icons import DRAWERS  # noqa: PLC0415

    used: set[str] = set()
    for path, text in _client_sources().items():
        if path.name == "icons.py":
            continue          # 定义文件自己不算引用
        used |= _used_icon_names(text)

    # NAV_ITEMS 是数据不是调用，上面的调用型正则抓不到，按元组字面量补扫。
    # **只扫 NAV_ITEMS 那段**：全文件扫会把 setProperty("role", "group")
    # 这类属性元组误当图标名（实测报出 'count' / 'ghost' / 'group'）。
    nav_py = re.search(r"NAV_ITEMS: list\[tuple\[str, str\]\] = \[(.*?)\]",
                       (CLIENT / "app" / "main.py").read_text(encoding="utf-8"), re.S)
    if nav_py:
        used |= set(re.findall(r"""\(\s*"[^"]+"\s*,\s*"([^"]+)"\s*\)""",
                               nav_py.group(1)))

    assert used, "没扫到任何图标引用，检查正则"
    missing = sorted(used - set(DRAWERS))
    assert not missing, (
        f"代码里用了 icons.py 里不存在的图标名：{missing}。"
        f"目前可用的有：{sorted(DRAWERS)}")


# ===========================================================================
# 二·五、桌面端导航必须与 Web 端对齐（界面统一的契约）
# ===========================================================================
# 同一个功能在两端各自的标题。**两端叫法不同是允许的，但必须在这里显式登记**：
# 登记这个动作强迫改动者回答"这算不算同一个功能"，而不是悄悄改名蒙过去
# （若改成"按名字取交集"，改名就成了绕开守卫的捷径）。
_NAV_PAIRS: list[tuple[str, str]] = [
    ("工作台", "工作台"),
    ("AI 助手", "AI 助手"),
    ("合同审查", "合同审查"),
    ("案件管理", "案件管理"),
    ("文书中心", "文书中心"),
    ("日程任务", "日程任务"),
]

# 只在一端存在的导航条目。**必须写明另一端为什么没有** —— 否则"少一项"会被
# 当成疏忽，而下一个接手的人就会去"补齐"，把两端各自的定位差异抹平。
_WEB_ONLY_NAV: list[tuple[str, str]] = [
    ("类案检索", "按案情语义检索裁判文书。桌面端的「法规检索」按法条编号/关键词"
                 "查法规，不是同一个功能，因此不与它配对。"),
    ("证据链", "选诉讼案件 → 传证据材料 → 生成「主张 → 证据 → 缺口」链。"
               "桌面端的案件详情只读（client/panels/cases_panel.py 明写"
               "「上传与解析材料等写操作在 Web 端进行」），没有这一页，因此不与它配对。"),
    ("专家", "按领域配置的 AI 专家（诉讼/合同/劳动/知产…），点击接入助手对话。"
             "桌面端的 NAV_ITEMS 里没有这一页 —— 专家与技能目前只在 Web 端配置。"),
    ("技能", "AI 助手可调用的原子能力清单（启停 / 重新载入）。"
             "理由同上：桌面端没有这一页，只有 Web 端能配置。"),
]
_DESKTOP_ONLY_NAV: list[tuple[str, str]] = [
    ("法规检索", "按法条检索。Web 端目前只有「类案检索」，没有这一页。"),
    ("能力与降级", "运维自述页（哪些子系统在降级、回落到什么）。Web 端只有页面内的"
                   "「AI 能力状态」小卡，没有独立一页。"),
    ("管理后台", "桌面端刻意只做只读速览；建用户、改配置这类写操作留在 Web 端"
                 "（见 panels/admin_panel.py）。"),
]

_SIDE_NAV = re.compile(r'<nav class="side">(.*?)</nav>', re.S)
# 导航按钮：前面的 <svg> 图标和后面的 <span class="bdg"> 角标都不是标题。
_NAV_BUTTON = re.compile(r'<button class="nav[^"]*"[^>]*>.*?</svg>\s*([^<]+)', re.S)
_NAV_ITEMS_BLOCK = re.compile(r"NAV_ITEMS.*?=\s*\[(.*?)\n\]", re.S)


def _web_nav_titles() -> list[str]:
    """从 ``web/index.html`` 的 ``<nav class="side">`` 取导航标题（按侧栏顺序）。"""
    block = _SIDE_NAV.search(INDEX.read_text(encoding="utf-8"))
    assert block, '没在 web/index.html 里找到 <nav class="side">，检查解析逻辑'
    return [t.strip() for t in _NAV_BUTTON.findall(block.group(1))]


def _desktop_nav_titles() -> list[str]:
    """从 ``main.py`` 的 ``NAV_ITEMS`` 取导航标题（按渲染顺序）。"""
    block = _NAV_ITEMS_BLOCK.search((CLIENT / "app" / "main.py").read_text(encoding="utf-8"))
    assert block, "没在 main.py 里找到 NAV_ITEMS，检查解析逻辑"
    return re.findall(r'\(\s*"([^"]+)"\s*,\s*"[^"]+"\s*\)', block.group(1))


def test_nav_parsers_actually_parse() -> None:
    """检波器自检：两个解析器都要真的能取到标题，否则下面的断言会"假绿"。"""
    web = _web_nav_titles()
    desktop = _desktop_nav_titles()
    assert web, f"web 侧没解析出导航项：{web}"
    assert desktop, f"桌面侧没解析出导航项：{desktop}"
    # 角标（<span class="bdg">3</span>）不该混进标题里
    assert not any("<" in t or ">" in t for t in web), f"标题里混进了标签：{web}"
    assert "工作台" in web and "工作台" in desktop


def test_nav_pair_table_covers_both_sides() -> None:
    """对照表必须**不多不少**地覆盖两端的全部导航条目。

    这是上面那张表的维护性守卫：任一端新增 / 改名 / 删项却没同步对照表，
    就会在这里红 —— 而不是等到用户发现"网页上有、客户端里没有"。
    """
    web = _web_nav_titles()
    desktop = _desktop_nav_titles()

    expected_web = [w for w, _ in _NAV_PAIRS] + [t for t, _ in _WEB_ONLY_NAV]
    expected_desktop = [d for _, d in _NAV_PAIRS] + [t for t, _ in _DESKTOP_ONLY_NAV]

    assert sorted(web) == sorted(expected_web), (
        f"Web 端导航与对照表对不上：\n  实际 = {web}\n  表里 = {expected_web}\n"
        "新增/改名/删项时请更新本文件顶部的 _NAV_PAIRS / _WEB_ONLY_NAV。")
    assert sorted(desktop) == sorted(expected_desktop), (
        f"桌面端导航与对照表对不上：\n  实际 = {desktop}\n  表里 = {expected_desktop}\n"
        "新增/改名/删项时请更新本文件顶部的 _NAV_PAIRS / _DESKTOP_ONLY_NAV。")


def test_nav_order_matches_between_clients() -> None:
    """两端**共同条目**的相对顺序必须一致。

    名称按对照表可以不同，但顺序不一致等于让用户记两套位置 ——
    这正是这条守卫存在的理由（分组不参与比对：Web 端侧栏是平铺的，
    没有分组标题，分组只是桌面端的视觉辅助）。
    """
    web = _web_nav_titles()
    desktop = _desktop_nav_titles()

    web_index = {w: i for i, (w, _) in enumerate(_NAV_PAIRS)}
    desktop_index = {d: i for i, (_, d) in enumerate(_NAV_PAIRS)}

    web_seq = [web_index[t] for t in web if t in web_index]
    desktop_seq = [desktop_index[t] for t in desktop if t in desktop_index]

    assert len(web_seq) == len(_NAV_PAIRS), (
        f"有对照表里的条目没在 Web 端出现：web={web}")
    assert len(desktop_seq) == len(_NAV_PAIRS), (
        f"有对照表里的条目没在桌面端出现：desktop={desktop}")

    assert web_seq == desktop_seq, (
        "两端共同导航项的顺序不一致 —— 用户得在两边各记一套位置：\n"
        f"  web     = {[t for t in web if t in web_index]}\n"
        f"  desktop = {[t for t in desktop if t in desktop_index]}")


def test_every_drawer_paints_without_error() -> None:
    """每个图形都要真的能画出来。

    **这条不在 pytest 里跑，交给 ``scripts/qt_smoke.py``。**
    原因很实际：在 pytest 进程里起 Qt，一旦平台插件选错（比如无显示环境里
    没先设 ``QT_QPA_PLATFORM=offscreen`` 就 import 了 PySide6），
    Qt 是直接 ``abort()`` 的 —— Python 层捕获不到，**整个测试套件连同
    另外两百多个测试一起崩掉**。为了验几个图标付这个代价不值。
    静态的名字校验（上面那条）留在 pytest，真渲染留给本来就跑 Qt 的冒烟脚本。
    """
    pytest.skip("Qt 渲染验证见 scripts/qt_smoke.py（避免在 pytest 里起 Qt）")


# ===========================================================================
# 三、桌面端配色不得与 Web 端 light 主题分叉
# ===========================================================================
# theme.py 的常量名 → Web 端内联样式里的 CSS 变量名。
# 只登记**两端都有明确对应**的颜色：Web 端自己没有 hover / pressed / soft /
# brand 这一批派生色（桌面端有），那些不参与比对 —— 否则等于逼着两端
# 编同一套派生规则，而派生色本来就是各自的实现自由。
COLOR_PAIRS = {
    "BG": "--bg",
    "PANEL_2": "--mu",
    "BORDER": "--br",
    "TEXT": "--tp",
    "TEXT_DIM": "--ts",
    "TEXT_FAINT": "--tt",
    "ACCENT": "--ac",
    "OK": "--ps",
    "WARN": "--wn",
    "DANGER": "--dg",
}

_STYLE_BLOCK = re.compile(r"<style[^>]*>(.*?)</style>", re.S)


def _root_vars_from_css(css: str) -> dict[str, str]:
    """取 ``:root { ... }`` 里的变量（只取第一个 :root 块，隔壁是暗色主题）。"""
    block = re.search(r":root\s*\{(.*?)\}", css, re.S)
    assert block, "没找到 :root 块"
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block.group(1)))


def _norm_color(value: str) -> str:
    """把颜色规范成小写 ``#rrggbb``。

    **必须先规范化再比**：``#fff`` 与 ``#ffffff`` 是同一个白色，字符串却不相等。
    实测 Web 端写 ``--bg:#fff``、桌面端写 ``BG = "#ffffff"`` —— 直接比会报一个
    并不存在的"配色分叉"，而真正的分叉（比如 #2f6bff vs #1e5aa8）会被这种噪音淹没。
    """
    v = value.strip().lower()
    m = re.fullmatch(r"#([0-9a-f]{3})", v)
    return "#" + "".join(c * 2 for c in m.group(1)) if m else v


def _web_root_vars() -> dict[str, str]:
    """两页内联样式合并后取 light 主题的变量。"""
    css = "\n".join(_STYLE_BLOCK.findall(INDEX.read_text(encoding="utf-8")))
    css += "\n" + "\n".join(_STYLE_BLOCK.findall(LOGIN.read_text(encoding="utf-8")))
    return _root_vars_from_css(css)


def test_web_var_parser_works() -> None:
    """检波器自检：解析器要能在有暗色主题的情况下只取 light 的那一块。"""
    css = """
    :root { --bg: #fff; --ac: #2f6bff; }
    [data-theme="dark"] { --bg: #171717; }
    """
    parsed = _root_vars_from_css(css)
    assert parsed == {"--bg": "#fff", "--ac": "#2f6bff"}, parsed


def test_color_normalizer_works() -> None:
    """检波器自检：三位简写必须等价于六位，但不同颜色不能被判成相同。"""
    assert _norm_color("#fff") == _norm_color("#ffffff") == "#ffffff"
    assert _norm_color(" #FFF ") == "#ffffff"
    assert _norm_color("#2f6bff") == "#2f6bff"
    assert _norm_color("#fff") != _norm_color("#fffffe")


def test_desktop_colors_match_web_light_theme() -> None:
    """桌面端与 Web 端 light 主题必须同色。

    两边是同一个产品。分叉不会报错，只会让"从网页换到客户端"的人觉得
    走错了地方 —— 而这种观感上的不一致，通常要等有人截图对比才会被发现。
    """
    sys.path.insert(0, str(ROOT))
    from client.app import theme  # noqa: PLC0415

    web = _web_root_vars()

    mismatches = []
    for py_name, css_name in COLOR_PAIRS.items():
        theirs = web.get(css_name)
        assert theirs is not None, (
            f"Web 端没有 {css_name} 这个变量（COLOR_PAIRS 里的映射过时了？）")
        mine = getattr(theme, py_name, None)
        if _norm_color(str(mine)) != _norm_color(theirs):
            mismatches.append(f"{py_name}={mine} vs {css_name}={theirs}")
    assert not mismatches, (
        "桌面端与 Web 端配色分叉：" + "；".join(mismatches) +
        "。改一处时另一处要同步（映射表在本文件顶部 COLOR_PAIRS，"
        "对应关系写在 theme.py 的常量注释里）。")
