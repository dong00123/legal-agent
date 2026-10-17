"""桌面端 Qt 界面冒烟（**离屏**）。

验的是「界面真的能构造出来、真的能拿到数据」，而不是「代码能不能 import」。
这两件事差得很远：PyInstaller 打包成功 ≠ EXE 能启动，EXE 能启动 ≠ 侧栏
三组九项、工作台首页、九个页面都不崩。而这一类问题**打包阶段一个都不会报**
—— 它们只在真的构造出 QWidget、真的发一次请求、真的回填列表时才暴露。

用法（项目根目录；需要后端已在跑）：

    python scripts/qt_smoke.py
    python scripts/qt_smoke.py --base http://127.0.0.1:8001
    python scripts/qt_smoke.py --user admin --password admin123

**环境变量 ``QT_QPA_PLATFORM=offscreen`` 必须在 import PySide6 之前设好**，
否则在没有显示器的环境（CI / SSH）里 QApplication 直接起不来。
脚本自己会设，但用 ``--platform`` 可以覆盖（想在真桌面上看一眼就传 ``--platform`` 空值不方便，
直接跑 ``QT_QPA_PLATFORM=windows python scripts/qt_smoke.py`` 即可）。

退出码：``0`` 全通过；``1`` 有未通过项；``2`` 环境不具备（没装 PySide6 或后端不在）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# **必须在 import PySide6 之前。** QApplication 创建时才读这个变量，
# 但 import 期间 Qt 就会去探测平台插件，晚了就救不回来。
DEFAULT_PLATFORM = "offscreen"

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "", fail_hint: str = "") -> bool:
    suffix = f"  [{detail}]" if detail else ""
    if not cond and fail_hint:
        suffix = f"  ← {fail_hint}"
    print(f"  {'OK  ' if cond else 'FAIL'} {label}{suffix}")
    if not cond:
        FAILED.append(label)
    return cond


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="桌面端 Qt 界面离屏冒烟")
    parser.add_argument("--base", default="http://127.0.0.1:8001")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="admin123")
    parser.add_argument("--platform", default=DEFAULT_PLATFORM)
    parser.add_argument("--wait-ms", type=int, default=6000,
                        help="等工作线程回填数据的时长")
    args = parser.parse_args(argv)

    os.environ.setdefault("QT_QPA_PLATFORM", args.platform)

    try:
        import PySide6  # noqa: F401
    except ImportError:
        print("!! 本机没装 PySide6，跳过界面冒烟。装法：")
        print("   .venv/Scripts/python.exe -m pip install --only-binary :all: PySide6")
        return 2

    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from client.app.api_client import ApiClient
    from client.app.main import (
        NAV_GROUPS, NAV_ITEMS, PAGE_ADMIN, PAGE_CAPS, PAGE_CASES, PAGE_CHAT,
        PAGE_HOME, PAGE_SEARCH, LoginDialog, MainWindow,
    )
    from client.app.state import AppState

    print(f"PySide6 {PySide6.__version__}　平台插件 {os.environ['QT_QPA_PLATFORM']}")

    # 后端不在就别硬跑：那样失败的是"网络"而不是"界面"，
    # 而后者才是本脚本要验的东西。
    try:
        import httpx

        httpx.Client(base_url=args.base, timeout=5).get("/health?full=false").raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"!! 后端不在 {args.base}（{type(exc).__name__}），先起后端再跑本脚本")
        return 2

    app = QApplication([])

    print("\n[1/4] 矢量图标")
    from client.app.icons import DRAWERS, icon_pixmap

    bad = [name for name in DRAWERS if icon_pixmap(name, 32, "#1e5aa8").isNull()]
    check("每个图标都能渲染", not bad, f"{len(DRAWERS)} 个" + (f" 失败：{bad}" if bad else ""))
    try:
        icon_pixmap("这个图标不存在", 16)
    except KeyError:
        check("图标名写错会抛错（不静默画空白）", True)
    else:
        check("图标名写错会抛错（不静默画空白）", False,
              fail_hint="名字写错时静默返回空图，界面上只会少一个图标，查不出来")

    print("\n[2/4] 登录框")
    state = AppState()
    dlg = LoginDialog(state)
    check("登录框可构造", dlg is not None)

    print("\n[3/4] 登录后主窗口（真实后端数据）")
    client = ApiClient(args.base, token_provider=state)
    try:
        data = client.login(args.user, args.password)
    except Exception as exc:  # noqa: BLE001
        print(f"!! 登录失败：{type(exc).__name__}: {exc}")
        return 2
    state.user = dict(data.get("user") or {})
    check("登录拿到 token", bool(client.token), f"用户 {state.user.get('username')}")

    win = MainWindow(client, state)
    # **必须跑事件循环**：面板的 refresh() 走工作线程，数据通过信号回填。
    # 不跑循环的话线程还没回来进程就退了，看起来像"面板是空的"。
    loop = QEventLoop()
    QTimer.singleShot(args.wait_ms, loop.quit)
    loop.exec()

    # ---- 外壳 ----
    # 侧栏导航要与 Web 端 NAV 表逐项一致（管理员多一项「管理后台」）。
    # 两端导航不一致时，用户要在两边各记一套位置 —— 那正是本次统一要消灭的。
    expected_nav = [NAV_ITEMS[i][0] for _title, idxs in NAV_GROUPS for i in idxs
                    if not (i == PAGE_ADMIN and not state.is_admin)]
    actual_nav = [b.label.text() for b in win.nav_group.buttons()]
    check("侧栏导航与 Web 端逐项一致", actual_nav == expected_nav,
          f"{actual_nav}" if actual_nav != expected_nav else f"{len(actual_nav)} 项")
    check("内容区九页", win.stack.count() == 9, str(win.stack.count()))

    # ---- 工作台首页 ----
    check("首页渲染出意图卡片", len(win.home._cards) > 0,
          f"{len(win.home._cards)} 张")
    check("卡片标题非空",
          bool(win.home._cards) and all(c.title.text().strip()
                                        for c in win.home._cards))
    # 卡片与下拉必须是同一份数据。分成两处维护的话，界面上会出现
    # "卡片里有、下拉里没有"的意图，而两边都不报错。
    check("意图下拉与卡片同源",
          win.home.intent_combo.count() == len(win.home._cards) + 1,
          f"{win.home.intent_combo.count()} 项 vs 卡片 {len(win.home._cards)} 张")

    if win.home._cards:
        win.home._cards[0].clicked.emit()
        check("点卡片把模板填进输入框",
              bool(win.home.input.toPlainText().strip()),
              win.home.input.toPlainText()[:24])
        # 参考图那行小字："模板填入后仍可自由修改" —— 直接发出去就违背了它
        check("点卡片没有直接发出去",
              "【问】" not in win.chat.log.toPlainText())
        win.home.clear()

    # ---- 侧栏最近任务 ----
    check("最近任务已回填", len(win._recent) > 0, f"{len(win._recent)} 条")
    check("最近任务渲染出控件", win.recent_box.count() > 0,
          f"{win.recent_box.count()} 个")

    # ---- 各功能面板 ----
    tasks = getattr(win.tasks, "_rows", None) or []
    check("任务面板拿到数据", len(tasks) > 0, f"{len(tasks)} 条")
    check("草稿面板没崩", "错" not in str(win.drafts.status.text()),
          win.drafts.status.text())
    print(f"     任务：{win.tasks.status.text()}　草稿：{win.drafts.status.text()}")

    # ---- 新增的四页（法规检索 / 案件管理 / 能力与降级 / 管理后台）----
    # 每页都要"真的发一次请求、真的回填"，只构造不请求是验不出接线错误的。
    def _wait() -> None:
        loop2 = QEventLoop()
        QTimer.singleShot(args.wait_ms, loop2.quit)
        loop2.exec()

    win._switch(PAGE_SEARCH)
    win.search_panel.query.setText("劳动合同")
    win.search_panel.topk.setCurrentIndex(0)     # 前 3 条，快点
    win.search_panel.search()
    _wait()
    check("法规检索出结果", "检索失败" not in win.search_panel.status.text()
          and bool(win.search_panel.result.toPlainText().strip()),
          win.search_panel.status.text())

    win._switch(PAGE_CASES)
    _wait()
    check("案件面板没崩", "加载失败" not in win.cases.status.text(),
          win.cases.status.text())
    print(f"     案件：{win.cases.status.text()}")

    win._switch(PAGE_CAPS)
    _wait()
    check("能力与降级读了健康", win.caps.status.text() == "就绪"
          and bool(win.caps.banners.toPlainText().strip()),
          win.caps.status.text())
    check("后端矩阵有行", win.caps.matrix.rowCount() > 0,
          f"{win.caps.matrix.rowCount()} 行")

    if state.is_admin:
        win._switch(PAGE_ADMIN)
        check("管理后台三个 tab", win.admin.tabs is not None
              and win.admin.tabs.count() == 3,
              str(win.admin.tabs.count() if win.admin.tabs else 0))
        win.admin.refresh_users()
        _wait()
        check("用户名册拿到数据", "加载失败" not in win.admin.user_status.text()
              and win.admin.user_table.rowCount() > 0,
              win.admin.user_status.text())
        print(f"     用户：{win.admin.user_status.text()}")

    # ---- 切页 ----
    win._switch(PAGE_CHAT)
    check("切到智能问答", win.stack.currentIndex() == PAGE_CHAT
          and win.page_title.text() == "智能问答",
          f"{win.page_title.text()} / {win.stack.currentIndex()}")
    win._switch(PAGE_HOME)
    check("切回工作台", win.stack.currentIndex() == PAGE_HOME)

    print("\n[4/4] 退出后凭证与内存一起清空")
    win.close()
    # **凭证只有一份**（client 从 state 取），清一份没清另一份的表现是：
    # 界面回到了登录框，但客户端仍握着旧 token，下次请求照样发得出去。
    check("token 已清空", client.token == "", repr(client.token))
    check("内存用户已清空", not state.user, str(state.user))

    print()
    if FAILED:
        print(f"❌ 未通过 {len(FAILED)} 项：{', '.join(FAILED)}")
        return 1
    print("✅ 桌面端界面冒烟通过（构造 → 真实数据 → 退出清凭证）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
