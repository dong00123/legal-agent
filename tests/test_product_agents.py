"""4 个产品 Agent（侧边栏那 4 个入口）的接线回归测试。

背景：侧边栏对外是 **4 个 agent**（AI 助手 / 类案检索 / 合同审查 / 文书中心），
但 2026-10-07 之前只有合同审查真的走了 ``orchestrator.execute``：
AI 助手打的是纯 RAG 端点、类案检索直接调检索工具、文书中心确定性地套模板。
"4 个 Agent"于是只是界面上的一句话 —— 四个入口里有三个压根没有 agent。

这里钉的四件事**坏了都不会报错**，只会静默地少一块：

1. **产品目录与意图路由对不上**（钉了一个没登记过的意图、或意图的路线里有
   没实现的节点）。症状是"这个页面的结果看起来少了点东西"，日志里一行都没有。
2. **入口端点没真的挂进 app**。前端点下去是一个 404，被 catch 成一句 toast，
   读起来像网络抖动。
3. **调用方钉的意图被分类器覆写**（这正是改动前的实际行为：``RunRequest.intent``
   从来没进过 State）。界面上看不出异常，路由只是"恰好"和你想的不一样。
4. **``intent`` / ``top_k`` 传了等于没传**。类案检索页的「返回条数」就是这样
   假了一整轮：面板里选 3 条还是 20 条，返回值一模一样，且不报错。

前四组都在**不调模型、不连库**的层面上验（注册表级 + 节点级），毫秒级；
端到端的三条在 ``test_api.py`` 里 —— 那个文件的既定做法是不 mock 编排。
"""

from __future__ import annotations

import asyncio

import pytest

from backend.agents.harness import Harness
from backend.agents.harness.router import INTENT_ROUTES, PRODUCT_AGENTS
from backend.agents.nodes import NODE_FUNCS
from backend.agents.nodes.case_node import DEFAULT_TOP_K, MAX_TOP_K
from backend.agents.nodes.intent_node import intent_node
from backend.agents.router import explicit_result
from backend.agents.state import new_state

# 侧边栏那 4 个入口的 id → 界面上的名字。**逐字对齐 index.html 的导航表**：
# 名字对不上时"目录里有 4 个"和"用户看到 4 个"就是两件事。
SIDEBAR = {
    "assistant": "AI 助手",
    "case_search": "类案检索",
    "contract_review": "合同审查",
    "document": "文书中心",
}


# --------------------------------------------------------------------------- 目录
def test_sidebar_has_exactly_four_product_agents() -> None:
    """对外只有 4 个，且名字/页面与侧边栏逐字一致。

    与 ``NODE_SPECS``（11 个节点的声明）是**两套命名空间**，不能合并：节点会随实现
    调整，而对外这 4 个入口是承诺给用户的。合并的后果是"界面上有 11 个 Agent、
    用户只认得 4 个"，且没有任何一处会报错。
    """
    assert set(PRODUCT_AGENTS) == set(SIDEBAR), (
        f"产品 Agent 名单变了：{sorted(PRODUCT_AGENTS)}。"
        "加减入口要连同 index.html 的导航表与这里一起改"
    )
    for aid, name in SIDEBAR.items():
        assert PRODUCT_AGENTS[aid].name == name, f"{aid} 的界面名不是「{name}」"
    assert {s.page for s in PRODUCT_AGENTS.values()} == {
        "pg-ai", "pg-search", "pg-contract", "pg-doc"}


