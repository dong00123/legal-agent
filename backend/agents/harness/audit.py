"""Harness Audit：全链路审计留痕（**只增不改不删**）。

方案第十四章要求「材料访问全链路审计日志，不可删改；Agent 每次检索/引用可追溯」。

落盘策略是**双写**，且顺序很重要：
1. 先写**文件**（``data/audit/audit-YYYY-MM-DD.jsonl``，追加模式）——
   这是持久化真相来源。文件写失败才认为审计失败；
2. 再写**MySQL**（``la_audit_log``）——便于管理后台按案件/用户/时间查询。
   DB 失败只记警告，不抛异常：**律所内网 MySQL 抖动不能导致业务中断，
   但审计记录必须已经落在文件里**。

因此本模块不提供 delete/update 接口 —— 不存在"删审计"的代码路径。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.app.config import settings

logger = logging.getLogger("legal_agent.harness.audit")

_write_lock = threading.Lock()


class AuditWriter:
    """审计写入器。"""

    def __init__(self, audit_dir: str | None = None) -> None:
        self.audit_dir = Path(audit_dir or settings.AUDIT_LOG_DIR)
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        self.pending_db: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ 路径
    def _file(self) -> Path:
        return self.audit_dir / f"audit-{datetime.now():%Y-%m-%d}.jsonl"

    # ------------------------------------------------------------------ 写入
    def record(
        self,
        action: str,
        *,
        trace_id: str = "",
        task_id: str = "",
        case_id: str = "",
        user_id: int | None = None,
        username: str = "",
        team_id: int | None = None,
        resource: str = "",
        resource_id: str = "",
        detail: str = "",
        client: str = "api",
        ip: str = "",
        result: str = "ok",
        extra: dict[str, Any] | None = None,
        write_db: bool = True,
    ) -> dict[str, Any]:
        """记录一条审计。返回落盘后的记录体。"""
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "trace_id": trace_id,
            "task_id": task_id,
            "case_id": case_id,
            "user_id": user_id,
            "username": username,
            "team_id": team_id,
            "action": action,
            "resource": resource,
            "resource_id": resource_id,
            "detail": detail[:2000],
            "client": client,
            "ip": ip,
            "result": result,
            "extra": extra or {},
        }

        # ---- 第一跳：文件（持久化真相来源）
        ok = self._write_file(entry)
        if not ok:
            logger.error("审计落文件失败，该记录仅存在于内存：%s %s", action, resource_id)

        # ---- 第二跳：DB（可查询副本，失败不影响业务）
        if write_db:
            self._write_db(entry)

        return entry

    def _write_file(self, entry: dict[str, Any]) -> bool:
        try:
            line = json.dumps(entry, ensure_ascii=False)
            # 进程内锁 + 追加模式（"a"），跨进程由 OS 的 O_APPEND 保证原子追加
            with _write_lock:
                with open(self._file(), "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("审计文件写入异常：%s", exc)
            return False

    def _write_db(self, entry: dict[str, Any]) -> None:
        try:
            from backend.app.db.models import AuditLog
            from backend.app.db.session import session_scope

            with session_scope() as db:
                db.add(
                    AuditLog(
                        trace_id=entry["trace_id"],
                        task_id=entry["task_id"],
                        case_id=entry["case_id"],
                        user_id=entry["user_id"],
                        username=entry["username"],
                        team_id=entry["team_id"],
                        action=entry["action"],
                        resource=entry["resource"],
                        resource_id=entry["resource_id"],
                        detail=entry["detail"],
                        client=entry["client"],
                        ip=entry["ip"],
                        result=entry["result"],
                        extra=entry["extra"],
                    )
                )
        except Exception as exc:  # noqa: BLE001 - 审计降级：文件已写，DB 失败不中断业务
            logger.warning("审计写库失败（文件已记录，不影响业务）：%s", exc)

    # ------------------------------------------------------------------ 便捷方法
    def material_access(self, *, case_id: str, material: str, action: str = "material.read",
                        resource_id: str = "", **kw: Any) -> dict[str, Any]:
        """材料访问留痕：谁、何时、访问了什么材料。

        ``resource_id`` 是**可覆盖的**：不传时用材料名（列表/详情这类"按名字访问"的场景
        没有单条 id），上传时才传材料 id。这里不能把 ``resource_id`` 写死 --
        调用方一旦也传了这个关键字，Python 会抛
        ``record() got multiple values for keyword argument 'resource_id'``，
        而报错点是本方法内部那一行，看起来像审计模块自己坏了。
        """
        return self.record(action, case_id=case_id, resource="case_material",
                           resource_id=resource_id or material, **kw)

    def agent_retrieval(self, *, agent: str, tool: str, query: str, hits: int, **kw: Any) -> dict[str, Any]:
        """Agent 检索留痕：哪个 Agent 用哪个工具、检索什么、命中几条。"""
        return self.record("agent.retrieval", resource=tool, resource_id=agent,
                           detail=f"query={query[:200]} hits={hits}", **kw)

    def citation_final(self, *, passed: bool, checked: int, rejected: list[str], **kw: Any) -> dict[str, Any]:
        """引用终审留痕：校验通过与否，被拦截的引用。"""
        return self.record(
            "citation.verify", resource="citation",
            result="ok" if passed else "denied",
            detail=f"checked={checked} rejected={len(rejected)} "
                   f"{'；'.join(rejected[:10])}",
            **kw,
        )

    def permission_denied(self, *, reason: str, **kw: Any) -> dict[str, Any]:
        return self.record("access.denied", result="denied", detail=reason, **kw)

    # ------------------------------------------------------------------ 查询
    def _file_of(self, date: str | None = None) -> Path:
        """某天的审计文件路径。

        **括号不能省**：写成 ``f"audit-{date or datetime.now():%Y-%m-%d}.jsonl"`` 时，
        格式说明符 ``:%Y-%m-%d`` 作用在 ``date or datetime.now()`` 这个整体上；
        传进来的是字符串日期时，就会去格式化一个 str，抛
        ``ValueError: Invalid format specifier`` —— 而这个错只在"按指定日期查询"
        时才出现，默认查今天永远不触发，属于最容易被漏掉的一类。
        """
        day = date or f"{datetime.now():%Y-%m-%d}"
        return self.audit_dir / f"audit-{day}.jsonl"

    def tail(self, limit: int = 100, *, date: str | None = None) -> list[dict[str, Any]]:
        """读取审计尾部（管理后台审计中心用）。只读，不提供删除。"""
        path = self._file_of(date)
        if not path.exists():
            return []
        out: list[dict[str, Any]] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取审计日志失败：%s", exc)
            return []
        return out[-limit:]

    def search(self, keyword: str, limit: int = 200) -> list[dict[str, Any]]:
        if not keyword:
            return self.tail(limit)
        hits = [e for e in self.tail(10000) if keyword in json.dumps(e, ensure_ascii=False)]
        return hits[-limit:]

    def stats(self, *, date: str | None = None) -> dict[str, Any]:
        rows = self.tail(100000, date=date)
        by_action: dict[str, int] = {}
        by_result: dict[str, int] = {}
        for r in rows:
            by_action[r.get("action", "")] = by_action.get(r.get("action", ""), 0) + 1
            by_result[r.get("result", "")] = by_result.get(r.get("result", ""), 0) + 1
        return {
            "date": date or f"{datetime.now():%Y-%m-%d}",
            "total": len(rows),
            "by_action": dict(sorted(by_action.items(), key=lambda kv: -kv[1])),
            "by_result": by_result,
            "dir": str(self.audit_dir),
        }


audit_writer = AuditWriter()
