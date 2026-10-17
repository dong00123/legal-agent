"""动态规划 + 相似问题缓存的回归测试。

测的是**决策**而不是"内部怎么实现"：给定什么问题 → 跑哪些节点。
因为这两处的失败模式都是"静默"——裁错节点不报错，只是结果缺一块；
缓存错命中也不报错，只是把别人的结论当成你的答案。
"""

from __future__ import annotations

import pytest

from backend.agents import planning


# --------------------------------------------------------------------------- 动态规划
def test_rights_question_gets_case_node():
    """「能主张哪些权利」这类诉求要类案支撑，不能只给法条然后直答。"""
    draft = planning.draft_plan(
        intent="quick_answer",
        query="用人单位未签书面劳动合同，劳动者可以主张哪些权利？",
    )
    assert "CaseNode" in draft.nodes, draft.rules
    assert draft.nodes[-1] == "ReportNode"


def test_shallow_lookup_stays_short():
    """「补偿金最低标准是多少」是单一结论型，不该跑完整证据链。"""
    draft = planning.draft_plan(
        intent="consult", query="竞业限制补偿金最低标准是多少",
    )
    assert "AnalysisNode" not in draft.nodes
    assert "IntakeNode" not in draft.nodes
    assert draft.nodes[-1] == "ReportNode"


def test_contract_review_keeps_intake_without_material_text():
    """回归：材料以 material_ids 传入时 material_text 仍是空的。

    此时若按"没材料"裁掉 Intake/Review，跑完 clauses=0 ——
    一份没有条款的合同审查比不跑更糟（历史上踩过）。
    """
    draft = planning.draft_plan(
        intent="contract_review",
        query="请审查这份合同，逐条指出风险条款、法律依据与修改建议。",
        has_material=False, material_text="",
    )
    assert "IntakeNode" in draft.nodes
    assert "ReviewNode" in draft.nodes


def test_draft_intent_keeps_draft_node():
    draft = planning.draft_plan(intent="doc_draft", query="帮我写一份劳动仲裁申请书")
    assert "DraftNode" in draft.nodes


def test_off_topic_only_answers():
    draft = planning.draft_plan(intent="off_topic", query="你好")
    assert draft.nodes == ["ReportNode"]


def test_sanitize_filters_unknown_node():
    """模型幻觉出的节点名必须被挡掉：否则 next_node 找不到它 → 流程静默结束。"""
    nodes = planning.sanitize(
        ["StatuteNode", " hallucinatedNode ", "ReportNode", "CaseNode"],
        intent="quick_answer", fallback=["StatuteNode", "ReportNode"],
    )
    assert " hallucinatedNode " not in nodes
    assert nodes[-1] == "ReportNode"


def test_sanitize_falls_back_when_empty():
    assert planning.sanitize([], intent="consult", fallback=["StatuteNode", "ReportNode"]) == \
        ["StatuteNode", "ReportNode"]


# --------------------------------------------------------------------------- 语义缓存
@pytest.mark.asyncio
async def test_cache_roundtrip_hits_similar_question():
    """同一个问题换个说法仍应命中；不同领域的问题不该命中。"""
    from backend.app.services import query_cache
    from backend.app.services.task_service import task_service

    if not query_cache.enabled():
        pytest.skip("QUERY_CACHE_ENABLED=false")

    # 先清空：这条用例断言的是"命中的就是刚写进去的那条"，
    # 而 qcache 是**真实集合**，开发机上跑过 e2e 之后里面会有别的条目
    # （提问往往就是同一句），不清干净就会命中别人的结论。
    await query_cache.purge()

    # 缓存里只存 task_id（结论本体仍在 la_task）—— 所以必须真有一行任务，
    # 否则 lookup 会正确地判为"结论已不存在"而返回未命中。
    task_id = "task_unit_cache_test"
    task_service.create(task_id=task_id, query="用人单位未签书面劳动合同", kind="consult")
    task_service.finish(task_id, state={}, result={"report": "可主张双倍工资差额…"},
                        status="succeeded", elapsed_ms=1)

    ok = await query_cache.store(
        "用人单位未签书面劳动合同，劳动者可以主张哪些权利？",
        task_id=task_id, intent="consult", confidence=0.85,
        citation_check="passed",
    )
    if not ok:
        pytest.skip("embedding 不可用，跳过缓存往返测试")
    try:
        # 新集合写入后 Milvus 有短暂的数据可见延迟（生产上是"这次写完、下次才读"，
        # 不受影响；测试是写完立刻读，所以按最终一致轮询几次）。
        import asyncio as _asyncio

        hit = None
        for _ in range(5):
            hit = await query_cache.lookup("公司不签劳动合同，我能主张哪些权利？")
            if hit is not None:
                break
            await _asyncio.sleep(0.6)
        assert hit is not None, "换个说法就该命中，否则缓存等于没做"
        assert hit["task_id"] == task_id
        assert hit["similarity"] >= 0.90

        # 材料不同的同类提问不该命中（合同审查尤其致命）
        miss = await query_cache.lookup(
            "公司不签劳动合同，我能主张哪些权利？", material_text="甲方：某某公司 乙方：张三",
        )
        assert miss is None
    finally:
        await query_cache.purge()


