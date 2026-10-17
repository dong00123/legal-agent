"""**这类问题本机测试抓不到，CI 里没有目标版本也抓不到。**

2026-09-18 的实际事故：``review_node.py`` 里写过

    pool.add(f"case|{re.sub(r'[...]', '', c['case_no'])}")

这在 Python **3.12+** 完全合法（PEP 701 放宽了 f-string 内部的引号与反斜杠限制），
本机 3.13 跑得好好的、测试全绿；但 Docker 镜像用的是 3.11（torch 轮子最全），
于是容器在 **import 阶段**就 ``SyntaxError`` 崩了 —— 连一行日志都来不及打，
现象是"容器起来又退出"。同一个仓库在两个 Python 版本上，一个能跑一个不能。

**为什么必须写成测试而不是"注意一下"。** 写这行的人在本机既不报错、
也没有测试变红；唯一的判据是"目标 Python 版本能不能 parse 它"。
而 CI 里没有 3.11 的话，这条边界就没人守。

实现上用 ``ast`` 而不是 ``tokenize``：**Python 3.12 之后 tokenize 把 f-string
拆成了 FSTRING_START / MIDDLE / END 三个 token**，按 ``tokenize.STRING`` 过滤会
一个都匹配不到（这条守卫第一版就是这么哑掉的，靠 `scanned > 0` 的自检才发现）。
``ast`` 的 ``JoinedStr`` 节点在所有版本上一致，配合 ``get_source_segment``
能拿到 f-string 的原始源码，再按花括号深度判断反斜杠是否落在**表达式里** ——
大括号外的反斜杠（``f"{a}\\n{b}"``）是合法的，不能误报。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# client/ 也扫：EXE 打包用的 Python 版本可能与容器不同，同样的坑照样成立
TARGETS = ("backend", "client", "scripts")
_SKIP_PARTS = {"build", "dist", "__pycache__", "third_party", "node_modules", ".venv"}


def _backslash_in_expression(fstring: str) -> bool:
    """f-string 的**表达式部分**（花括号内）有没有反斜杠。

    按深度数花括号：``{{`` ``}}`` 是字面大括号，深度会先加到 2 再降回来，
    恰好不会把紧随其后的普通文本误判成表达式里面的内容。
    """
    depth = 0
    for ch in fstring:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif ch == "\\" and depth > 0:
            return True
    return False


def _scan(path: Path) -> list[tuple[int, str]]:
    """返回 ``[(行号, f-string 源码)]`` —— 只看表达式里带反斜杠的那些。"""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        seg = ast.get_source_segment(src, node) or ""
        if seg and _backslash_in_expression(seg):
            out.append((node.lineno, seg))
    return out


def _gather() -> tuple[list[str], int]:
    offenders: list[str] = []
    scanned = 0
    for base in TARGETS:
        for path in sorted((ROOT / base).rglob("*.py")):
            if _SKIP_PARTS & set(path.parts):
                continue
            try:
                hits = _scan(path)
            except SyntaxError as exc:  # 3.13 都 parse 不了，别等 3.11 来报
                offenders.append(f"{path.relative_to(ROOT).as_posix()}  本机就 SyntaxError：{exc}")
                continue
            except UnicodeDecodeError:
                continue
            scanned += 1
            for lineno, seg in hits:
                rel = path.relative_to(ROOT).as_posix()
                offenders.append(f"{rel}:{lineno}  {seg[:90]}")
    return offenders, scanned


def test_scanner_sees_fstrings_that_are_fine() -> None:
    """**先自检验波器。** 别把"没扫到任何东西"当成"没有违规"。

    它应当在合法写法（反斜杠在大括号外、字面双大括号）上保持沉默 ——
    一个只会报 OK 的守卫等于没有守卫。
    """
    assert not _backslash_in_expression(r'f"{a}\n{b}"'), "大括号外的反斜杠是合法的，不该报"
    assert not _backslash_in_expression('f"{{字面}} {x}"'), "字面双大括号不该报"
    assert _backslash_in_expression(r"f\"{re.sub(r'[\s]', '', v)}\""), (
        "表达式里的反斜杠必须报出来，否则这条守卫是哑的"
    )


def test_no_backslash_inside_fstring_expression() -> None:
    """f-string 的表达式里不能出现反斜杠 —— Python 3.11 会直接 SyntaxError。"""
    offenders, scanned = _gather()

    assert scanned > 0, "一个 .py 都没扫到 —— 是扫描逻辑坏了，不是代码干净"
    assert not offenders, (
        "以下位置 Python 3.11 会 SyntaxError（3.12+ 才合法，且 Docker 镜像是 3.11）：\n  "
        + "\n  ".join(offenders)
    )
