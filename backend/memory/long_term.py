"""长期结构化记忆：MySQL 案件档案。

方案 9.1：MySQL 存 case_id、脱敏案件摘要、争议焦点归档、办理结果、承办律师，权限隔离。

写入纪律（合规硬约束）：
- :meth:`LongTermMemory.upsert_case` 内部**强制调用脱敏器**，不提供"跳过脱敏"的参数。
  这不是可以用开关关掉的行为 —— 一旦提供开关，早晚会有人为了"方便调试"关掉它。
- 承办律师与团队字段用于权限隔离，本身不脱敏（否则无法判断能不能访问）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from backend.app.db.models import Case
from backend.app.db.session import session_scope
from backend.memory.sanitizer import sanitize_case_payload

logger = logging.getLogger("legal_agent.memory.long_term")


class LongTermMemory:
    """案件档案的结构化存取。"""

    # ------------------------------------------------------------------ 写
    def upsert_case(self, case_id: str, payload: dict[str, Any], *,
                    owner_id: int | None = None, team_id: int | None = None) -> dict[str, Any]:
        """写入/更新案件档案。``payload`` 中所有文本字段会被强制脱敏。"""
        if not case_id:
            raise ValueError("case_id 不能为空")

        clean = sanitize_case_payload(payload)

        with session_scope() as db:
            case = db.query(Case).filter(Case.case_id == case_id).one_or_none()
            created = case is None
            if case is None:
                case = Case(case_id=case_id)
                db.add(case)

            for field_name in ("title", "case_type", "cause_of_action", "client_name",
                               "opponent_name", "stage", "status", "summary", "outcome"):
                if field_name in clean and clean[field_name] is not None:
                    setattr(case, field_name, str(clean[field_name])[:2000]
                            if field_name == "summary" else str(clean[field_name])[:256])
            if "dispute_issues" in clean and clean["dispute_issues"] is not None:
                case.dispute_issues = clean["dispute_issues"]
            if "tags" in clean and clean["tags"] is not None:
                case.tags = clean["tags"]
            if owner_id is not None:
                case.owner_id = owner_id
            if team_id is not None:
                case.team_id = team_id
            case.updated_at = datetime.now()

            db.flush()
            result = {"case_id": case_id, "created": created,
                      "fields": sorted(k for k in clean if clean.get(k) is not None)}

        logger.info("案件档案已%s：%s（字段：%s）",
                    "创建" if result["created"] else "更新", case_id,
                    ",".join(result["fields"]))
        return result

    def upsert_from_state(self, state: dict[str, Any], *, owner_id: int | None = None,
                          team_id: int | None = None) -> dict[str, Any]:
        """从一次 Agent 任务的 State 归档案件档案。"""
        case_id = state.get("case_id") or ""
        if not case_id:
            return {"skipped": True, "reason": "本次任务未关联案件，不写长期记忆"}

        issues = [{"title": i.get("title", ""), "description": i.get("description", "")}
                  for i in (state.get("issues") or [])][:20]
        facts = [f.get("text", "") for f in (state.get("facts") or [])][:30]

        summary_parts = []
        if state.get("query"):
            summary_parts.append(f"咨询事项：{state['query']}")
        if facts:
            summary_parts.append("查明事实：" + "；".join(facts[:8]))

        return self.upsert_case(
            case_id,
            {
                "title": (state.get("query") or "")[:120],
                "case_type": _case_type(state),
                "cause_of_action": _cause(state),
                "stage": "in_progress",
                "summary": "\n".join(summary_parts)[:4000],
                "dispute_issues": issues,
                "outcome": (state.get("report") or state.get("draft") or "")[:2000],
                "tags": [state.get("intent") or ""] + [s.get("doc_name", "") for s in (state.get("statutes") or [])][:5],
            },
            owner_id=owner_id, team_id=team_id,
        )

    # ------------------------------------------------------------------ 读
    def get_case(self, case_id: str) -> dict[str, Any] | None:
        with session_scope() as db:
            case = db.query(Case).filter(Case.case_id == case_id).one_or_none()
            if case is None:
                return None
            return {
                "case_id": case.case_id,
                "title": case.title,
                "case_type": case.case_type,
                "cause_of_action": case.cause_of_action,
                "client_name": case.client_name,
                "opponent_name": case.opponent_name,
                "stage": case.stage,
                "status": case.status,
                "summary": case.summary,
                "dispute_issues": case.dispute_issues or [],
                "outcome": case.outcome,
                "tags": case.tags or [],
                "progress": case.progress or [],
                "owner_id": case.owner_id,
                "team_id": case.team_id,
                "updated_at": case.updated_at.isoformat(timespec="seconds") if case.updated_at else "",
            }

    def case_digest(self, case_id: str) -> str:
        """案件档案摘要文本（注入 Prompt 用，已脱敏）。"""
        case = self.get_case(case_id)
        if case is None:
            return ""
        lines = [f"案件编号：{case['case_id']}", f"案件名称：{case['title']}"]
        if case["cause_of_action"]:
            lines.append(f"案由：{case['cause_of_action']}")
        if case["summary"]:
            lines.append(f"档案摘要：{case['summary'][:1500]}")
        if case["dispute_issues"]:
            titles = [i.get("title", "") for i in case["dispute_issues"] if i.get("title")]
            if titles:
                lines.append("已归档争议焦点：" + "；".join(titles[:10]))
        if case["outcome"]:
            lines.append(f"办理结果：{case['outcome'][:600]}")
        if case["progress"]:
            last = case["progress"][-1]
            lines.append(f"最近进度：{last.get('stage', '')}｜{last.get('note', '')[:200]}")
        return "\n".join(lines)

    def list_cases(self, *, owner_id: int | None = None, team_id: int | None = None,
                   limit: int = 50) -> list[dict[str, Any]]:
        with session_scope() as db:
            q = db.query(Case)
            if owner_id is not None:
                q = q.filter((Case.owner_id == owner_id) | (Case.team_id == team_id))
            rows = q.order_by(Case.updated_at.desc()).limit(limit).all()
            return [{"case_id": r.case_id, "title": r.title, "case_type": r.case_type,
                     "cause_of_action": r.cause_of_action, "status": r.status,
                     "stage": r.stage, "updated_at":
                         r.updated_at.isoformat(timespec="seconds") if r.updated_at else ""}
                    for r in rows]

    def stats(self) -> dict[str, Any]:
        with session_scope() as db:
            total = db.query(Case).count()
        return {"cases": total}


def _case_type(state: dict[str, Any]) -> str:
    dt = state.get("doc_type") or ""
    if dt == "contract":
        return "contract"
    if dt in ("litigation", "evidence"):
        return "litigation"
    return "consult"


def _cause(state: dict[str, Any]) -> str:
    for s in state.get("statutes") or []:
        name = (s.get("doc_name") or "").strip("《》")
        if "劳动" in name:
            return "劳动争议"
        if "民法典" in name:
            return "民事纠纷"
    return ""


long_term_memory = LongTermMemory()