@pytest.mark.asyncio
async def test_cache_reads_back_degraded_tasks():
    """``degraded`` 也必须能读回来 —— 否则缓存**写得进去、永远读不出来**。

    这个状态不是边角情况，而是这条链上最后一处静默失效：
    ``harness._status_of`` 判"有证据缺口就记 degraded"，而证据缺口是常态
    （先前的假缺口更让它必然出现），于是**每一次**入库的任务都存成 degraded。
    而 ``_load_task`` 原先只认 succeeded/finished/done —— 查得到任务、状态对不上、
    返回 None、按未命中处理，日志里连一条警告都没有。
    表现是：入库日志一切正常，换个说法再问还是从头跑一遍。
    """
    from backend.app.services import query_cache
    from backend.app.services.task_service import task_service

    task_id = "task_unit_cache_degraded"
    task_service.create(task_id=task_id, query="试用期最长可以约定多久", kind="consult")
    task_service.finish(task_id, state={}, result={"report": "……"},
                        status="degraded", elapsed_ms=1)
    row = query_cache._load_task(task_id)
    assert row is not None, "degraded（降级完成）是可用交付物，不该被当成不可复用"

    # 反面对照：还在跑的、失败的任务仍然必须挡住
    for bad in ("running", "pending", "failed"):
        task_service.save_snapshot(task_id=task_id, status=bad, state={})
        assert query_cache._load_task(task_id) is None, f"{bad} 的任务不该被复用"


@pytest.mark.asyncio
async def test_cache_returns_newest_of_equivalent_entries():
    """同等问题有多条缓存时取**最新**的：强制重跑出来的新结论要能推翻旧的。

    按相似度排会留下死角 —— 律师重跑得到新结论并入库后，
    下次提问仍可能命中被推翻的旧条目，而且他再也绕不过去。
    """
    from backend.app.services import query_cache
    from backend.app.services.task_service import task_service

    if not query_cache.enabled():
        pytest.skip("QUERY_CACHE_ENABLED=false")

    ids = ("task_unit_cache_old", "task_unit_cache_new")
    for tid in ids:
        task_service.create(task_id=tid, query="试用期最长可以约定多久", kind="consult")
        task_service.finish(tid, state={}, result={"report": f"结论-{tid}"},
                            status="succeeded", elapsed_ms=1)
    try:
        ok = await query_cache.store("试用期最长可以约定多久", task_id=ids[0],
                                     intent="consult", citation_check="passed")
        if not ok:
            pytest.skip("embedding 不可用，跳过缓存往返测试")
        await query_cache.store("试用期最长可以约定多久", task_id=ids[1],
                                intent="consult", citation_check="passed")
        import asyncio as _asyncio

        # 采样到**最新那条可见**为止，而不是「拿到任意一条就 break」。
        #
        # 后者会撞上向量库的写入可见性窗口：新条目尚未可见、旧条目已经可见的那几百
        # 毫秒里，lookup 会正常返回旧条目，循环立刻退出，断言拿到旧条目而红 ——
        # 看着像「排序写错了」，实际是**测试自己没等**。实测就是这么红的
        # （全量跑一次红、单独重跑三次绿），排查时被它误导过一轮。
        #
        # 断言本身一点都不放松：两条都可见之后，最新那条必须胜出。
        # 真要排错序，这里会一直取到旧条目直到超时，报错信息会直说。
        hit = None
        for _ in range(12):
            hit = await query_cache.lookup("试用期最长可以约定多久")
            if hit is not None and hit["task_id"] == ids[1]:
                break
            await _asyncio.sleep(0.5)
        assert hit is not None, "等了 6 秒一次都没命中缓存"
        assert hit["task_id"] == ids[1], (
            f"两条都可见之后仍取到旧条目：{hit['task_id']} —— "
            "律师强制重跑得到的新结论会被被推翻的旧条目盖住，而且他再也绕不过去。"
        )
    finally:
        await query_cache.purge()


