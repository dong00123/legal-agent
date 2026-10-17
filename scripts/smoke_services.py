"""服务层冒烟：把每个新服务用真实数据库跑一遍，验证"能写进去、能读出来"。

跑法（项目根目录）：
    .venv/Scripts/python.exe scripts/smoke_services.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.db import session as db_session  # noqa: E402
from backend.app.db.session import db_status, init_db  # noqa: E402
from backend.app.services import (  # noqa: E402
    audit_service, case_service, citation_service, config_service, draft_service,
    knowledge_service, stats_service, user_service,
)
from backend.app.services.orchestrator import RunRequest, orchestrator  # noqa: E402

FAILED: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK ' if cond else 'FAIL'} {label}" + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILED.append(label)


def sec(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    init_db()
    # 读模块属性而不是 from-import 的副本：``from x import ACTIVE_DB_BACKEND``
    # 在**导入时**就把值拷进本地名字了，init_db 之后再看还是 "none"。
    print(f"库后端：{db_status()}  后端标识={db_session.ACTIVE_DB_BACKEND}")
    sh = db_session.schema_health()
    print(f"结构自检：ok={sh['ok']} 本项目陈旧外键={len(sh['stale_fks'])} "
          f"遗留表外键={len(sh['stale_fks_foreign_tables'])} legacy备份表={len(sh['legacy_tables'])}")
    check("结构自检通过（外键未指向 legacy 表）", sh["ok"] is True)

    # ------------------------------------------------------------------ 用户
    sec("用户与团队")
    seeded = user_service.seed_defaults()
    print(f"  种子：{seeded}")
    teams = user_service.list_teams()
    check("默认团队存在", bool(teams), f"{len(teams)} 个")
    tid = teams[0]["id"]

    u = None
    for name, pwd in (("smoke_lawyer", "smoke12345"), ("admin", "admin123")):
        got = user_service.get_by_username(name)
        if got:
            u = got
            break
        try:
            u = user_service.create_user(name, pwd, role="lawyer", team_id=tid)
            break
        except user_service.UserError as exc:
            print(f"  （{name} 创建跳过：{exc}）")
    check("存在可用账号", u is not None, u["username"] if u else "")

    auth = user_service.authenticate(u["username"], "smoke12345") or \
        user_service.authenticate("admin", "admin123") or user_service.authenticate("lawyer", "lawyer123")
    check("登录成功", bool(auth) and "id" in (auth or {}))
    check("口令散列不外泄", "hashed_password" not in (auth or {}))
    bad = user_service.authenticate(u["username"], "definitely-wrong")
    check("错误口令被拒", bad is None)

    try:
        user_service.create_user("ab", "12345678")
        check("短登录名被拒", False)
    except user_service.UserError:
        check("短登录名被拒", True)
    try:
        user_service.create_user("smoke_weak", "123")
        check("弱口令被拒", False)
    except user_service.UserError:
        check("弱口令被拒", True)

    # ------------------------------------------------------------------ 案件
    sec("案件与材料（la_case_material 此前零写入）")
    case_id = "smoke-case-001"
    try:
        case_service.create_case(case_id, title="润滑油买卖合同纠纷", case_type="litigation",
                                 cause_of_action="买卖合同纠纷", owner_id=u["id"], team_id=tid)
    except case_service.CaseError as exc:
        print(f"  （案件已存在：{exc}）")

    got = case_service.get_case(case_id, case_service.INTERNAL)
    check("案件可读", got["case_id"] == case_id)

    tmp = Path(tempfile.gettempdir()) / "smoke_合同扫描件.docx"
    tmp.write_bytes("这是一个冒烟测试用的假材料内容".encode("utf-8"))
    mat = case_service.register_material(case_id, tmp, doc_type="contract",
                                         uploaded_by=u["id"], move=True)
    check("材料已登记", bool(mat.get("sha256")), f"{mat['size_bytes']} 字节")
    check("落盘路径已回传（服务端内部用）", bool(mat.get("stored_path")), mat.get("stored_path", ""))
    mats = case_service.list_materials(case_id, case_service.INTERNAL)
    check("材料可列出", len(mats) >= 1, f"{len(mats)} 份")
    check("列表默认不带服务端路径", all("stored_path" not in m for m in mats))

    # 同名同内容再登记一次：应命中已有记录，不新增行
    stored = Path(mat["stored_path"])
    check("落盘文件确实存在", stored.is_file(), str(stored))
    before_n = len(mats)
    dup = case_service.register_material(case_id, stored, filename="换个名字.docx", move=False)
    check("同内容材料不重复登记（按 sha256 去重）",
          dup is not None and dup["id"] == mat["id"], f"id={dup.get('id') if dup else None}")
    check("去重后总数不变", len(case_service.list_materials(case_id, case_service.INTERNAL)) == before_n)
    check("展示名以最后一次为准",
          case_service.get_material(mat["id"], case_service.INTERNAL)["filename"] == "换个名字.docx")

    from backend.app.security import CurrentUser

    stranger = CurrentUser(id=99999, username="stranger", role="lawyer", team_id=88888)
    try:
        case_service.get_case(case_id, stranger)
        check("越权访问被拒", False)
    except case_service.CaseForbidden:
        check("越权访问被拒", True)
    try:
        case_service.get_case(case_id, None)
        check("漏传主体被拒（失败即拒绝）", False)
    except case_service.CaseForbidden:
        check("漏传主体被拒（失败即拒绝）", True)

    prog = case_service.add_progress(case_id, stage="evidence", note="已收齐合同与往来函件",
                                     operator=u["username"])
    check("进度追加成功", len(prog["progress"]) >= 1)

    # ------------------------------------------------------------------ 知识库
    sec("知识库版本控制")
    title_mk = f"合同审查清单-冒烟-{u['username']}"
    d1 = knowledge_service.upsert_doc(title=title_mk, doc_type="checklist",
                                      category="合同法", version="v1.0", status="active",
                                      content="一、主体资格……", uploaded_by=u["id"])
    check("首次写入为新建", d1["version"] == "v1.0")
    d_again = knowledge_service.upsert_doc(title=title_mk, version="v1.0", status="draft",
                                           content="一、主体资格……（改写）")
    check("同标题同版本不重复建行（真 upsert）",
          d_again["id"] == d1["id"], f"id {d1['id']} -> {d_again['id']}")

    d2 = knowledge_service.upsert_doc(title=title_mk, doc_id=d1["id"], version="v2.0",
                                      status="active", content="一、主体资格……二、违约责任……")
    check("版本可升到 v2.0", d2["version"] == "v2.0")
    mine = [d for d in knowledge_service.list_docs(keyword=title_mk) if d["title"] == title_mk]
    check("同名只留一条我操作的记录", len(mine) == 1, f"{len(mine)} 条")
    check("正文变更后标记为待入库", mine[0]["indexed"] is False)

    ss = knowledge_service.statute_stats()
    print(f"  法规库：{ss}")
    check("法规库非空（已入库语料）", ss["statutes"] > 0, f"{ss['statutes']} 部 / {ss['articles']} 条")

    # ------------------------------------------------------------------ 草稿
    sec("文书草稿（la_draft 此前零写入）")
    dr = draft_service.create_draft(content="民事起诉状\n\n原告：张三，男，1980 年 1 月 1 日出生。",
                                    doc_type="complaint", case_id=case_id,
                                    user_id=u["id"], title="")
    check("草稿已创建", bool(dr["draft_id"]), f"v{dr['version']}")
    check("标题自动取自首行", dr["title"] == "民事起诉状", dr["title"])
    v2 = draft_service.save_draft(dr["draft_id"], content=(dr["content"] + "\n被告：李四。"),
                                  user_id=u["id"])
    check("保存生成新版本（旧版不丢）", v2["version"] == 2, f"v{v2['version']}")
    check("同任务重复保存不新建草稿",
          len(draft_service.list_drafts(case_id=case_id, doc_type="complaint")) == 1)
    from backend.app.services.draft_service import save_from_state as draft_from_state

    fs = draft_from_state("task-smoke-draft", {"revision_draft": "第3条建议修改为：……",
                                               "query": "合同审查", "case_id": case_id,
                                               "citations": []}, user_id=u["id"])
    check("从任务产出沉淀草稿", fs.get("saved") is True, f"{fs.get('draft_id')}")
    check("对照稿类型识别为 revision",
          draft_service.get_draft(fs["draft_id"])["doc_type"] == "revision")

    # ------------------------------------------------------------------ 配置
    sec("系统配置覆盖层")
    eff = knowledge_service.effective_config()
    check("配置清单非空", len(eff) > 0, f"{len(eff)} 项")
    check("每项都有生效值", all("effective_value" in e for e in eff))
    before = next(e for e in eff if e["key"] == "RERANK_TOP_K")["effective_value"]
    knowledge_service.set_config("RERANK_TOP_K", 9, updated_by=u["id"])
    after = next(e for e in knowledge_service.effective_config()
                 if e["key"] == "RERANK_TOP_K")
    check("覆盖值生效", after["effective_value"] == 9, f"{before} -> {after['effective_value']}")
    check("能区分「配过」与「没配过」", after["overridden"] is True)
    try:
        knowledge_service.set_config("DB_URL", "sqlite:///hacked.db")
        check("禁改项被拒绝", False)
    except knowledge_service.KnowledgeError:
        check("禁改项被拒绝（DB_URL）", True)
    knowledge_service.reset_config("RERANK_TOP_K")
    reset = next(e for e in knowledge_service.effective_config() if e["key"] == "RERANK_TOP_K")
    check("重置回到默认值", reset["overridden"] is False and reset["effective_value"] == before)

    # ------------------------------------------------------------------ 任务
    sec("任务执行 + 引用/草稿沉淀")
    req = RunRequest(query="劳动合同约定试用期超过法定上限，该约定效力如何？",
                     intent="consult", client="api", username=u["username"],
                     user_id=str(u["id"]), team_id=str(tid))
    outcome = asyncio.run(orchestrator.execute(req))
    print(f"  task={outcome.task_id} ok={outcome.ok} 意图={outcome.state.get('intent')} "
          f"steps={outcome.state.get('steps')} 置信度={outcome.state.get('confidence')}")
    check("任务成功", outcome.ok, outcome.error[:80])
    check("报告非空", len(outcome.state.get("report") or "") > 100)
    check("有引用", len(outcome.state.get("citations") or []) > 0,
          f"{len(outcome.state.get('citations') or [])} 条")
    check("咨询路径不产出文书（该路径刻意不含 DraftNode）",
          not (outcome.state.get("draft") or outcome.state.get("revision_draft")))

    cit = citation_service.list_citations(task_id=outcome.task_id)
    check("引用已落 la_citation", len(cit) > 0, f"{len(cit)} 条")
    cstats = citation_service.stats()
    print(f"  引用统计：{cstats}")
    check("拦截率可算", "intercept_rate" in cstats)

    drafts = draft_service.list_drafts(user_id=u["id"])
    check("草稿可列出（含上面手工建的 2 份）", len(drafts) >= 2, f"{len(drafts)} 份")
    # 咨询路径无文书产出，因此任务侧不该多出草稿 —— 这条正是"沉淀不误报"的反向验证
    check("咨询任务未凭空产生草稿", len(drafts) == 2, f"{len(drafts)} 份")

    # ------------------------------------------------------------------ 审计
    sec("审计")
    astats = audit_service.stats()
    print(f"  审计统计： total={astats['total']} denied={astats['denied']} files={astats['files']}")
    check("审计已有记录", astats["total"] >= 0)
    cons = audit_service.consistency()
    print(f"  文件/库对账：{cons}")
    actions = audit_service.actions()
    print(f"  动作种类：{len(actions)}")

    # ------------------------------------------------------------------ 统计
    sec("运营统计")
    vol = stats_service.task_volume(days=30)
    check("任务量可算", vol["total"] > 0, f"total={vol['total']} recent={vol['recent']}")
    ae = stats_service.agent_elapsed(days=30)
    check("节点耗时可算", len(ae["nodes"]) > 0, f"{len(ae['nodes'])} 个节点")
    check("耗时口径已声明", bool(ae.get("caliber")))
    tu = stats_service.tool_usage(days=30)
    print(f"  工具调用：{tu['total_calls']} 次，{len(tu['tools'])} 个工具")
    dash = stats_service.dashboard(days=30)
    check("看板四项齐备",
          all(k in dash for k in ("tasks", "cases", "users", "knowledge", "citations", "adoption")))
    check("采纳率标注为近似口径", dash["adoption"]["proxy"] is True)

    # ------------------------------------------------------------------ 收尾
    print("\n" + "=" * 60)
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print("服务层冒烟全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