def test_pinned_intents_exist_and_all_nodes_are_implemented() -> None:
    """钉住的意图必须在 ``INTENT_ROUTES`` 里，且路线上的节点都有实现。

    缺一个节点不会在启动时报错，也不会在请求时报 500 —— 流程跑到那一步才算不出
    下一步，然后**静默结束**（``next_node`` 返回 END），产出物看起来只是"少了一段"。
    """
    from backend.agents.protocol.registry import product_health

    health = product_health()
    assert health["ok"], f"产品 Agent 自检没过：{health['problems']}"
    assert health["products"] == 4
    # ``assistant`` 是唯一不钉意图的入口（钉死了等于把分类器关掉）。
    # 单列出来是为了"有一天多出一个不钉意图的入口"能被看见。
    assert health["unpinned"] == ["assistant"]

    for aid, spec in PRODUCT_AGENTS.items():
        if not spec.intent:
            continue
        route = INTENT_ROUTES[spec.intent]
        assert route["nodes"], f"{aid} 钉的意图「{spec.intent}」是条空路线"
        assert not [n for n in route["nodes"] if n not in NODE_FUNCS]


def test_doc_compose_route_never_drafts_or_reports() -> None:
    """文书中心那条路**只到终审为止**，不生成文书、不出报告。

    这是有意的取舍（见 ``doc_compose`` 意图的注释）：交付物由 31 份模板渲染，
    段落形状、``__DELETE__`` 哨兵、Word 导出链路都是确定性的资产，
    让模型再写一遍只会把它们弄丢。有人"顺手"把 DraftNode 加回这条路线时，
    这条用例先红。
    """
    nodes = INTENT_ROUTES["doc_compose"]["nodes"]
    assert "DraftNode" not in nodes, "文书中心的路线里出现了模型起草节点"
    assert "ReportNode" not in nodes, "文书中心的路线里出现了报告汇总节点"
    # 反过来也要钉住：``ValidatorNode`` 是这条路线的**全部意义**，
    # 去掉它这套编排就退化成"白跑一次检索"。
    assert "ValidatorNode" in nodes
    assert "StatuteNode" in nodes


def test_all_product_endpoints_are_really_mounted() -> None:
    """四个入口的端点在 OpenAPI 里必须真的存在。

    ``PRODUCT_AGENTS`` 里那串路径是**字符串**，写错了不会有任何编译期错误；
    前端调过去拿到 404，而 404 在 ``stream()``/``J()`` 里是一句 toast ——
    看起来像网络抖动，不像"这个端点根本没写"。
    """
    from backend.app.main import app

    paths = app.openapi()["paths"]
    for aid, spec in PRODUCT_AGENTS.items():
        assert spec.endpoint in paths, f"{aid} 的端点 {spec.endpoint} 没挂进 app"
        methods = {m.lower() for m in paths[spec.endpoint]}
        assert "post" in methods, f"{aid} 的端点 {spec.endpoint} 不是 POST：{methods}"


# --------------------------------------------------------------------------- 钉意图
def test_explicit_result_rejects_unknown_intent() -> None:
    """不在路由表里的意图返回 ``None`` —— 由调用方决定怎么兜，而不是这里瞎猜。"""
    assert explicit_result("", "随便问问") is None
    assert explicit_result("不存在的意图", "随便问问") is None
    got = explicit_result("case_search", "竞业限制纠纷的裁判倾向")
    assert got is not None
    # 钉住 = 确定，不是"猜得很准"：置信度必须是 1.0、判定方式必须自报家门，
    # 否则事后排障时分不清"判对了"和"被钉住了"。
    assert got.confidence == 1.0 and got.decided_by == "explicit"
    assert got.doc_type == "litigation", "类案检索的材料形态应当推成 litigation"


def _run_intent(query: str, **kw) -> tuple[dict, Harness]:
    state = new_state(query, **kw)
    harness = Harness.start(state, client="test", username="pytest")
    try:
        asyncio.run(intent_node(state, harness))
    finally:
        harness.finish()
    return state, harness