def test_fingerprint_ignores_punctuation():
    assert query_cache_fingerprint_equal("甲方：某某公司", "甲方某某公司")


def query_cache_fingerprint_equal(a: str, b: str) -> bool:
    from backend.app.services.query_cache import fingerprint

    return fingerprint(a) == fingerprint(b)


# ===========================================================================
# 以下用例针对一组**真实的静默失效**：两个功能（动态规划、语义缓存）后端都写完
# 并且开关都打开了，端到端却一个都没生效。根因是一条链：
#   动态规划裁短链路 → 顺手裁掉了终审的输入 → 终审空转（checked 恒为 0）
#   → 引用未经核验 + 内容项拿不到分 → 置信度 0.35 < 0.70 → 缓存一条也写不进去。
# 所以下面按"链"来测，而不是按模块测：任何一环单独看都是绿的。
# ===========================================================================

# --------------------------------------------------------------------- 桩
class _FakeTracer:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def add(self, *a, **kw) -> None:
        self.events.append((a, kw))

    def info(self, *a, **kw) -> None:
        self.events.append((a, kw))

    def warn(self, *a, **kw) -> None:
        self.events.append((a, kw))

    def error(self, *a, **kw) -> None:
        self.events.append((a, kw))

    def to_list(self) -> list:
        return []


class _FakeCtx:
    def __init__(self) -> None:
        self.tracer = _FakeTracer()
        self.notes: list[str] = []

    def note(self, text: str) -> None:
        self.notes.append(text)

    def audit(self, *a, **kw) -> None:
        return None


class _StatusInvoker:
    """按**入参顺序**逐条返回效力状态（与 ``statute_tools`` 的工具行为一致）。

    顺序而非名称匹配是刻意的：池核验正是靠下标对齐的，
    桩按名称匹配就把这条实现细节测掉了。
    """

    def __init__(self, statuses: dict[str, str] | None = None) -> None:
        self.statuses = statuses or {}

    async def call(self, tool: str, agent: str, **kw):
        from backend.tools.base import ToolResult

        if tool != "statute_validity_check":
            return ToolResult(ok=True, tool=tool, data={})
        results = []
        for it in kw.get("items") or []:
            key = f"{it.get('doc_name')}|{it.get('article_no')}"
            results.append({
                "doc_name": it.get("doc_name"),
                "article_no": it.get("article_no"),
                "status": self.statuses.get(key, "现行有效"),
            })
        return ToolResult(ok=True, tool=tool, data={"results": results})


class _DeadInvoker:
    """校验工具全部不可用（用来验证"工具挂了"时的降级方向）。"""

    async def call(self, tool: str, agent: str, **kw):
        from backend.tools.base import ToolResult

        return ToolResult(ok=False, tool=tool, error="stub: 工具不可用", degraded=True)


def _validate(state: dict, statuses: dict[str, str] | None = None):
    from backend.agents.harness.validator import CitationValidator

    return CitationValidator(_FakeCtx(), state, _StatusInvoker(statuses)).validate()


# ------------------------------------------------------------------ 终审覆盖证据池
def test_validatable_accepts_evidence_pool_without_prose():
    """只有检索结果、没有任何散文时，终审也必须认为"有东西可核"。

    此前 ``_validatable`` 只看 draft/report/risk_points/strategy/issues —— 全是散文。
    而 ReportNode 排在 ValidatorNode **之后**（第一轮必然为空）、strategy 由被裁掉的
    AnalysisNode 产出，于是快速问答这一整类链路六项检查全空，
    明明检索到 14 条法条却判"无待校验内容"，终审空转。
    """
    from backend.agents.nodes.validator_node import _validatable

    ok, why = _validatable({"statutes": [{"doc_name": "中华人民共和国劳动法",
                                          "article_no": "第八十七条"}]})
    assert ok is True, why

    ok, why = _validatable({"cases": [{"case_no": "（2023）京01民终12345号"}]})
    assert ok is True, why

    # 真的什么都没检索到时仍是 pending —— 不能为了"有结论"就谎报通过
    ok, why = _validatable({})
    assert ok is False, why


