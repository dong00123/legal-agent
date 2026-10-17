"""任务生命周期回归测试：Trace 合并 与 进度单调。

覆盖的缺陷都是**静默数据丢失**（任务状态成功、报告完整，只有落库的附件变短）：

1. ``state["trace"] = tracer.to_list()`` 这个写法在本项目出现过**三处**
   （``Harness.flush_trace`` / ``finalize`` / ``snapshot``）。
   收尾归档（MemoryNode）用一个**新 Harness** 跑，它的 Tracer 里只有自己那几条事件，
   于是整条任务的 63 条 Trace 在任务成功之后被冲成 4 条。
   而落库 trace、审计追溯、管理后台的「各 Agent 耗时分布」全都读它。

2. 进度按"计划节点里已执行的比例"重算。归档阶段重算时 route 已含 MemoryNode、
   Trace 又变短了，算出来的进度从 100 掉回 11 —— 进度条倒退恰好发生在
   "任务刚跑完"的时刻，看起来像出了故障。

这两条都不该靠"记得别这么写"来防，所以下面直接对行为下断言。
"""

from __future__ import annotations

import uuid

from backend.agents.harness import Harness
from backend.agents.harness.state_manager import merge_trace
from backend.agents.state import new_state
from backend.app.db.session import init_db
from backend.app.services.task_service import TaskService, task_service


def _ev(node: str, event: str = "step", ts: str = "2026-01-01T00:00:00") -> dict:
    return {"node": node, "event": event, "detail": "", "level": "info",
            "elapsed_ms": 0, "ts": ts}


# --------------------------------------------------------------------------- 合并
def test_merge_trace_appends_without_losing() -> None:
    state: dict = {"trace": [_ev("A"), _ev("B"), _ev("C")]}
    added = merge_trace(state, [_ev("D")])
    assert added == 1
    assert [e["node"] for e in state["trace"]] == ["A", "B", "C", "D"]


def test_merge_trace_dedupes_identical_events() -> None:
    """同一 Tracer 被 flush 两次时不该把同一条事件记两遍。"""
    state: dict = {"trace": [_ev("A")]}
    assert merge_trace(state, [_ev("A")]) == 0
    assert len(state["trace"]) == 1


def test_merge_trace_distinguishes_by_detail_and_ts() -> None:
    """同节点同事件但详情不同，是两条不同的事实，不能折叠。"""
    state: dict = {"trace": [{"node": "A", "event": "e", "detail": "x", "ts": "t1"}]}
    merge_trace(state, [{"node": "A", "event": "e", "detail": "y", "ts": "t2"}])
    assert len(state["trace"]) == 2


def test_merge_trace_never_shrinks() -> None:
    """核心不变式：合并只会让 trace 变长或不变，绝不变短。"""
    state: dict = {"trace": [_ev(n) for n in ("A", "B", "C", "D", "E")]}
    before = len(state["trace"])
    merge_trace(state, [])                 # 空事件集（新 Harness 刚启动时就是这样）
    assert len(state["trace"]) == before
    merge_trace(state, [_ev("F")])
    assert len(state["trace"]) > before


def test_finalize_does_not_overwrite_trace() -> None:
    """finish() 走 finalize —— 那里原先也有一行覆盖式赋值。"""
    state = new_state("测试", task_id="task_finalize_check")
    state["trace"] = [_ev(n) for n in ("A", "B", "C")]
    harness = Harness.start(state, client="test", username="pytest")
    harness.tracer.info("D", "step", "d")
    harness.finish()
    nodes = [e["node"] for e in state["trace"]]
    assert nodes[:3] == ["A", "B", "C"], "finish 把已有 trace 冲掉了"
    assert "D" in nodes


def test_second_harness_does_not_truncate_trace() -> None:
    """复现归档场景：新 Harness 只带自己几条事件，合并后全量保留。

    断言**不写死条数**：Harness.start 自己会写一条 start 事件，不同版本节点数不同，
    写死等于把测试变成"实现快照"。这里只钉住两件真正要防的事 —— 旧事件一条不少、
    新事件确实进来了。
    """
    state = new_state("测试", task_id="task_new_harness")
    h1 = Harness.start(state, client="test", username="pytest")
    for i in range(20):
        h1.tracer.info("NodeA", "step", f"第 {i} 步")
    h1.finish()
    first = list(state["trace"])
    assert len(first) >= 20, f"第一个 Harness 的事件没落进 state：{len(first)} 条"

    h2 = Harness.start(state, client="worker", username="archive")
    h2.tracer.info("MemoryNode", "archived", "归档完成")
    h2.finish()

    after = state["trace"]
    assert len(after) > len(first), f"新 Harness 把 trace 截断了：{len(first)} → {len(after)}"
    assert [e["node"] for e in after[: len(first)]] == [e["node"] for e in first], \
        "已有事件被覆盖或重排"
    assert any(e["node"] == "MemoryNode" for e in after), "归档事件没合并进来"


