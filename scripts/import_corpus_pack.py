"""离线语料更新包导入。

内网/离线部署时没法从网上拉法规，只能把 PDF 打成包带进来。**这一步最容易
出的问题是"导入了但检索不到"**，所以本脚本在真正入库前先做三件事：

1. 列出包里到底有什么（文件名 + 被判成法规还是类案），让你先核对；
2. 只拷受支持的格式，其余**明确报出来**而不是静默忽略 —— 静默忽略的表现是
   "导入报告说 0 失败，但检索什么都查不到"；
3. 入库后回查库里的法规条数与条文数，与导入前对拍。
   只信"过程日志没报错"是不够的：入库失败在多数情况下只体现为条数没涨。

用法（项目根目录）：
    python scripts/import_corpus_pack.py dist/法规更新包_2026Q3.zip
    python scripts/import_corpus_pack.py ./新法规目录 --kind statutes
    python scripts/import_corpus_pack.py pack.tar.gz --dry-run     # 只看不导
    python scripts/import_corpus_pack.py pack.zip --force          # 强制重建

**不删除任何已有文件。** 同名文件会被覆盖（并在报告里列出），
但既有的其他法规不受影响 —— 更新包不该有"删库"的副作用。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.config import settings  # noqa: E402

SUPPORTED = {".pdf", ".docx", ".doc"}
CASE_HINT = "案例"
# 与 ingest.py 里 discover_statute_files 排除的目录同名
TEMPLATE_DIR = "文书模板"


def _classify(path: Path, root: Path) -> str:
    """按文件名与所在目录猜语料类型 —— **与 ingest 的实际行为对齐**。

    这里故意重复 ``backend/corpus/ingest.py`` 的判据而不是去 import 它：
    本脚本要在入库**之前**先给你看清单，而那边的函数是 Discovery 的一部分、带 IO。

    ``文书模板`` 目录要单独认出来：ingest 会把整个目录排除在法规之外
    （它们进的是知识库/模板库）。标成 statute 的话，清单显示"54 个法规"
    而导入后条数一个没涨 —— 这个落差会让人去查检索链路，而原因只是分类显示。
    """
    try:
        rel_parts = path.relative_to(root).parts[:-1]
    except ValueError:
        rel_parts = ()
    if TEMPLATE_DIR in rel_parts:
        return "template"
    if CASE_HINT in path.name:
        return "case"
    return "statute"


def _unpack(source: Path, dest: Path) -> Path:
    """把 zip / tar.gz / 目录统一摊平成一个目录。"""
    if source.is_dir():
        return source
    dest.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as zf:
            zf.extractall(dest)
    elif source.suffix.lower() in (".gz", ".tgz", ".tar") or ".tar." in source.name:
        with tarfile.open(source) as tf:
            tf.extractall(dest)
    else:
        raise SystemExit(f"不支持的包格式：{source.name}（只认 .zip / .tar.gz / 目录）")
    return dest


def _collect(root: Path) -> tuple[list[Path], list[Path]]:
    """返回（受支持的文件, 被忽略的文件）。**忽略的必须报出来。**"""
    supported: list[Path] = []
    ignored: list[Path] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.name.startswith(".") or "__MACOSX" in p.parts:
            continue
        (supported if p.suffix.lower() in SUPPORTED else ignored).append(p)
    return supported, ignored


def _statute_counts() -> tuple[int, int]:
    """回查库里的法规部数与条文数，用于导入前后对拍。"""
    try:
        from sqlalchemy import func, select

        from backend.app.db.models.knowledge import StatuteDoc
        from backend.app.db.session import session_scope

        with session_scope() as db:
            docs = int(db.execute(select(func.count()).select_from(StatuteDoc)).scalar_one() or 0)
            arts = int(db.execute(select(func.sum(StatuteDoc.article_count))).scalar_one() or 0)
        return docs, arts
    except Exception as exc:  # noqa: BLE001
        print(f"  （回查库失败：{type(exc).__name__}: {exc}）")
        return -1, -1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线语料更新包导入")
    parser.add_argument("pack", help="zip / tar.gz / 目录")
    parser.add_argument("--kind", default="all",
                        choices=["all", "statutes", "cases", "knowledge"],
                        help="入库范围（默认 all）")
    parser.add_argument("--force", action="store_true",
                        help="强制重建（先删后写）。法规**换新版**时必须用它，"
                             "否则同名文件会被判为已入库而跳过")
    parser.add_argument("--dry-run", action="store_true", help="只列清单，不拷贝不入库")
    parser.add_argument("--no-ingest", action="store_true", help="只拷贝到 data/raw，不入库")
    args = parser.parse_args(argv)

    source = Path(args.pack).expanduser()
    if not source.exists():
        raise SystemExit(f"找不到：{source}")

    raw_dir = Path(settings.RAW_DIR)
    print(f"语料根目录：{raw_dir}")

    with tempfile.TemporaryDirectory(prefix="corpus_pack_") as tmp:
        staged = _unpack(source, Path(tmp) / "unpacked")
        supported, ignored = _collect(staged)

        print(f"\n包内受支持文件 {len(supported)} 个：")
        by_kind: dict[str, int] = {}
        for p in supported:
            kind = _classify(p, staged)
            by_kind[kind] = by_kind.get(kind, 0) + 1
        for kind, n in sorted(by_kind.items()):
            print(f"  {kind}: {n} 个")
        for p in supported[:20]:
            print(f"    · [{_classify(p, staged)}] {p.name}")
        if len(supported) > 20:
            print(f"    … 还有 {len(supported) - 20} 个")

        if ignored:
            # **必须报。** 静默忽略的表现是"导入成功但检索不到"，
            # 而那时没人会想到"包里的文件根本没被识别"。
            print(f"\n被忽略的非受支持文件 {len(ignored)} 个（不会入库）：")
            for p in ignored[:10]:
                print(f"    · {p.name}  ({p.suffix or '无扩展名'})")
            if len(ignored) > 10:
                print(f"    … 还有 {len(ignored) - 10} 个")

        if not supported:
            print("\n包里没有受支持的法规文件，退出。")
            return 2

        if args.dry_run:
            print("\n--dry-run：未拷贝、未入库。")
            return 0

        # ---- 拷进 data/raw/<包名>/
        pack_name = source.stem if not source.is_dir() else source.name
        target_dir = raw_dir / pack_name
        target_dir.mkdir(parents=True, exist_ok=True)
        replaced: list[str] = []
        for p in supported:
            rel = p.relative_to(staged)
            dst = target_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                replaced.append(rel.as_posix())
            shutil.copy2(p, dst)
        print(f"\n已拷入 {target_dir}（{len(supported)} 个）")
        if replaced:
            print(f"  覆盖了 {len(replaced)} 个同名文件：{'、'.join(replaced[:5])}"
                  + ("…" if len(replaced) > 5 else ""))
            if not args.force:
                print("  **同名文件默认是「已入库就跳过」** —— 若这次是新版本换旧版本，"
                      "请加 --force，否则库里还是旧的。")

        if args.no_ingest:
            print("\n--no-ingest：已跳过入库。")
            return 0

    # ---- 入库。用子进程而不是直接调函数：
    # ingest 会 import 一整套检索链路（embedding 模型很重），
    # 而本脚本所在的进程要保持轻量、且要能用它的退出码当 CI 判据。
    before = _statute_counts()
    cmd = [sys.executable, "-m", "backend.corpus.ingest", f"--{args.kind}"]
    if args.force:
        cmd.append("--force")
    print("\n入库：" + " ".join(cmd))
    proc = subprocess.run(cmd, cwd=str(Path(__file__).resolve().parents[1]))
    if proc.returncode != 0:
        print(f"\n入库进程退出码 {proc.returncode} —— 看上面它的输出。")
        return proc.returncode

    after = _statute_counts()
    if before[0] >= 0 and after[0] >= 0:
        print(f"\n法规部数 {before[0]} → {after[0]}；条文数 {before[1]} → {after[1]}")
        if after[0] == before[0] and after[1] == before[1]:
            print("  **条数没有变化。** 常见原因是同名文件被判为已入库而跳过"
                  "（加 --force 重试），或包里的 PDF 抽不出文本。"
                  "不要只看这一步没报错就认为导入成功了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