@pytest.mark.asyncio
async def test_pool_only_validation_is_not_empty_pass():
    """没有正文、只有证据池：终审要**真的核过**，且不能是空转通过。

    断言的是 ``inspected > 0``：只有 ``passed`` 为真是不够的 ——
    ``passed == (blocking == 0)``，什么都没核时它天然为真。
    """
    state = {
        "statutes": [
            {"doc_name": "中华人民共和国劳动法", "article_no": "第八十七条",
             "status": "现行有效"},
            {"doc_name": "中华人民共和国劳动合同法", "article_no": "第八十七条",
             "status": "现行有效"},
        ],
    }
    result = await _validate(state)
    assert result.pool_statutes == 2
    assert result.inspected > 0, "核验计数为 0 就是空转通过"
    assert result.passed is True
    assert result.to_summary()["inspected"] == result.inspected


_REPEALED = "中华人民共和国经济合同法|第二十七条"


@pytest.mark.asyncio
async def test_repealed_pool_statute_blocks_even_when_uncited():
    """池里出现已废止法条 → 拦截，**哪怕正文一个字都没引用它**。

    报告正文是从检索结果拼出来的（``report_node._citation_lines``），
    池里的废法迟早会出现在律师眼前 —— 「过时的法律依据比没有依据更危险」。
    """
    state = {
        "statutes": [
            {"doc_name": "中华人民共和国劳动法", "article_no": "第八十七条",
             "status": "现行有效"},
            {"doc_name": "中华人民共和国经济合同法", "article_no": "第二十七条",
             "status": "现行有效"},   # 检索层给的状态是旧的，以效力工具的结论为准
        ],
    }
    result = await _validate(state, {_REPEALED: "已废止"})
    assert result.passed is False
    assert result.blocking == 1, result.rejected
    assert any("经济合同法" in r for r in result.rejected), result.rejected
    assert result.pool_statutes == 2, "池里两条都要核，不能只核被引用到的"


@pytest.mark.asyncio
async def test_repealed_pool_statute_blocks_when_tool_unavailable():
    """效力工具不可用时，池里**自己标着已废止**的仍要拦住。

    工具抖动不等于法律依据没问题：状态字段是随证据一起检索回来的，
    工具答不上来时就该用它 —— 反过来（工具一挂就全放行）等于
    "把校验服务变成绕过校验的开关"。
    """
    from backend.agents.harness.validator import CitationValidator

    state = {"statutes": [{"doc_name": "中华人民共和国经济合同法",
                           "article_no": "第二十七条", "status": "已废止"}]}
    dead = CitationValidator(_FakeCtx(), state, _DeadInvoker())
    result = await dead.validate()
    assert result.passed is False
    assert result.blocking == 1, result.rejected


@pytest.mark.asyncio
async def test_cited_repealed_statute_is_reported_once():
    """正文引用了的废法只在"引用"那一轮报一次，不在池那一轮重复计数。

    重复计数会让"拦截 23 条"里的 23 虚高，而报告里逐条列出的拦截项
    却对不上数 —— 律师第一条就会怀疑整份清单。
    """
    state = {
        "draft": "依据《中华人民共和国经济合同法》第二十七条，本案应当……",
        "statutes": [
            {"doc_name": "中华人民共和国经济合同法", "article_no": "第二十七条",
             "status": "现行有效"},
        ],
    }
    result = await _validate(state, {_REPEALED: "已废止"})
    assert result.blocking == 1, result.rejected
    assert result.checked == 1


# ------------------------------------------------------------------ 动态规划接终审
def test_retrieval_plan_always_gets_validator():
    """不变式：计划里只要有法条/类案检索，就必须有节点判定它们的效力。

    这条原先靠提问措辞触发（正文里点了具体法条/案号才加），
    于是"竞业限制补偿金最低标准是多少"这类最常见的问题永远没有终审，
    ``citation_check`` 恒为 pending、报告里写着「未执行引用终审」。
    """
    from backend.agents.harness.router import INTENT_ROUTES
    from backend.agents.planning import _EXPANDABLE

    for intent in INTENT_ROUTES:
        draft = planning.draft_plan(
            intent=intent, query="用人单位未签书面劳动合同，劳动者可以主张哪些权利？",
        )
        if "ValidatorNode" not in (set(_EXPANDABLE.get(draft.intent, ()))
                                   | set(INTENT_ROUTES.get(draft.intent, {}).get("nodes", []))):
            continue  # 该意图的候选池里没有终审，不是本用例的范围
        if "StatuteNode" in draft.nodes or "CaseNode" in draft.nodes:
            assert "ValidatorNode" in draft.nodes, (
                f"{intent} 检索了法条/类案却没有终审：{draft.nodes}"
            )
        assert draft.nodes[-1] == "ReportNode", draft.nodes


