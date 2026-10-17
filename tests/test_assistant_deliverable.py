"""AI 助手交付物（**对话正文＋报告尾巴**）的形状回归。

背景：2026-10-07 助手页从 ``/chat/rag`` 换到 Agent 链路后，正文换成了
``consult`` 报告的七节骨架（结论摘要/分析说明/引用清单/…），律师要的是
**图 2 那种好读的四节对话**（（一）核心结论 /（二）具体分析 /（三）通俗化输出 /
（四）额外思考 + 金额测算表），同时保留**图 1 那种合规骨架**（引用清单 /
证据缺口 / 置信度 / 免责声明）。于是有了 ``answer_style="conversational"``。

这个文件钉的是**合并后最容易静默坏掉的四类东西**：

1. **编号**。四节的编号由模型写，代码这边只能出无编号标题 ——
   一旦有人顺手喂个 ``_next("（二）")`` 进去，"（二）"会变成后面每一节的名字
   （全角括号让 ``order.find`` 返回 -1，函数原样返回）。
2. **``[S1]`` / ``[K1]`` 与前端卡片编号对齐**。前端按数组下标造卡片，
   这里按数组下标写块标签；谁把 ``continue`` 改成"先过滤再编号"就整体错位，
   症状是点 ``[S3]`` 跳到另一部法 —— 静默、且看起来像"检索不准"。
3. **降级不冒充分析**，但**金额照样给**（``damages_calc`` 是纯函数，不依赖模型）。
4. **形态进缓存指纹**。不进的话，同一条 query 先被合同审查页跑过，
   助手页再问会静默领回一份报告型交付物。
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

from backend.agents.harness import Harness
from backend.agents.state import new_state

report_mod = importlib.import_module("backend.agents.nodes.report_node")

QUERY = "公司违法解除，月均 8500，2019-03-01 入职、2026-10-07 离职，能拿多少"
RULE_REPORT = "# 法律辅助分析意见（consult）\n\n## 一、结论摘要\n- 共检索到 11 条相关法条。\n"


def _ready(style: str = "conversational") -> tuple[dict, Harness]:
    state = new_state(QUERY, answer_style=style)
    state["intent"] = "consult"          # type: ignore[typeddict-item]
    state["statutes"] = [
        {"doc_name": "劳动合同法", "article_no": f"第{i}条", "status": "现行有效",
         "text": "" if i == 4 else f"第{i}条的正文。"}
        for i in range(1, 13)
    ]
    state["knowledges"] = [
        {"title": f"指引{i}", "doc_type": "办案指引", "version": f"2026-0{i}",
         "snippet": f"指引{i}的片段。"} for i in range(1, 4)
    ]
    harness = Harness.start(state, client="test", username="pytest")
    return state, harness


@pytest.fixture
def fake_chat(monkeypatch: pytest.MonkeyPatch):
    """替换模型调用（与 ``test_report_assembly`` 同一套做法）。"""
    from backend.llm.client import LLMResult

    box: dict = {"result": LLMResult(text="", provider="openai", model="fake")}

    async def _fake_chat(messages, **kwargs):  # noqa: ANN001, ANN003
        box["messages"] = messages
        return box["result"]

    async def _available() -> bool:
        return True

    monkeypatch.setattr(report_mod.llm_client, "chat", _fake_chat)
    monkeypatch.setattr(report_mod, "_llm_available", _available)
    return box


def _build(state: dict, harness: Harness) -> str:
    return asyncio.run(report_mod._build_report(state, harness))


# --------------------------------------------------------------------------- 编号
def test_next_rejects_fullwidth_paren() -> None:
    """``_next("（二）")`` **原样返回** —— 这就是"尾巴不许调 ``_next``"的根因。

    钉住它是因为这个行为**看起来像 bug、实际是设计**：全角括号不在
    ``"一二三四五六七八九十"`` 里，``order.find`` 给 -1，函数走 else 分支返回原值。
    谁"顺手修好"它，反而会让对话式尾巴的第一节编号变成「（三）」。
    """
    assert report_mod._next("（二）") == "（二）"
    assert report_mod._next("二") == "三"


def test_conversational_tail_has_no_chinese_numbering(fake_chat) -> None:
    from backend.llm.client import LLMResult

    state, harness = _ready()
    fake_chat["result"] = LLMResult(text="（一）核心结论\n结论。\n\n（二）具体分析\n分析。",
                                    provider="openai", model="fake")
    try:
        out = _build(state, harness)
    finally:
        harness.finish()
    for sec in ("一", "二", "三", "四", "五"):
        assert f"## {sec}、" not in out, f" tails 里出现了中文编号「{sec}、」"
    for head in ("## 引用清单", "## 证据缺口与不确定性", "## 置信度"):
        assert head in out, head


# --------------------------------------------------------------------------- 编号对齐
def test_marker_index_aligned_with_frontend_card_index() -> None:
    """第 4 条没有正文（被跳过）时，第 5 条仍然必须是 ``[S5]``。

    前端 ``answerShape`` 按 ``data.statutes`` 的**数组下标**造卡片
    （``marker='S'+(i+1)``）。块标签若不跟数组下标走，点 ``[S5]`` 会跳到第 4 条上
    —— 而第 4 条恰好是空的，律师看到的是"点开什么都没有"。

    （块只取前 10 条：``S11``/``S12`` 不在参考资料里是**有意的截断**，
    不是错位 —— 它们只是模型看不见，卡片仍在界面上。）
    """
    state, harness = _ready()
    try:
        blocks = dict(report_mod._report_context_blocks(state))
    finally:
        harness.finish()
    assert "S4" not in blocks, "第 4 条没有正文，不该出块"
    assert set(t for t in blocks if t.startswith("S")) == {
        "S1", "S2", "S3", "S5", "S6", "S7", "S8", "S9", "S10"}
    # 每个标签都要对着**同下标**的那一条（错位的话这里会红）
    for i in (1, 5, 10):
        assert f"第{i}条的正文" in blocks[f"S{i}"], f"S{i} 指到了别的条文上"


def test_knowledges_are_numbered_for_the_cards(fake_chat) -> None:
    """知识库条目必须有 ``K{i}`` 块：前端会给它们造卡片，模型看不见编号就会瞎编。"""
    state, harness = _ready()
    try:
        blocks = dict(report_mod._report_context_blocks(state))
    finally:
        harness.finish()
    assert "K1" in blocks and "指引1" in blocks["K1"]
    assert set(("K1", "K2", "K3")) <= set(blocks)


# --------------------------------------------------------------------------- 金额
def test_calc_block_title_matches_prompt() -> None:
    """``CALC_BLOCK_TITLE`` 必须**逐字**出现在提示词里。

    提示词写的是"金额一律照抄「法定金额测算」块"。名字对不上时，模型在参考资料里
    找不到那个块，于是**自己估一个数**写进表格 —— 而它看起来完全正常。
    """
    from backend.llm.prompts import ASSISTANT_REPORT_PROMPT, REPORT_PROMPT

    assert report_mod.CALC_BLOCK_TITLE in ASSISTANT_REPORT_PROMPT
    # 报告形态（类案检索/合同审查/案件分析三个页面在用）同样要照抄，
    # 否则它自己折算出另一套数：真机 2026-10-07 一份报告里
    # 核心结论 153,000、正文 136,000，还自带一句"自我校对"。
    assert report_mod.CALC_BLOCK_TITLE in REPORT_PROMPT
    # 实测踩过：块标签就是 ``[T1]``（``build_messages`` 拼的），模型看见就照着
    # ``[S1]`` 的样子把它写进了正文 —— 而界面上没有 T 卡，``citeChip`` 对未知
    # 标记**原样返回**，律师看到的是满篇裸的 ``[T1]``。所以提示词里必须点名禁掉它。
    assert "[T1]" in ASSISTANT_REPORT_PROMPT


def test_stray_block_markers_are_stripped_from_the_body(fake_chat) -> None:
    """``[T1]`` 这类**没有卡片**的块标签必须由代码删掉，``[S1]``/``[K1]`` 必须留着。

    提示词里已经逐字禁掉 ``[T1]``，实测模型照写不误（2026-10-07，一次正文里 7 处）——
    所以这里钉的是 ``_clean_body`` 的兜底，而不是提示词。删错方向（顺手把 ``[S1]``
    也过滤掉）更糟：法条卡片点不动了，而且是静默的。
    """
    from backend.llm.client import LLMResult

    body = ("（一）核心结论\n应付 136,000 元 [T1]，依据《劳动合同法》第四十七条 [S1]。\n\n"
            "| 项目 | 金额 |\n| --- | --- |\n| 赔偿金 | 136,000 [T1] |\n\n"
            "（二）具体分析\n见本所指引 [K1]。")
    state, harness = _ready()
    fake_chat["result"] = LLMResult(text=body, provider="openai", model="fake")
    try:
        out = _build(state, harness)
    finally:
        harness.finish()
    assert "[T1]" not in out and "[T0" not in out, "没有卡片的块标签漏进正文了"
    assert "[S1]" in out, "[S1] 是有卡片的，不能一起删"
    assert "[K1]" in out, "[K1] 是有卡片的，不能一起删"
    assert "136,000" in out, "只删标记，钱数要留着"
    assert "136,000  |" not in out, "删标记后不该留下双空格"


def test_calc_block_carries_the_tool_total_verbatim(fake_chat) -> None:
    """``T1`` 块里的金额必须与 ``damages_calc`` 返回的 ``total`` 逐位一致。

    这是验收口径的单元版：**表格里的数只能从这里抄**。
    """
    from backend.tools.calc_tools import damages_calc

    state, harness = _ready()
    try:
        calc = asyncio.run(report_mod._calc_block(state, harness))
    finally:
        harness.finish()
    assert calc is not None and calc[0] == "T1"
    ref = asyncio.run(damages_calc(scenario="termination_bundle", monthly_wage=8500,
                                   start_date="2019-03-01", end_date="2026-10-07"))
    assert ref.ok
    assert report_mod._yuan(float(ref.data["total"])) in calc[1], calc[1]
    assert "互为备选" in calc[1], "三行是备选不是相加，这句话必须跟着数字走"


def _calc_gaps(state: dict) -> list[dict]:
    """只看**本工具**登记的那几条缺口（Harness 自己还会记置信度之类的，别混进来）。"""
    return [g for g in (state.get("evidence_gap") or []) if g.get("kind") == "calc"]


def test_calc_block_is_silent_when_there_is_nothing_to_compute(fake_chat) -> None:
    """纯概念题：不算、**也不记缺口**（记了就是往「证据缺口」里灌噪音）。"""
    state, harness = _ready()
    state["query"] = "试用期最长可以约定多久"
    try:
        assert asyncio.run(report_mod._calc_block(state, harness)) is None
    finally:
        harness.finish()
    assert _calc_gaps(state) == []


def test_calc_gap_is_recorded_when_facts_are_missing(fake_chat) -> None:
    """问的是赔偿却缺要素 ⇒ **如实记一条缺口**，而不是静默不给数。"""
    state, harness = _ready()
    state["query"] = "公司违法辞退我，我能拿多少赔偿"
    try:
        assert asyncio.run(report_mod._calc_block(state, harness)) is None
    finally:
        harness.finish()
    gaps = _calc_gaps(state)
    assert gaps and "月工资" in (gaps[0].get("reason") or ""), gaps


# --------------------------------------------------------------------------- 降级
def test_degraded_body_is_labelled_not_nested(fake_chat) -> None:
    """降级：标注清楚 + 不套第二份报告 + **金额照样给**。"""
    from backend.llm.client import LLMResult

    state, harness = _ready()
    fake_chat["result"] = LLMResult(text=RULE_REPORT, provider="rule",
                                    model="rule-based-legal-engine", degraded=True)
    try:
        out = _build(state, harness)
    finally:
        harness.finish()
    assert "法律辅助分析意见" not in out, "规则引擎那篇整份报告被当成正文了"
    assert "不是分析意见" in out, "降级必须明确标注，不能冒充分析"
    assert "136,000" in out, "金额是代码算的，模型不在了也必须给出来"


def test_swallowed_full_report_is_dropped(fake_chat) -> None:
    from backend.llm.client import LLMResult

    state, harness = _ready()
    fake_chat["result"] = LLMResult(text=RULE_REPORT, provider="openai", model="fake")
    try:
        out = _build(state, harness)
    finally:
        harness.finish()
    assert "法律辅助分析意见" not in out
    assert "## 引用清单" in out, "正文丢了，合规尾巴仍要在"


def test_four_sections_written_twice_keeps_only_the_first(fake_chat) -> None:
    from backend.llm.client import LLMResult

    twice = ("（一）核心结论\n第一遍。\n\n（二）具体分析\n第一遍分析。\n\n"
             "（一）核心结论\n第二遍。\n\n（二）具体分析\n第二遍分析。")
    state, harness = _ready()
    fake_chat["result"] = LLMResult(text=twice, provider="openai", model="fake")
    try:
        out = _build(state, harness)
    finally:
        harness.finish()
    assert "第一遍分析" in out and "第二遍分析" not in out


# --------------------------------------------------------------------------- 形态开关
def test_missing_style_defaults_to_report(fake_chat) -> None:
    """没有 ``answer_style`` 的历史任务重放**必须**还是报告形态。"""
    from backend.llm.client import LLMResult

    state, harness = _ready(style="report")
    del state["answer_style"]          # 模拟旧快照
    fake_chat["result"] = LLMResult(text="围绕违法解除的赔偿金，可分三步主张……",
                                    provider="openai", model="fake")
    try:
        out = _build(state, harness)
    finally:
        harness.finish()
    assert "# 法律辅助分析报告" in out and "## 一、结论摘要" in out


def test_answer_style_is_folded_into_the_cache_fingerprint() -> None:
    """交付物形态不同 ⇒ 指纹必须不同，否则两个页面会串用缓存。"""
    from backend.app.services.orchestrator import RunRequest, _cache_material

    report = _cache_material(RunRequest(query="q", answer_style="report"))
    conv = _cache_material(RunRequest(query="q", answer_style="conversational"))
    assert report != conv, "形态没进指纹：助手页会领回一份报告型交付物"
    assert _cache_material(RunRequest(query="q")) == report, "缺省必须是 report"


def test_report_style_still_gets_the_seventeen_sections(fake_chat) -> None:
    """报告形态那几节（风险点/时效/攻防）**一节都不能少** —— 另外三个页面在用。"""
    from backend.llm.client import LLMResult

    state, harness = _ready(style="report")
    state["strategy"] = ["先固定解除通知的送达证据"]
    state["limitation_check"] = {"period": "一年", "start_point": "解除之日",
                                 "conclusion": "未过时效", "basis": ["调解仲裁法第27条"]}
    fake_chat["result"] = LLMResult(text="（一）核心结论\n结论。", provider="openai",
                                    model="fake")
    try:
        out = _build(state, harness)
    finally:
        harness.finish()
    assert "攻防要点与策略" in out and "诉讼/仲裁时效核查" in out


# --------------------------------------------------------------------------- 证据不足（反幻觉）
def test_empty_evidence_skips_llm_and_labels_retrieval_gap(fake_chat) -> None:
    """证据池为空：**不调用 LLM**，直接走降级，如实声明未检索到依据。

    真机事故（问「我们的保险合同包括哪些」被答成社会保险/五险一金、还编造
    《环境保护法》《社会保险条例》）的单元版：模型在零证据下没有机会编结论。
    """
    state, harness = _ready()
    state["statutes"] = []
    state["knowledges"] = []
    state["cases"] = []
    try:
        out = _build(state, harness)
    finally:
        harness.finish()
    assert "messages" not in fake_chat, "证据池为空时不该调用模型"
    assert "未检索到相关依据" in out, out[:400]
    assert "检索结果的结构化汇总" in out, out[:400]
    assert "不是分析意见" in out, out[:400]
    gaps = state.get("evidence_gap") or []
    assert any(g.get("kind") == "analysis" and "未检索到相关法规" in (g.get("reason") or "")
               for g in gaps), gaps


def test_scarce_evidence_injects_one_line_hint(fake_chat) -> None:
    """证据非空但覆盖率极低（≤2 条）时，Prompt 尾部注入一行提示。

    不新增章节、不改四节正文结构 —— 注入的是提示词里的一行，不是正文的一节。
    """
    state, harness = _ready()
    state["statutes"] = [state["statutes"][0]]  # 只剩 1 条有效法条
    state["knowledges"] = []
    state["cases"] = []
    try:
        _build(state, harness)
    finally:
        harness.finish()
    joined = " ".join(str(m.get("content") or "") for m in fake_chat["messages"])
    assert "参考资料有限" in joined, joined[:500]
    assert "不得自行补充依据" in joined, joined[:500]
