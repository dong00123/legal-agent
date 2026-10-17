"""任务队列回归测试：认领语义 与 请求快照的存活。

覆盖的都是**静默失效**——不报错、状态看着正常，只是任务永远不执行：

1. ``run_request`` 存在 ``state`` 列里，而这一列会被图执行状态**整体覆盖**。
   不专门保住的话，任务第一次落快照之后"它是排队任务"这个标记就没了；
   于是 ``release()`` 放回 ``pending`` 的任务再也认领不到 —— 它安静地躺在
   ``pending`` 里，``/health`` 上只表现为"还没轮到"。
2. ``claim`` 必须是带条件的 UPDATE。多 worker 各自 ``peek`` 到同一条是常态，
   "先查后改"会让两边都认为自己该跑。
3. 队列快照里带着 ``material_paths``（服务端绝对路径）与客户端 IP，
   而 ``state`` 是原样透出给前端的 —— 不剥就是又一处路径泄露。

这些都不该靠"记得别这么写"来防，所以下面直接对行为下断言。
"""

from __future__ import annotations

import uuid

from backend.app.db.session import init_db
from backend.app.services import task_queue
from backend.app.services.orchestrator import RunRequest
from backend.app.services.task_service import task_service


def _task_id(tag: str) -> str:
    return f"task_q_{tag}_{uuid.uuid4().hex[:8]}"


def _payload(query: str = "未签订书面劳动合同二倍工资") -> dict:
    return RunRequest(query=query, intent="statute_lookup", client="web").to_payload()


def _settle(task_id: str) -> None:
    """把测试任务收尾，避免留在 pending 里污染后续测试（包括端到端冒烟）。"""
    task_service.finish(task_id, status="failed", error="测试收尾")


def _stored_state(task_id: str) -> dict:
    """直接读库里的 ``state`` 列。

    **不能用 ``task_service.get()``**：它刻意把队列元数据剥掉了（防路径泄露），
    于是"标记还在不在库里"这件事用它根本看不出来。这里要断言的正是**列里的值**。
    """
    from sqlalchemy import select

    from backend.app.db.models.task import TaskRecord
    from backend.app.db.session import session_scope

    with session_scope() as db:
        row = db.execute(
            select(TaskRecord).where(TaskRecord.task_id == task_id)
        ).scalar_one_or_none()
        return dict(row.state or {}) if row else {}


# --------------------------------------------------------------------------- 快照
def test_run_request_survives_snapshot_overwrite() -> None:
    """核心不变式：图状态落快照之后，**库里**的队列标记必须还在。

    模拟真实顺序——入队（写快照）→ 执行中逐个节点落快照（覆盖 state）。
    断言放在 ``state`` 列上而不是 ``peek()`` 上：``save_snapshot`` 会把状态写成
    ``running``，而 ``peek`` 只捞 ``pending``，用它断言就验不到"标记有没有被冲掉"。
    """
    init_db()
    tid = _task_id("survive")
    enq = task_queue.enqueue(_payload(), task_id=tid, query="快照存活测试")
    assert enq["queued"], enq
    assert _stored_state(tid)[task_queue.RUN_REQUEST_KEY], "入队时没写进请求快照"

    # 图执行会连着落好几次快照，每次的 state 都是"图状态"（不含 run_request）
    for i in range(3):
        task_service.save_snapshot(
            task_id=tid, status="running",
            state={"query": "快照存活测试", "task_id": tid,
                   "route": ["StatuteNode"][: i + 1]},
        )

    state = _stored_state(tid)
    assert state.get(task_queue.RUN_REQUEST_KEY), \
        "落快照之后 run_request 被冲掉了 —— release 放回的任务再也认领不到"
    assert state.get("query") == "快照存活测试", "图状态本身也要正常写进去"
    _settle(tid)


def test_release_keeps_task_claimable() -> None:
    """组合不变式：认领 → 执行中断 → release 之后，还能被再次认领。

    这正是超时/异常那条路径：``run_one`` 里 ``execute`` 已经落过快照，
    然后 ``release`` 把它放回 ``pending``。
    """
    init_db()
    tid = _task_id("release")
    task_queue.enqueue(_payload(), task_id=tid, query="重试测试")

    assert task_queue.claim(tid, "worker:test")
    task_service.save_snapshot(task_id=tid, status="running",
                               state={"query": "重试测试", "task_id": tid})
    task_queue.release(tid, "模拟执行中断")

    ids = {j["task_id"] for j in task_queue.peek(limit=200)}
    assert tid in ids, "release 之后认领不到 —— 任务永久卡在 pending"

    row = task_service.get(tid)
    assert row["status"] == "pending"
    assert row["progress"] == 0, "放回队列时进度该归零，否则前端一直显示 99%"
    assert row["error"] == "", f"不该把 worker 标记留给律师看：{row['error']}"
    _settle(tid)