def test_quick_answer_route_has_validator():
    """快速问答的**静态**路由也要带终审 —— 动态规划可以裁，静态路由不能没有。"""
    from backend.agents.harness.router import route_for_intent

    nodes = route_for_intent("quick_answer")["nodes"]
    assert nodes == ["StatuteNode", "ValidatorNode", "ReportNode"], nodes


# ------------------------------------------------------------------ 假缺口
def test_dynamic_trimming_is_not_a_gap():
    """动态规划按问题省节点**不是**超预算，不该记成证据缺口。

    原先的判据是"计划比静态路由短"，而动态规划的本职就是让计划变短：
    静态 7 个节点、按问题只跑 2 个，会被记成
    「节点数超出步数预算（16）」—— 只跑 2 步却说超了 16 步的预算，
    内容是假的，还凭空扣 0.05 置信度，而这 0.05 恰好卡在缓存准入门槛上。
    """
    from backend.agents.harness.planner import Planner

    plan = Planner().plan(
        intent="contract_review",
        query="请审查这份合同",
        has_material=True,
        nodes_override=["StatuteNode", "ReportNode"],
    )
    assert plan.nodes() == ["StatuteNode", "ReportNode"]
    assert plan.trimmed == [], "按问题主动裁剪被误判成了超预算"


def test_budget_trimming_records_what_was_dropped():
    """真的超预算时要如实记录被裁的节点 —— 反面对照，否则上面那条测了个空。"""
    from backend.agents.harness.planner import Planner

    plan = Planner().plan(intent="contract_review", query="请审查这份合同",
                          has_material=True, max_steps=3)
    assert plan.trimmed, "预算 3 步塞不下完整链路，trimmed 不该为空"
    for node in plan.trimmed:
        assert node not in plan.nodes()
    assert "ReportNode" in plan.nodes(), "收尾节点必须留下"


