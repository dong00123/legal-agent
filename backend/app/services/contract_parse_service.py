"""上传材料的**解析执行层**（``kind=contract_parse``）。

## 为什么要有这一层

合同审查页的三处上传入口原先都在请求里**同步**解析：``POST /contract/parse``
把整批文件逐份 OCR / 解析完才返回。一份扫描件走多模态整页转写要几十秒，
而前端 ``web/app.js`` 的 ``req()`` 有 20 秒全局超时兜底 —— 表现就是
「上传两页合同 → 解析失败：signal is aborted without reason」：
文件没问题，只是没人能等到结果。

治本改法是把"解析"本身变成一条队列任务：上传接口只做**受理**
（落盘 + 入队 + 立刻返回 ``task_id``），真正的解析在 worker 里跑，
进度写进 ``la_task``，前端订阅 ``/tasks/{id}/stream``。
于是"要跑三分钟"和"能看见进度"不再互相排斥。

## 三条刻意的设计

**一、正文落盘，任务表只留轻量元信息。**
``la_task.result`` 是给接口读的 JSON 列，塞进几万字的合同正文会让每一次
``GET /tasks`` 都拖着它走（更早的版本就是被这个撑爆的：正文被截断，
界面上出现半截合同）。所以每份材料的正文写进 ``PROCESSED_DIR`` 下的
``<task_id>/text/*.txt``，任务表里只留 ``{name, ok, chars, error}`` ——
谁要看正文谁去 ``GET /contract/parse/{task_id}``，那条接口一次读全。
``manifest.json`` 是这份正文的**目录**：哪份材料对应哪个文件、多少字、
是不是缓存命中的。

**二、同一份文件第二次解析必然命中缓存（幂等）。**
worker 超时（``WORKER_TASK_TIMEOUT``）会把任务放回队列重跑，而那一次重跑会
**从头再来一遍**：已经花了两分钟的 9 份材料会再花两分钟，且多模态识别本身
有随机性，重跑还会改动已经落盘的正文。所以解析结果按**文件内容哈希**缓存
（``_text_cache/<sha256>.txt``）：第一次跑完的正文在第二次跑时直接读缓存，
重跑只补那些**还没跑完**的文件。"超时重跑是安全的"这句话靠的就是这里。

> 与 ``parser/vision/client.py`` 的内容哈希缓存不重复：那层缓存的是
> **单次模型调用**（图片转写），而这里缓存的是**整份材料的最终正文** ——
> PDF / Word 那条走 ``parse_document`` 的路径没有任何缓存，
> 一份 30M 的 Word 重跑一次就是几十秒。

**三、逐份上报，不攒到最后。**
进度必须能回答"现在到哪儿了"，所以每解析完一份就往 ``la_task`` 写一次
（``progress`` + 一句 ``note`` 落进 ``trace``，另有 ``state.parse`` 给出
已解析/总数）。前端那条进度条的移动与"2/3 已解析：xx.pdf（813 字）"
是同一份数据。

--------------------------------------------------------------------------
调用关系：``api/v1/chat.py`` 负责受理与入队（``stage_uploads`` +
``parse_snapshot``），``worker/app.py`` 负责执行（``run_job``），
``api/v1/chat.py`` 的 ``GET /contract/parse/{task_id}`` 负责读结果
（``result_payload``）。**API 进程里不要调 ``run_job``** —— 它会跑几分钟，
那正是这次改造要挪走的东西。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.app.config import settings
from backend.app.services.task_service import task_service

logger = logging.getLogger("legal_agent.contract_parse")

#: 任务类型。**只此一处定义** —— API 入队、worker 分流、日后任务中心筛选都引它，
#: 各写一遍字符串字面量迟早会出现"worker 认不出自己入的队"。
KIND = "contract_parse"

_ROOT_NAME = "contract_parse"
_CACHE_NAME = "_text_cache"
_MANIFEST_NAME = "manifest.json"

#: 单份材料的解析路线。**存进快照**（而不是执行时按后缀现算）：
#: 快照是唯一的真相，重跑时不该依赖"文件后缀有没有被改过"。
_ROUTE_IMAGE = "vision"
_ROUTE_DOC = "document"


# --------------------------------------------------------------------------- 路径
def _root() -> Path:
    """解析产物根目录：``<PROCESSED_DIR>/contract_parse``。

    挂在 ``PROCESSED_DIR`` 下而不是 ``TEMP``：这些正文是**结果**（律师关掉页面
    再回来还要看），而系统临时目录会被清理进程扫掉。
    （``PROCESSED_DIR`` 已由 ``settings.ensure_dirs()`` 建好。）
    """
    return Path(settings.PROCESSED_DIR) / _ROOT_NAME


def task_dir(task_id: str) -> Path:
    return _root() / str(task_id)


def source_dir(task_id: str) -> Path:
    """上传材料的落盘位置。入队前必须落盘 —— 入队之后请求对象就没了。"""
    return task_dir(task_id) / "source"


def text_dir(task_id: str) -> Path:
    """解析出的正文。一份材料一个 ``NN.txt``（``NN`` 是受理时的序号）。"""
    return task_dir(task_id) / "text"


def manifest_path(task_id: str) -> Path:
    return task_dir(task_id) / _MANIFEST_NAME


def _cache_path(digest: str) -> Path:
    return _root() / _CACHE_NAME / f"{digest}.txt"


# --------------------------------------------------------------------------- 口径
def max_parse_bytes() -> int:
    """单份材料的大小上限（字节）。与旧同步接口同一口径（``MAX_PARSE_MB``）。"""
    limit = int(getattr(settings, "MAX_PARSE_MB", 50) or 50)
    return limit * 1024 * 1024


def max_parse_mb() -> int:
    return int(getattr(settings, "MAX_PARSE_MB", 50) or 50)


def suffix_tables() -> tuple[set[str], set[str]]:
    """``(可解析后缀, 图片后缀)``。**白名单只有一份，在 ``chat.py`` 里**
    （它写着"与前端 accept 对齐"，改动时是要一起动的）。

    这里**延迟导入**而不是复制一份：复制出来的那份一定会和前端、和
    ``_PARSE_SUFFIXES`` 三处分叉，而分叉的表现是"界面上能选、后端说不支持"。
    """
    from backend.app.api.v1.chat import _IMAGE_SUFFIXES, _PARSE_SUFFIXES

    return set(_PARSE_SUFFIXES), set(_IMAGE_SUFFIXES)


def count_chars(text: str) -> int:
    """正文字数口径（不计空白 / 不计占位符）。同样**只有一份实现**，见
    ``chat.count_chars`` 的长注释 —— 那里解释了为什么不能退回 ``len()``。"""
    from backend.app.api.v1.chat import count_chars as _impl

    return _impl(text)


def _now() -> str:
    """本地时间戳（秒级）。**不要用 ``%Y年`` 之类的中文格式串** ——
    在 Windows 上 ``strftime`` 遇中文格式会抛 ``UnicodeEncodeError``。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- 受理
