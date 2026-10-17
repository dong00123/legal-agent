"""人工抽查闭环（合同审查）：待抽查队列 + 抽查结论 + 回灌自我改进。

双模型复核（初审 + 复审）是**纯 AI** 的；人工抽查是"人"在里面的那一环：
- ``review_node._maybe_spot_check`` 按比例命中后，把任务写进这里；
- 律师在「待人工抽查」队列里逐条核，点「通过 / 有误」；
- 结论除记录外，还作为一条**人工判定样本**写进 ``second_pass.json`` 的
  ``examples``，下一轮复审提示词会把它带进去 —— 人工判定比两个 AI 之间的分歧
  更权威，这正是自我改进里此前缺的那一环。

持久化用 JSON 文件（与 second_pass.json 同目录），不引入新表：抽查是低频增强项，
文件读写足够、且方便测试与迁移。写失败只记日志，绝不让"记抽查"拖垮审查。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.app.config import settings

logger = logging.getLogger("legal_agent.services.spot_check")


def _queue_path() -> Path:
    return Path(settings.REVIEW_SPOT_CHECK_QUEUE)


def _second_pass_path() -> Path:
    return Path(settings.REVIEW_SECOND_PASS_STATE)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        blob = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        return blob if isinstance(blob, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _write_json(path: Path, blob: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("抽查/自我改进落盘失败：%s", exc)


def record_pending(task_id: str, query: str, risk_count: int, case_id: str = "") -> None:
    """把一份命中抽查的审查任务写进待抽查队列。幂等：同 task_id 不重复入队。"""
    if not task_id:
        return
    path = _queue_path()
    blob = _read_json(path)
    items = list(blob.get("items") or [])
    for it in items:
        if isinstance(it, dict) and it.get("task_id") == task_id:
            return
    # 新的插在最前：律师先看到最新要核的（不依赖时间戳排序 —— 同秒内连抽多条时
    # created_at 相同，靠时间排会把"后抽的"排到下面去）。
    items.insert(0, {
        "task_id": task_id,
        "case_id": case_id or "",
        "query": (query or "")[:80],
        "risk_count": int(risk_count or 0),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "status": "pending",
        "outcome": "",
        "note": "",
    })
    blob["items"] = items[:100]
    _write_json(path, blob)


def list_queue() -> dict[str, Any]:
    """读待抽查队列，返回 ``{"items": [...], "pending": N}``。"""
    blob = _read_json(_queue_path())
    items = [it for it in (blob.get("items") or []) if isinstance(it, dict)]
    pending = sum(1 for it in items if it.get("status") == "pending")
    # 只按状态稳定排（pending 0 / resolved 1）：插入顺序本身就是"新的在前"（见
    # record_pending），稳定排序把它原样保留 —— 不再依赖可能撞车的 created_at。
    items.sort(key=lambda it: 0 if it.get("status") == "pending" else 1)
    return {"items": items, "pending": pending}


def resolve(task_id: str, outcome: str, note: str = "") -> bool:
    """记录一份抽查结论（``outcome`` ∈ ``ok`` / ``wrong``），并回灌自我改进。

    返回是否找到并处理了该任务。找不到（task_id 不存在）返回 False，不抛。
    """
    if outcome not in ("ok", "wrong"):
        return False
    path = _queue_path()
    blob = _read_json(path)
    items = list(blob.get("items") or [])
    hit = False
    for it in items:
        if isinstance(it, dict) and it.get("task_id") == task_id:
            it["status"] = "resolved"
            it["outcome"] = outcome
            it["note"] = (note or "")[:200]
            it["resolved_at"] = datetime.now().isoformat(timespec="seconds")
            hit = True
    if not hit:
        return False
    blob["items"] = items
    _write_json(path, blob)

    # 回灌自我改进：人工判定作为一条样本写进 second_pass.json 的 examples。
    # 只写「有误」—— 那才是能校准复审模型"哪里会漏/哪里会错"的信号；
    # 「确认」不携带纠错信息，写进去只会稀释样本。
    if outcome == "wrong":
        _append_human_example(note)
    return True


def _append_human_example(note: str) -> None:
    """把一条人工「驳回」样本追加进复审自我改进文件，供下一轮提示词回灌。"""
    path = _second_pass_path()
    blob = _read_json(path)
    kept = list(blob.get("examples") or [])
    kept.append({
        "risk": (note or "")[:60],
        "level": "",
        "action": "人工·驳回",
        "reason": (note or "人工判定审查有误")[:120],
    })
    blob["examples"] = kept[-20:]
    _write_json(path, blob)