# ------------------------------------------------------------------ 缓存准入
class _CacheSpy:
    """替掉 ``query_cache.store``：只记录"到底写没写"，不碰 Milvus。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, query: str, **kw):
        self.calls.append({"query": query, **kw})
        return True


async def _admit(monkeypatch, *, check: str, inspected: int,
                 confidence: float = 0.80, intent: str = "consult") -> list[dict]:
    from backend.app.services import query_cache
    from backend.app.services.orchestrator import Orchestrator, RunOutcome, RunRequest

    spy = _CacheSpy()
    monkeypatch.setattr(query_cache, "enabled", lambda: True)
    monkeypatch.setattr(query_cache, "store", spy)
    outcome = RunOutcome(
        task_id="task_unit_cache_admit", ok=True, elapsed_ms=1,
        state={"intent": intent, "confidence": confidence, "citation_check": check,
               "citation_summary": {"checked": 0, "inspected": inspected}},
    )
    await Orchestrator._maybe_cache(RunRequest(query="试用期最长可以约定多久"), outcome)
    return spy.calls


@pytest.mark.asyncio
async def test_cache_rejects_pending_citation_check(monkeypatch):
    """终审没跑（pending）的结论不入库。

    模块 docstring 一直写着「引用终审没过…不入库」，而实现放行了 pending；
    实测里 pending 是常态而非偶发，于是缓存里会躺满未经核验的结论。
    """
    assert await _admit(monkeypatch, check="pending", inspected=5) == []


@pytest.mark.asyncio
async def test_cache_rejects_empty_validation(monkeypatch):
    """``passed`` 但一条都没核过 = 空转通过，不能当"已核验"缓存起来。"""
    assert await _admit(monkeypatch, check="passed", inspected=0) == []


@pytest.mark.asyncio
async def test_cache_accepts_verified_result(monkeypatch):
    """通过终审且确实核过东西 → 入库。正面对照，否则上面两条靠"永不写入"也能通过。"""
    calls = await _admit(monkeypatch, check="passed", inspected=14)
    assert len(calls) == 1, calls
    assert calls[0]["citation_check"] == "passed"


@pytest.mark.asyncio
async def test_cache_confidence_floor_is_reachable(monkeypatch):
    """无材料咨询的置信度天花板约 0.65，0.70 的门槛对最常见场景基本不可达。

    0.62 是刻意取在旧门槛（0.70）与新门槛（0.60）之间的值：
    它必须能入库，否则"相似问题直接命中缓存"在实际使用中永远触发不了。
    """
    calls = await _admit(monkeypatch, check="passed", inspected=14, confidence=0.62)
    assert len(calls) == 1, "置信度 0.62 被挡在门外，说明门槛没落到可达区间"


# ------------------------------------------------------------------ no_cache
def test_run_request_has_no_cache_flag():
    """``execute()`` 里那句 ``getattr(req, "no_cache", False)`` 曾经永远取到 False
    —— 字段根本没定义，开关看着有、按下去没反应。"""
    from backend.app.services.orchestrator import RunRequest

    assert RunRequest(query="x").no_cache is False
    assert RunRequest(query="x", no_cache=True).no_cache is True


def test_no_cache_skips_lookup(monkeypatch):
    """no_cache=true 时**不能**去查缓存，否则律师永远绕不过一份他不信的结论。"""
    from backend.app.services import orchestrator as orch
    from backend.app.services.orchestrator import RunRequest

    monkeypatch.setattr(orch.settings, "QUERY_CACHE_ENABLED", True)
    assert orch._cache_wanted(RunRequest(query="x")) is True
    assert orch._cache_wanted(RunRequest(query="x", no_cache=True)) is False, \
        "强制重跑仍然去查了缓存 —— 这个开关就白加了"


# ------------------------------------------------------------------ 置信度可达性
def test_consult_ceiling_clears_the_cache_floor():
    """端到端跑出来的最重要的一条：**咨询类任务的置信度天花板必须高过缓存门槛**。

    在这条链修好之前，一次检索到 14 条法条、9 篇类案、终审也通过的咨询
    拿到的是 0.50 —— 不是"差一点"，是**结构上够不着**：无材料时
    facts/issues/内容完整性 三块全空，只剩 0.35 + 0.20。
    于是缓存集合恒为空，需求里的"相似问题直接命中缓存"根本没发生过。
    这条用例把"够得着"钉死：改动任何一块权重，它会先红。
    """
    from backend.agents.harness.state_manager import compute_confidence
    from backend.app.config import settings

    state = {
        "intent": "consult",
        "statutes": [{"doc_name": "中华人民共和国劳动法", "article_no": "第八十七条"}],
        "cases": [{"case_no": "（2023）京01民终12345号"}],
        "citation_check": "passed",
        "report": "……（一份 3000 字的咨询意见）",
    }
    conf = compute_confidence(state)
    assert conf >= float(settings.QUERY_CACHE_MIN_CONFIDENCE), (
        f"咨询类天花板 {conf} 低于缓存门槛 {settings.QUERY_CACHE_MIN_CONFIDENCE}，"
        "缓存永远写不进去"
    )


def test_report_counts_as_content_but_empty_state_does_not():
    """`report` 是咨询/快速问答的交付物，必须计入内容完整性 —— 但不能变成白送。"""
    from backend.agents.harness.state_manager import compute_confidence

    base = {"statutes": [], "cases": [], "citation_check": "pending"}
    assert compute_confidence({**base, "report": "有内容"}) > \
        compute_confidence({**base}) + 0.09


def test_restamp_confidence_only_touches_its_own_line():
    """报告定稿后改置信度那一行：**整行匹配**，别误伤正文里的百分比。

    正文的金额测算里出现孤立的 ``**50%**`` 完全正常（"违约金可调减至 **50%**"），
    只替换片段会改错地方 —— 改错的是报告里最不能错的那类数字。
    """
    from backend.agents.nodes.report_node import _restamp_confidence

    state = {
        "confidence": 0.5,
        "report": "- 违约金可调减至 **50%** 为宜\n- 综合置信度：**50%**（低于 60% 时须复核）\n",
    }
    _restamp_confidence(state, 0.5, 0.65)
    assert state["confidence"] == 0.65
    assert state["report"] == (
        "- 违约金可调减至 **50%** 为宜\n- 综合置信度：**65%**（低于 60% 时须复核）\n"
    ), state["report"]


def test_restamp_confidence_is_a_noop_without_the_line():
    """直答路径的报告没有置信度段落 —— 找不到就地返回，不该报错。"""
    from backend.agents.nodes.report_node import _restamp_confidence

    state = {"confidence": 0.5, "report": "这是一个直接回答，没有报告模板。"}
    _restamp_confidence(state, 0.5, 0.65)
    assert state["confidence"] == 0.65
    assert state["report"] == "这是一个直接回答，没有报告模板。"