def _safe_name(name: str, index: int) -> str:
    """把客户端给的文件名收敛成一个安全的文件名。

    ``UploadFile.filename`` 是**客户端给的字符串**，可能是
    ``..\\..\\x.pdf``（目录穿越）或带 ``<>:"|?*``（Windows 非法字符）。
    这里只取最后一段、滤掉控制字符与非法字符；落盘时还会再拼一个序号前缀 ——
    同名两份材料（``1.jpg`` + ``1.jpg``）因此能同时存在，不会互相覆盖。
    """
    base = str(name or "").replace("\\", "/").split("/")[-1].strip()
    base = "".join(ch for ch in base if ch.isprintable() and ch not in '<>:"|?*')
    base = base.strip(". ") or f"material_{index}"
    return base[:120]


def stage_uploads(task_id: str, items: list[tuple[str, bytes]]) -> list[dict[str, Any]]:
    """把上传内容落成"清单 + 源文件"，返回逐份条目（含拒绝原因）。

    ``items`` 是 ``[(文件名, 原始字节)]`` —— 字节由 API 层读好，因为只有它
    知道 ``UploadFile.read(n)`` 的上限该怎么给。这里**只做校验与落盘**。

    逐份失败**不整批 4xx**：与旧同步实现同一条纪律 —— 一份坏文件不该让同批的
    好文件白传。拒绝原因写进条目的 ``error``，受理结果直接回给前端
    （用户当场就知道哪份没被收下，不必等任务跑完）。
    """
    suffixes, images = suffix_tables()
    limit = max_parse_bytes()
    directory = source_dir(task_id)
    directory.mkdir(parents=True, exist_ok=True)

    out: list[dict[str, Any]] = []
    for index, (name, data) in enumerate(items):
        name = str(name or "未命名")
        suffix = Path(name).suffix.lower()
        entry: dict[str, Any] = {
            "index": index, "name": name, "suffix": suffix,
            "size": len(data or b""), "stored": False, "src": "",
            "sha256": "", "route": "", "error": "",
        }
        try:
            if suffix not in suffixes:
                raise ValueError(f"不支持的文件类型：{suffix or '未知'}")
            if not data:
                raise ValueError("文件内容为空")
            if len(data) > limit:
                raise ValueError(f"文件超过 {max_parse_mb()}M 上限，请压缩或拆分后再上传")
            target = directory / f"{index:02d}__{_safe_name(name, index)}"
            # ``write_bytes`` 而不是 ``shutil.move``：上传内容来自内存，
            # 而且这一步必须**落定**（入队之后没人能再补写）。
            target.write_bytes(data)
            entry.update({
                "stored": True,
                "src": str(target),
                "sha256": hashlib.sha256(data).hexdigest(),
                "route": _ROUTE_IMAGE if suffix in images else _ROUTE_DOC,
            })
        except Exception as exc:  # noqa: BLE001 - 单份失败不拖垮整批
            entry["error"] = str(exc)
            logger.warning("材料受理失败 %s: %s", name, exc)
        out.append(entry)
    return out


