"""打包桌面端为单文件 EXE。

用法（项目根目录）：
    python scripts/build_exe.py               # 正常打包（单文件 GUI）
    python scripts/build_exe.py --console     # 带控制台：EXE 启动闪退时用它能看到报错

**打包前先确认 PySide6 装了**：
    python -c "import PySide6" 否则产物会在启动时直接闪退，
    而 PyInstaller 这一侧**不会报错**（它只按 import 分析，缺包照打）。

为什么单独写这个脚本而不是让人直接敲 pyinstaller 命令：
命令行参数一长串，漏一个（尤其是 ``--noconfirm`` 与 ``--clean``）
就会出现"改了代码但产物没变"这种极难察觉的问题。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "client" / "legal_agent.spec"
DIST = ROOT / "dist"


def _check_deps() -> None:
    missing = []
    for mod in ("PySide6", "PyInstaller"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        raise SystemExit(
            "缺少依赖：" + "、".join(missing) +
            "\n装法：python -m pip install PySide6 pyinstaller"
            "\n（服务端环境不用装，它们只服务于 client/）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打包桌面端 EXE")
    parser.add_argument("--console", action="store_true",
                        help="带控制台窗口（排查启动闪退时用）")
    args = parser.parse_args(argv)

    _check_deps()

    spec = SPEC.read_text(encoding="utf-8")
    if args.console:
        spec = spec.replace("console=False", "console=True")

    # PyInstaller 解析 ``Analysis(["client/entry.py"])`` 用的是 **spec 文件所在
    # 目录** 作基准，不是 cwd。放到 ``client/`` 下它就会去找
    # ``client/client/entry.py``，报错看起来像"路径写错了"。
    #
    # **工作目录每次换新的，且不带 ``--clean``。** 某些沙箱/宿主会把删除
    # 包装成"回收站不可达"并直接抛 OSError —— PyInstaller 清 build 目录时
    # 撞上它，整个打包就中断了（实测报
    # ``SAFE_DELETE_FAIL_CLOSED ... Analysis-00.toc``）。改用带时间戳的新目录，
    # 从根上不需要删除动作；代价是 ``build/`` 会累积，末行会给出清理提示。
    stamp = time.strftime("%Y%m%d_%H%M%S")
    workpath = ROOT / "build" / f"exe_{stamp}"
    tmp_spec = ROOT / f"_legal_agent_build_{stamp}.spec"
    tmp_spec.write_text(spec, encoding="utf-8")

    # **旧产物必须先改名让位。** PyInstaller 在 assemble 阶段会
    # ``os.remove()`` 目标 exe，而某些沙箱/宿主把删除包装成"回收站不可达"
    # 并抛 OSError —— 于是打包跑完全程、在最后写文件那一步崩掉，日志里
    # 只有一句 SAFE_DELETE_FAIL_CLOSED，看着像产物路径写错了。
    # 改名是移动不是删除，不受此限，顺带留下上一版可回滚。
    prev_exe = DIST / "LegalAgent.exe"
    if prev_exe.exists():
        backup = DIST / f"LegalAgent.exe.prev-{stamp}"
        prev_exe.rename(backup)
        print(f"旧产物已改名保留：{backup.name}")

    cmd = [sys.executable, "-m", "PyInstaller", str(tmp_spec),
           "--noconfirm", "--distpath", str(DIST),
           "--workpath", str(workpath)]
    print("打包：" + " ".join(cmd))
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT))
    finally:
        try:
            tmp_spec.unlink(missing_ok=True)
        except OSError as exc:
            # 某些沙箱/宿主会把删除包装成"回收站不可达"并抛异常。
            # **临时 spec 留着不影响产物**，但必须说一声：否则下次看到
            # 根目录多一个 spec，会以为它是被手工维护的。
            print(f"（临时 spec {tmp_spec.name} 没删掉：{exc}；可手动删除）")
    if proc.returncode != 0:
        print(f"\n打包失败（退出码 {proc.returncode}）。看上面的 PyInstaller 输出。")
        return proc.returncode

    exe = DIST / "LegalAgent.exe"
    if exe.exists():
        size_mb = exe.stat().st_size / 1024 / 1024
        print(f"\n产物：{exe}（{size_mb:.1f} MB）")
        print("**必须真双击跑一次才算验证过。** 打包成功 ≠ 能启动：")
        print("  缺 Qt 插件、缺 DLL、入口导入错误，都在启动那一刻才暴露，")
        print("  而 PyInstaller 这一侧一律显示 build completed successfully。")
    else:
        print(f"\n没找到 {exe}，检查 dist/ 目录。")
    # 每跑一次都会在 build/ 下留一个新目录（原因见上面的注释）。
    # 不自动清理是因为删不掉；给出命令让人自己决定什么时候清。
    leftovers = sorted((ROOT / "build").glob("exe_*"))
    if len(leftovers) > 1:
        print(f"\n提示：build/ 下已累积 {len(leftovers)} 个打包工作目录。"
              f"确认产物可用后可清理：")
        print(f'  Remove-Item "{ROOT / "build"}" -Recurse -Force')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