def test_claim_is_exclusive() -> None:
    """两个 worker 抢同一条：只有一个能拿到。"""
    init_db()
    tid = _task_id("claim")
    task_queue.enqueue(_payload(), task_id=tid, query="认领互斥")

    first = task_queue.claim(tid, "worker:A")
    second = task_queue.claim(tid, "worker:B")
    assert first is True
    assert second is False, "同一条任务被两次认领 —— 会被两个 worker 各跑一遍"
    assert task_service.get(tid)["status"] == "running"
    _settle(tid)


def test_claim_clears_worker_marker_from_error_field() -> None:
    """``error`` 是给律师看的失败原因，不能长期留着 ``worker:xxx``。

    这条守的是"注释描述了清理、代码其实没清"那类假象：``claim`` 为把认领
    做成一条原子 UPDATE，只能把 worker 标识临时写进 ``error``，
    随后必须自己擦掉 —— 而 ``set_progress`` 默认并不动 ``error``。
    """
    init_db()
    tid = _task_id("marker")
    task_queue.enqueue(_payload(), task_id=tid, query="标记清理")
    assert task_queue.claim(tid, "worker:XYZ")
    assert "worker:" not in (task_service.get(tid)["error"] or "")
    _settle(tid)


def test_peek_skips_inline_tasks_without_snapshot() -> None:
    """内联执行（``/chat`` 直接跑）也建 ``pending`` 行，worker **不该**认领它。

    否则会和 API 进程里的那次执行撞车：同一个 task_id 跑两遍，两边都以为自己对。
    """
    init_db()
    tid = _task_id("inline")
    # 模拟内联路径：create 时不带 state
    task_service.create(task_id=tid, query="内联任务", source_client="web")
    assert tid not in {j["task_id"] for j in task_queue.peek(limit=200)}
    _settle(tid)


def test_queue_stats_separates_queued_from_pending() -> None:
    """``pending`` 里混着"认领不到的孤儿"，必须分开报，否则积压数字是假的。"""
    init_db()
    queued = _task_id("statq")
    orphan = _task_id("stato")
    task_queue.enqueue(_payload(), task_id=queued, query="统计-排队")
    task_service.create(task_id=orphan, query="统计-孤儿", source_client="web")

    stats = task_queue.queue_stats()
    assert stats["queued"] >= 1
    assert stats["pending"] >= stats["queued"], stats
    assert "caliber" in stats, "口径必须一起给出，否则数字没法核对"
    _settle(queued)
    _settle(orphan)


def test_queue_meta_not_exposed_by_get() -> None:
    """``run_request`` 里有服务端绝对路径与客户端 IP —— 不出接口。"""
    init_db()
    tid = _task_id("leak")
    task_queue.enqueue(
        RunRequest(query="泄露检查", material_paths=["C:/secret/合同.docx"],
                   ip="10.0.0.9", client="exe").to_payload(),
        task_id=tid, query="泄露检查",
    )
    row = task_service.get(tid)
    assert "run_request" not in row["state"], "队列快照被透出，带出了服务端路径"
    assert "C:/secret" not in str(row), f"响应体里出现了服务端路径：{row['state'].keys()}"
    _settle(tid)


# --------------------------------------------------------------------------- 回收
def test_requeue_stale_returns_queued_marks_orphan_failed() -> None:
    """卡死的任务分两类处理，**不能都放回 pending**。

    没有请求快照的那些（内联执行被中断）放回去也没人能执行，
    只会让 ``pending`` 越堆越多；它们要显式标失败收尾。

    阈值用 ``0.001`` 而不是 ``0``：``0`` 的语义是"关闭回收"（见下一条测试），
    而"刚认领完就算卡死"用"几毫秒之前"来表达才不歧义。
    """
    init_db()
    with_snap = _task_id("stale1")
    without = _task_id("stale2")
    task_queue.enqueue(_payload(), task_id=with_snap, query="卡死-有快照")
    task_service.create(task_id=without, query="卡死-无快照", source_client="web")
    assert task_queue.claim(with_snap, "worker:dead")
    assert task_queue.claim(without, "worker:dead")

    recovered = task_queue.requeue_stale(older_than_seconds=0.001)
    assert with_snap in recovered
    assert task_service.get(with_snap)["status"] == "pending"
    # 无快照的：标失败，且给出原因（不是悄无声息地消失）
    dead = task_service.get(without)
    assert dead["status"] == "failed"
    assert dead["error"]
    assert without not in recovered, "认领不到的任务不该被放进队列"
    _settle(with_snap)


def test_requeue_stale_disabled_when_threshold_is_zero() -> None:
    """阈值为 0 = **关闭回收**。

    判据关掉时宁可什么都不动，也不要误伤正在跑的长任务 ——
    把在跑的任务放回队列会被另一个 worker 认领，同一任务并发执行两遍，
    比"卡住"更难查。
    """
    from backend.app.config import settings

    init_db()
    tid = _task_id("nostale")
    task_queue.enqueue(_payload(), task_id=tid, query="不回收")
    assert task_queue.claim(tid, "worker:live")

    original = settings.WORKER_TASK_TIMEOUT
    try:
        settings.WORKER_TASK_TIMEOUT = 0        # 超时关闭 → 有效阈值退化为 0
        assert task_queue.effective_stale_seconds() == 0.0
        assert task_queue.requeue_stale() == []
    finally:
        settings.WORKER_TASK_TIMEOUT = original

    assert task_service.get(tid)["status"] == "running", "关闭回收时不该动状态"
    _settle(tid)