def parse_snapshot(task_id: str, entries: list[dict[str, Any]], *,
                   user_id: int | None = None, team_id: int | None = None,
                   client: str = "web") -> dict[str, Any]:
    """组装 worker 重建这次解析所需的**全部**东西（``task_queue.enqueue`` 的第一参）。

    **不是** ``RunRequest``：解析任务没有意图、没有材料权限、不跑任何节点。
    硬塞进 ``RunRequest`` 会让编排层多出一串"如果这是解析任务就跳过某几步"
    的分支，而那些分支迟早会漏（worker 那边同样按 ``kind`` 分流，见
    ``worker/app.py`` 的 ``run_one``）。

    ``entries[].src`` 是**服务端绝对路径**，与 ``material_paths`` 同类：
    落库留存（重跑要用）但**不出接口** —— ``task_service._to_dict`` 会把整个
    ``run_request`` 从 ``state`` 里剥掉。
    """
    return {
        "kind": KIND,
        "task_id": task_id,
        "user_id": user_id,
        "team_id": team_id,
        "client": client,
        "created_at": _now(),
        "entries": entries,
    }


# --------------------------------------------------------------------------- 执行
async def _text_of(entry: dict[str, Any]) -> tuple[str, bool]:
    """取一份材料的正文，返回 ``(正文, 是否命中缓存)``。**命中缓存就不再解析。**

    缓存键是**文件内容哈希**（不是文件名/路径）：重跑时同一份文件当然路径相同，
    但真正要防的是"同一份合同换个名字再传一次"—— 那种情况下路径不同、
    内容相同，该命中的仍然要命中。
    """
    digest = str(entry.get("sha256") or "")
    src = Path(str(entry.get("src") or ""))
    if not digest or not src.is_file():
        raise ValueError("材料源文件已丢失（临时目录被清理？），请重新上传")

    cached = _cache_path(digest)
    if cached.is_file():
        try:
            # **空正文也算命中**："这张图确实没有文字"同样是一个确定的结果，
            # 不缓存它就会被反复重跑（而每次重跑都会真的再调一次模型）。
            return cached.read_text(encoding="utf-8"), True
        except OSError as exc:  # noqa: BLE001
            logger.warning("读解析缓存失败，改为重新解析：%s | %s", digest[:12], exc)

    if str(entry.get("route")) == _ROUTE_IMAGE:
        # 图片：多模态**整页转写**（不是"描述图片"）—— 合同审查要靠逐条正文定位风险。
        # 走 ``kind_override="document_scan"``，否则 ``1.1.jpg`` 这种文件名匹配不到
        # 关键词，会落到 generic prompt 只给一句概括（这条坑见旧同步实现的注释）。
        from backend.parser.vision.client import vision_client

        vr = await vision_client.describe_image(
            str(src), hint=str(entry.get("name") or ""), kind_override="document_scan")
        text = (vr.semantic or "").strip()
        if not text:
            raise ValueError(vr.error or "图片未识别出文字（多模态服务不可用）")
    else:
        from backend.parser.pipeline import parse_document

        doc = await parse_document(src)
        # ``doc.text`` 才是**回插了图片语义**的成品；``raw_text`` 是回插前的原始文本。
        text = (getattr(doc, "text", "") or getattr(doc, "raw_text", "") or "").strip()

    write_text_file(cached, text)
    return text, False