def test_pinned_intent_is_not_reclassified() -> None:
    """**钉住的意图不许被分类器改掉**，哪怕提问看起来完全是另一回事。

    ``case_search`` 这条路线里没有 DraftNode，所以"帮我写一份起诉状"这种提问
    走分类器必然落到 doc_draft —— 用它来验"到底听谁的"最直接。
    改动前的实际行为是：``RunRequest.intent`` 从未进过 State，``IntentNode``
    无条件重跑分类并覆写，传什么都会被盖掉。
    """
    state, _ = _run_intent("帮我写一份离婚起诉状", intent_override="case_search")
    assert state["intent"] == "case_search", (
        "钉住的意图被分类器覆写了 —— 页面点的是类案检索，跑的是别的路线"
    )
    # 走的是**完整**路由，不是动态规划裁剪后的。动态规划的本职是"按问题把路由
    # 裁到够用"，而钉住的入口要的恰恰是完整那条：类案检索页被裁掉 CaseNode
    # 之后就空空如也，且不报任何错。
    # （``state["route"]`` 是 ``plan.nodes()``，入口节点 IntentNode 不在里面）
    assert state["route"] == INTENT_ROUTES["case_search"]["nodes"]
    assert "CaseNode" in state["route"]


def test_unknown_pin_falls_back_to_classifier_and_says_so() -> None:
    """钉了个路由表里没有的意图：**降级可以，静默不行**。

    静默忽略正是"界面说走 A、管线跑了 B"这类问题的成因 —— 事后只看得到
    一份正常的结果，看不出入口那条意图没生效。
    """
    state, _ = _run_intent("试用期最长可以约定多久", intent_override="查无此意图")
    assert state["intent"] != "查无此意图", "未知意图被原样写进了 state"
    assert state["intent"] in INTENT_ROUTES
    reasons = " ".join(str(g.get("reason") or "") for g in state.get("evidence_gap") or [])
    assert "不在路由表里" in reasons, f"降级没有留下缺口说明：{state.get('evidence_gap')}"


# --------------------------------------------------------------------------- top_k
def test_top_k_survives_the_queue_payload_round_trip() -> None:
    """``intent`` / ``top_k`` 必须能过 ``to_payload`` → ``from_payload``。

    排队执行的任务（合同审查那条路走 ``enqueue``）不是直接调 ``execute`` ——
    请求先序列化进任务表，worker 再反序列化出来跑。字段漏在 ``to_payload`` 里
    的话，**同步调用时一切正常、只有排队的那条会丢**，而它跑出来的结果
    看起来同样完整。
    """
    from backend.app.services.orchestrator import RunRequest

    back = RunRequest.from_payload(
        RunRequest(query="竞业限制纠纷", intent="case_search", top_k=12).to_payload())
    assert back.intent == "case_search" and back.top_k == 12