def test_stale_running_lists_all_when_threshold_disabled() -> None:
    """阈值关闭时 ``stale_running`` 要**列出全部 running**，而不是返回空。

    返回空会让人以为"没有卡住的任务"，而实际只是判据关掉了 ——
    这两件事在界面上必须能分开。"""
    from backend.app.config import settings

    init_db()
    tid = _task_id("staleall")
    task_queue.enqueue(_payload(), task_id=tid, query="列出全部 running")
    assert task_queue.claim(tid, "worker:live")

    original = settings.WORKER_TASK_TIMEOUT
    try:
        settings.WORKER_TASK_TIMEOUT = 0
        listed = {r["task_id"] for r in task_queue.stale_running()}
    finally:
        settings.WORKER_TASK_TIMEOUT = original
    assert tid in listed
    _settle(tid)


def test_effective_stale_seconds_follows_task_timeout() -> None:
    """阈值跟着任务超时走，而不是写死一小时。

    写死的风险是"有人把超时调到 4 小时之后，固定阈值就不够用了"，
    于是回收逻辑开始误伤正常的长任务。
    """
    from backend.app.config import settings

    original = settings.WORKER_TASK_TIMEOUT
    try:
        settings.WORKER_TASK_TIMEOUT = 600.0
        assert task_queue.effective_stale_seconds() == 1200.0
        settings.WORKER_TASK_TIMEOUT = 0          # 超时关闭
        assert task_queue.effective_stale_seconds() == 0.0, "超时关闭时无法区分长任务与死任务，必须不回收"
    finally:
        settings.WORKER_TASK_TIMEOUT = original


# --------------------------------------------------------------------------- 序列化
def test_run_request_payload_roundtrip_keeps_every_field() -> None:
    """入队快照与 worker 重建走同一份字段清单，不手写两遍。"""
    req = RunRequest(query="往返", task_id="task_x", intent="doc_draft",
                     draft_type="defense", material_paths=["a.docx"], use_graph=False)
    back = RunRequest.from_payload(req.to_payload())
    assert back == req


def test_from_payload_tolerates_unknown_and_missing() -> None:
    """API 与 worker 不是同一时刻部署的：多出来的键要忽略，缺的键用默认值。"""
    payload = RunRequest(query="兼容").to_payload()
    payload["未来新增的字段"] = 1
    payload["material_paths"] = None
    back = RunRequest.from_payload(payload)
    assert back.query == "兼容"
    assert back.material_paths == []


def test_from_payload_rejects_snapshot_without_query() -> None:
    """``query`` 是唯一必填项：没有它只能重建出一条空报告，不如显式报错。"""
    import pytest

    with pytest.raises(ValueError):
        RunRequest.from_payload({"task_id": "task_x", "intent": "consult"})
    with pytest.raises(ValueError):
        RunRequest.from_payload("不是字典")


def test_execute_reuses_given_task_id() -> None:
    """``RunRequest.task_id`` 必须被沿用。

    不带它的话 worker 认领的那行永远停在 ``running``（前端一直转圈），
    而结果落在一个凭空多出来的新任务上 —— 两边都"看起来正常"。
    """
    from backend.agents.state import new_state

    state = new_state("沿用编号", task_id="task_claimed_123")
    assert state["task_id"] == "task_claimed_123"
    assert RunRequest(query="q", task_id="task_claimed_123").task_id == "task_claimed_123"


# --------------------------------------------------------------------------- 健康
def test_queue_is_not_reported_degraded_without_broker() -> None:
    """没装 RabbitMQ 时，**队列本身不能报降级**。

    曾经 ``runtime_backends()["task_queue"]`` 的 ``enabled`` 直接取
    ``RABBITMQ_ENABLED``，于是不装 MQ 的环境里 ``/health`` 一直显示
    "task_queue 已关闭，回落：进程内 asyncio 队列" —— 而实际上队列由
    数据库支撑、任务照常排队执行，那条回落实为虚构（项目里唯一的
    ``asyncio.Queue`` 是 SSE 用的）。看到它的人会去排查一个不存在的故障。

    队列与 broker 要分开报：broker 只影响"多久被发现"，不影响正确性。
    """
    from backend.app.config import settings

    backends = settings.runtime_backends()
    queue = backends["task_queue"]
    broker = backends["task_broker"]

    assert queue["enabled"] is settings.DB_ENABLED, "队列随关系库，不随 broker"
    assert broker["enabled"] is settings.RABBITMQ_ENABLED
    assert "asyncio" not in str(queue), "队列的回落说明不该提进程内 asyncio 队列（那是 SSE 的）"