async def run_job(task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """解析全部材料并收尾落库。**``async`` 且必须被 ``await``** ——
    两条解析链路（``describe_image`` / ``parse_document``）都是协程，
    在同步函数里调它们只会拿到一个没被 await 的协程对象，
    表现是"解析完成、零字、零报错"。

    **worker 侧唯一的调用点**（``backend/worker/app.py``）。返回一份轻量摘要
    （``{status, total, ok_count, merged_chars, elapsed_ms}``，**不含正文**），
    worker 只用它打日志。任务表里的最终结果由本函数自己写。

    状态口径沿用任务表的既有枚举：
    - 全部成功 → ``succeeded``
    - 部分成功 → ``degraded``（有几份读不出字，但别的还能审 —— 这必须**如实**
      暴露成降级，不能因为"至少有一份成功"就报成功）
    - 一份都没成 → ``failed``

    单份失败**不抛异常**：抛出去会被 worker 当成"环境问题"放回队列重跑，
    而"这份 PDF 是加密的"重跑一百次也是一样的结果。
    """
    entries = [e for e in (payload.get("entries") or []) if isinstance(e, dict)]
    # 只把**真正入库**的材料算进"要解析的份数"：受理时就被拒收的（比如 .exe）
    # 已经在受理响应里回过用户了（``rejected`` 列表）。若还计进 total，
    # "2 收 1 拒"会被判成 ``degraded``（部分材料读不出字），
    # 把"我拒了个不该收的"错报成"解析降级" —— 律师看到降级会去查文件，
    # 而真正的原因在受理阶段就已经说清楚了。
    entries = [e for e in entries if e.get("stored")]
    total = len(entries)
    started = time.monotonic()
    text_root = text_dir(task_id)
    text_root.mkdir(parents=True, exist_ok=True)

    files: list[dict[str, Any]] = []
    _report(task_id, 3, f"开始解析 {total} 份材料", 0, total)

    for i, entry in enumerate(entries):
        name = str(entry.get("name") or f"第 {i + 1} 份")
        item: dict[str, Any] = {"name": name, "ok": False, "chars": 0,
                                "error": "", "file": "", "cached": False}
        if not entry.get("stored"):
            item["error"] = str(entry.get("error") or "文件未成功入库")
        else:
            try:
                text, cached = await _text_of(entry)
            except Exception as exc:  # noqa: BLE001 - 单份失败不拖垮整批
                logger.warning("材料解析失败 %s: %s", name, exc)
                item["error"] = str(exc)
            else:
                item["cached"] = cached
                if text:
                    fname = f"{int(entry.get('index', i)):02d}.txt"
                    write_text_file(text_root / fname, text)
                    item.update({"ok": True, "chars": count_chars(text), "file": fname})
                else:
                    item["error"] = "未从文件中识别出文字"
        files.append(item)

        done = i + 1
        note = f"{done}/{total} 已解析：{name}"
        if item["ok"]:
            note += f"（{item['chars']} 字" + ("，缓存命中）" if item["cached"] else "）")
        elif item["error"]:
            note += f"（失败：{item['error']}）"
        # 进度留 7% 给收尾：解析完最后一份 ≠ 任务完成（还要落清单、写结果）。
        _report(task_id, 3 + int(90 * done / max(1, total)), note, done, total, files)

    ok_count = len([f for f in files if f["ok"]])
    merged_chars = sum(int(f["chars"]) for f in files)
    if total and ok_count == total:
        status = "succeeded"
    elif ok_count:
        status = "degraded"
    else:
        status = "failed"
    elapsed_ms = int((time.monotonic() - started) * 1000)

    note = f"解析结束：{ok_count}/{total} 份成功，共 {merged_chars} 字"
    manifest = {
        "task_id": task_id, "kind": KIND, "status": status,
        "finished_at": _now(), "elapsed_ms": elapsed_ms,
        "total": total, "ok_count": ok_count, "merged_chars": merged_chars,
        # ⛔ 正文**不进这里**（更不进 ``la_task.result``）：这份清单是目录，
        #    正文在同目录的 ``text/*.txt``，由 ``result_payload`` 现读现拼。
        "files": files, "note": note,
    }
    write_text_file(manifest_path(task_id),
                    json.dumps(manifest, ensure_ascii=False, indent=2))
    logger.info("材料解析完成 %s：%s（%d/%d 份，%d 字，%.1f 秒）",
                task_id, status, ok_count, total, merged_chars, elapsed_ms / 1000)

    task_service.finish(
        task_id, status=status,
        state={
            "trace": [{"node": KIND, "detail": note, "at": _now()}],
            # 轻量元信息：前端画进度条与"哪几份没成"只用这些。
            "parse": {"note": note, "done": total, "total": total,
                      "ok_count": ok_count, "files": files},
        },
        result={"kind": KIND, "status": status, "total": total,
                "ok_count": ok_count, "chars": merged_chars, "elapsed_ms": elapsed_ms,
                "files": files, "note": note},
        elapsed_ms=elapsed_ms,
    )
    return {"status": status, "total": total, "ok_count": ok_count,
            "merged_chars": merged_chars, "elapsed_ms": elapsed_ms}


def _report(task_id: str, progress: int, note: str, done: int = 0, total: int = 0,
            files: list[dict[str, Any]] | None = None) -> None:
    """写一次进度：``la_task.progress`` + 一句 ``note`` 进 ``trace``。

    ``note`` 进 ``trace`` 而不是只写 ``error`` 列：``/tasks/{id}/stream`` 的
    progress 帧唯一带出来的过程信息就是 ``trace[-1].detail``，
    前端那句"2/3 已解析：..."就是从这儿读的。
    """
    state: dict[str, Any] = {"trace": [{"node": KIND, "detail": note, "at": _now()}]}
    if files is not None:
        state["parse"] = {"note": note, "done": done, "total": total, "files": files}
    try:
        task_service.save_snapshot(task_id=task_id, status="running", state=state,
                                   progress=max(0, min(95, int(progress))))
    except Exception as exc:  # noqa: BLE001
        # 进度写不进去**不该让解析停**：正文照样落盘，结果照样能取。
        logger.warning("解析进度上报失败：%s | %s", task_id, exc)


def write_text_file(path: Path, text: str) -> None:
    """原子写文本：临时文件 + ``replace``。

    **不要直接 ``write_text`` 到目标路径**：同一个任务被两个 worker 同时认领的
    窗口很窄但存在（见 ``task_queue.requeue_stale`` 的注释），
    直接写会让读方拿到半截文件；``replace`` 是原子的，读方看到的要么是旧的
    完整内容、要么是新的完整内容。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- 结果
def result_payload(task_id: str) -> dict[str, Any] | None:
    """把清单 + 正文拼成"和旧同步接口同形"的一份结果；清单不存在时返回 ``None``。

    **必须现读现拼**：正文不在库里、也不在内存里，它只在磁盘上。
    这也正是 ``la_task.result`` 能保持轻量的原因。

    返回结构与旧 ``POST /contract/parse`` 一致（``files[].text`` / ``merged`` /
    ``merged_chars``）—— 前端那三处调用点的取数逻辑因此不用改，
    只换了"怎么等"（见 ``web/app.js`` 的 ``parseUpload``）。
    """
    path = manifest_path(task_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # noqa: BLE001
        logger.warning("解析清单读取失败：%s | %s", task_id, exc)
        return None

    root = text_dir(task_id)
    files: list[dict[str, Any]] = []
    parts: list[str] = []
    for raw in data.get("files") or []:
        if not isinstance(raw, dict):
            continue
        item: dict[str, Any] = {
            "name": raw.get("name", ""), "ok": bool(raw.get("ok")),
            "chars": int(raw.get("chars") or 0), "error": raw.get("error") or "",
            "cached": bool(raw.get("cached")), "text": "",
        }
        if item["ok"] and raw.get("file"):
            try:
                item["text"] = (root / str(raw["file"])).read_text(encoding="utf-8")
            except OSError as exc:  # noqa: BLE001
                item.update({"ok": False, "error": f"正文文件读取失败：{exc}"})
        if item["ok"] and item["text"]:
            parts.append("【" + str(item["name"]) + "】\n" + item["text"])
        files.append(item)

    merged = "\n\n".join(parts)
    return {
        "task_id": task_id,
        "status": data.get("status") or "",
        "files": files,
        "merged": merged,
        # 与 ``files[].chars`` 同一口径（都不计空白），免得列表里说 900 字、
        # 提示条里说 950 字 —— 这条在旧实现里就踩过。
        "merged_chars": count_chars(merged),
        "total": int(data.get("total") or len(files)),
        "ok_count": int(data.get("ok_count") or 0),
        "elapsed_ms": int(data.get("elapsed_ms") or 0),
        "note": data.get("note") or "",
    }


__all__ = [
    "KIND", "count_chars", "manifest_path", "max_parse_bytes", "max_parse_mb",
    "parse_snapshot", "result_payload", "run_job", "source_dir",
    "stage_uploads", "task_dir", "text_dir", "write_text_file",
]