def test_pin_and_top_k_are_folded_into_the_cache_fingerprint() -> None:
    """``intent`` / ``top_k`` 必须进缓存指纹，**值为空时也要占位**。

    这是 ``top_k`` 通路的**下半截**。上半截（``state`` → ``CaseNode``）走通之后，
    面板仍然是假的：缓存靠 ``fingerprint(material_text)`` 做 ``material_fp``，
    而 ``query_cache.lookup`` 对它是**精确比对**。指纹不带 ``top_k`` 时，
    「3 条」那次入库后，「10 条」这次 query 文本一模一样 ⇒ 直接命中 ⇒
    回来还是 3 条。现象与"``top_k`` 压根没通路"完全一致，只有成因不同。

    第二段钉的是**空值也必须占位**，它比第一段更要命。``lookup`` 里
    ``fp = fingerprint(material_text) if material_text else ""`` ——
    "没材料"的请求算出来是**空串**，而 AI 助手 / 类案检索 / 文书中心都不传
    ``material_text``。空串对空串，那道 ``fp != meta.material_fp`` 的闸门
    对它们整个失效：**所有无材料的请求挤进同一个桶**，提问文本一样就互相领走。
    实测过：类案检索页问过的问题在 AI 助手页再问，领回一份 ``case_search`` 的结论，
    ``Route`` 里只剩 ``MemoryNode``（一个分析节点都没跑），界面上写着"命中缓存"。

    所以指纹是**五个维度全写、值空的也写**，并且带 ``FP_VERSION`` 前缀 ——
    改过算法的那一版条目一律不再命中，这是有意作废（见 ``FP_VERSION`` 的注释）。
    """
    from backend.app.services.orchestrator import (
        FP_VERSION,
        RunRequest,
        _cache_material,
    )

    def fp(**kw) -> str:
        return _cache_material(RunRequest(query="q", material_text="合同正文", **kw))

    # 五个维度都在指纹里，空值也占位
    assert fp().startswith(FP_VERSION + "\x00"), "指纹没带版本前缀，旧条目会继续被领走"
    for dim in ("role=", "intent=", "top_k=", "style="):
        assert dim in fp(), f"指纹里少了 {dim} 这一维（空值也必须占位）"

    # 钉住的意图要能区分开（哪怕正文一模一样）
    assert fp(intent="case_search") != fp(), "钉住意图与不钉共用同一个指纹 —— 两个页面会串用缓存"
    assert fp(intent="case_search") != fp(intent="contract_review")

    # 返回条数要能区分开
    assert fp(top_k=3) != fp(top_k=10), "不同返回条数共用同一个指纹 —— 面板选了不生效"
    assert fp(top_k=3) != fp(), "top_k=3 与不传共用指纹，面板第一次就取不到新结果"

    # **没材料的请求不能退化成空指纹** —— 那正是两个页面串用的成因
    no_mat = _cache_material(RunRequest(query="q"))
    assert no_mat, "没材料时指纹为空，闸门失效，所有无材料请求会挤进同一个桶"
    assert no_mat != _cache_material(RunRequest(query="q", intent="case_search"))

    # ``键=值`` 前缀：光写值的话「意图叫 10」会和 ``top_k=10`` 撞在一起
    assert fp(intent="10") != fp(top_k=10)


def test_case_node_uses_state_top_k(monkeypatch: pytest.MonkeyPatch) -> None:
    """``state["top_k"]`` 要原样落到 ``case_search`` 工具调用上，0/缺省用默认值。

    这条钉的是类案检索页的「返回条数」面板。改动前 ``case_node`` 在调用点上
    **写死 ``top_k=5``** —— 面板里选 3 条还是 20 条，命中数一模一样，
    而且不会报错。一个不生效也不报错的面板，比没有这个面板更糟。
    """
    # 取**注册表里那一个**，不是自己 import 一份：``graph`` 调度用的就是
    # ``NODE_FUNCS`` 里的函数，另取一份哪天被替换掉也测不出来。
    # （⚠ ``from backend.agents.nodes import case_node`` 拿到的是**函数**，
    #   它在包的 ``__init__`` 里被重名导入过 —— 当模块用会 AttributeError。）
    case_fn = NODE_FUNCS["CaseNode"]

    seen: list[int] = []

    async def _fake_tool(self, name, agent, **kwargs):  # noqa: ANN001, ANN003
        from backend.tools.base import ToolResult

        if name == "case_search":
            seen.append(kwargs.get("top_k"))
            # 给一条命中就走完，别去跑案号核验（那是另一个工具的活）
            return ToolResult(ok=True, tool=name,
                              data={"cases": [{"case_name": "测试类案"}]}, elapsed_ms=0)
        return ToolResult(ok=True, tool=name, data={"exists": True}, elapsed_ms=0)

    monkeypatch.setattr(Harness, "tool", _fake_tool)

    def _run(**kw) -> int:
        state = new_state("竞业限制纠纷的裁判倾向", doc_type="litigation", **kw)
        state["intent"] = "case_search"  # type: ignore[typeddict-item]
        harness = Harness.start(state, client="test", username="pytest")
        try:
            asyncio.run(case_fn(state, harness))
        finally:
            harness.finish()
        return seen.pop()

    assert _run(top_k=3) == 3, "面板选 3 条没有传到工具调用上"
    assert _run(top_k=20) == 20
    assert _run(top_k=0) == DEFAULT_TOP_K, "不指定条数时应当用默认值"
    assert _run(top_k=999) == MAX_TOP_K, "超过上限要截断到 MAX_TOP_K"