# --------------------------------------------------------------------------- 进度
def test_progress_monotonic_within_same_state() -> None:
    """收尾阶段重算进度不能把 100 拉回去。"""
    init_db()
    svc = TaskService()
    task_id = "task_progress_monotonic"
    svc.create(task_id=task_id, query="进度测试", source_client="api")

    state = new_state("进度测试", task_id=task_id)
    state["route"] = ["A", "B", "C", "D"]
    state["trace"] = [_ev("A"), _ev("B"), _ev("C"), _ev("D")]
    svc.save_snapshot(task_id=task_id, state=state, trace=state["trace"],
                      status="succeeded", finished=True)
    row = svc.get(task_id)
    assert row["progress"] == 100

    # 归档阶段：route 里多了 MemoryNode、trace 被合并过，重算值更小
    state["route"] = ["A", "B", "C", "D", "MemoryNode"]
    state["trace"] = state["trace"] + [_ev("MemoryNode")]
    svc.save_snapshot(task_id=task_id, state=state, trace=state["trace"], status="degraded")
    assert svc.get(task_id)["progress"] == 100, "进度回退了"


def test_progress_advances_then_holds() -> None:
    """进度随节点推进，且不因重算而回退。

    task_id **必须唯一**：``save_snapshot`` 的进度是 ``max(旧值, 新值)``（防回退），
    而 ``la_task`` 落在 SQLite 文件里跨测试留存 —— 用固定 id 的话，
    上一轮跑到 95 的那行还在，这一轮第一次写入就被 max 成 95，
    "进度会往前走"这条断言永远看不出真相。
    """
    init_db()
    svc = TaskService()
    task_id = f"task_progress_advance_{uuid.uuid4().hex[:8]}"
    svc.create(task_id=task_id, query="进度测试", source_client="api")
    state = new_state("进度测试", task_id=task_id)
    state["route"] = ["A", "B", "C", "D"]

    seen = []
    for i, node in enumerate(("A", "B", "C", "D")):
        state["trace"] = [_ev(n) for n in ("A", "B", "C", "D")[: i + 1]]
        svc.save_snapshot(task_id=task_id, state=state, trace=state["trace"], status="running")
        seen.append(svc.get(task_id)["progress"])
    assert seen == sorted(seen), f"进度非单调：{seen}"
    assert seen[-1] > seen[0], f"进度没往前走：{seen}"
    assert 0 < seen[0] < 100, f"首末节点进度不合理：{seen}"

    # 终态：哪怕只跑了 4 个节点里的 2 个，收尾也必须到 100 —— 否则前端永远转圈
    svc.finish(task_id, state=state, status="degraded", elapsed_ms=10)
    assert svc.get(task_id)["progress"] == 100


def test_sse_progress_payload_is_a_scalar_int() -> None:
    """推给 SSE 的 ``progress`` 是**标量百分比**，不是 ``{"percent": n}``。

    来历：``progress_of`` 的返回注解原先写的是 ``dict[str, Any]``，而它
    实际转发的是 ``task_service._progress`` 的 ``int``。当初照着注解写的
    前端取了 ``data.progress.percent`` —— 拿到 ``undefined``，那根进度条
    **一次都没亮过**：不报错、不告警，只是永远空着（浏览器实测
    ``style.width`` 从未被赋值，渲染宽度 0px）。
    （那个前端消费点 2026-09-29 已随「执行轨迹」面板一起摘掉，但形状契约不变：
    ``/tasks/{id}`` 与桌面端仍在读这个字段。）

    所以这里既钉住形状，也钉住**注解**（注解是当初误导前端的那份文档，
    它再变回 ``dict`` 就等于把这套误导重新挂上去）。
    """
    import typing

    from backend.app.services.orchestrator import Orchestrator

    state = new_state("进度形状测试", task_id="task_progress_shape")
    state["route"] = ["A", "B", "C", "D"]
    state["trace"] = [_ev("A"), _ev("B")]

    got = Orchestrator.progress_of(state)
    assert isinstance(got, int) and not isinstance(got, bool), (
        f"SSE 的 progress 必须是标量 int，实得 {type(got).__name__} —— "
        "前端按 data.progress 直接当数字用"
    )
    assert 0 <= got <= 99, f"进度该在 0~99（留 5% 给收尾），实得 {got}"

    ann = typing.get_type_hints(Orchestrator.progress_of)["return"]
    assert ann is int, f"progress_of 的返回注解不再是 int（{ann}），会再次误导前端"


# --------------------------------------------------------------------------- 收尾
def test_finish_sets_terminal_state() -> None:
    init_db()
    svc = TaskService()
    task_id = "task_finish_terminal"
    svc.create(task_id=task_id, query="收尾测试", source_client="api")
    state = new_state("收尾测试", task_id=task_id)
    state["route"] = ["A", "B"]
    state["trace"] = [_ev("A"), _ev("B")]
    svc.finish(task_id, state=state, status="failed", error="模拟失败", elapsed_ms=123)
    row = svc.get(task_id)
    assert row["progress"] == 100          # 终态一律 100，否则前端永远转圈
    assert row["status"] == "failed"
    assert row["error"] == "模拟失败"
    assert row["finished_at"]


def test_snapshot_survives_unknown_task_id() -> None:
    """未知 task_id 时补建，而不是静默丢弃 —— 否则报告出来了、任务却查不到。"""
    init_db()
    svc = TaskService()
    state = new_state("补建测试", task_id="task_auto_create")
    ok = svc.save_snapshot(task_id="task_auto_create", state=state, status="running")
    assert ok
    assert svc.get("task_auto_create") is not None
