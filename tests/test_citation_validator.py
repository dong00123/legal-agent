"""引用终审（Citation Validator）回归测试。

这是整个项目最不能出错的一环 —— 方案 8.3 的三条反幻觉红线里，
「无来源不出引用」与「时效性校验」都在这里落地。
所以本文件刻意**正反两组用例都给**：
只跑"该拦的拦住了"不够，还要跑"不该拦的没拦"，
否则把判据放宽到 `return True` 也能全绿。

覆盖的缺陷（都曾真实存在）：
1. ``ValidationResult.blocking`` 恒为 0 —— ``passed`` 永远 True，终审"看着在拦、实际从不拦截"；
2. 引用收集只扫 draft/report/risk_points，**咨询与案件分析的引用全在 strategy 里**，
   那一整类任务的引用从未被核验；
3. 引用收集只认带书名号的写法，而律师正文里最常写的是「劳动合同法第八十七条」；
4. 「上述规定第3条」这类行文回指被当成引用 —— 误拦比漏拦更快摧毁信任。
"""

from __future__ import annotations

import pytest

from backend.agents.harness.validator import (
    BARE_STATUTE_RE,
    CitationValidator,
    ValidationResult,
    _looks_like_statute,
    _plain,
    _as_text_list,
)


# --------------------------------------------------------------------------- 桩
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


class _FakeInvoker:
    """所有校验工具都"不可用" —— 专门用来验证降级路径不会误拦截。"""

    async def call(self, *a, **kw):
        from backend.tools.base import ToolResult

        return ToolResult(ok=False, tool=(a[0] if a else ""), error="stub: 工具不可用",
                          degraded=True)


def _validator(state: dict) -> CitationValidator:
    return CitationValidator(_FakeCtx(), state, _FakeInvoker())  # type: ignore[arg-type]


def _mentions(state: dict) -> list[dict]:
    return _validator(state)._collect_mentions()


def _keys(state: dict) -> set[str]:
    out: set[str] = set()
    for m in _mentions(state):
        if m["kind"] == "statute":
            out.add(f"{m['name']}|{m.get('article', '')}")
        else:
            out.add(f"case|{m.get('case_no') or m.get('name') or ''}")
    return out


# --------------------------------------------------------------------------- 判据
@pytest.mark.parametrize("name,expected", [
    # 结尾是法规体裁词 → 按法规处理（哪怕含「合同」这个文档提示词）
    ("合同法", True),
    ("劳动合同法", True),
    ("中华人民共和国民法典", True),
    ("女职工劳动保护特别规定", True),
    ("最高人民法院关于适用民法典合同编通则若干问题的解释", True),
    # 文档体裁 → 不是法规
    ("房屋租赁合同", False),
    ("买卖合同（模板）", False),
    ("法律意见书", False),
    ("尽职调查报告", False),
    ("还款承诺函", False),
    ("证据清单", False),
    # 过短的名字一律不认
    ("合同", False),
    ("", False),
])
def test_looks_like_statute(name: str, expected: bool) -> None:
    assert _looks_like_statute(name) is expected


@pytest.mark.parametrize("raw,expected", [
    ("《中华人民共和国劳动合同法》", "劳动合同法"),
    ("《最高人民法院关于适用民法典的解释》", "关于适用民法典的解释"),
    ("劳动合同法", "劳动合同法"),
    ("  《民法典》  ", "民法典"),
])
def test_plain_strips_prefix(raw: str, expected: str) -> None:
    assert _plain(raw) == expected


# --------------------------------------------------------------------------- 引用抽取
def test_collects_bracketed_statute() -> None:
    state = {"draft": "依据《中华人民共和国劳动合同法》第八十七条，应当支付赔偿金。"}
    assert "劳动合同法|第87条" in _keys(state)


def test_collects_bare_statute_in_body() -> None:
    """律师正文里最常见的写法 —— 不带书名号。"""
    state = {"draft": "根据劳动合同法第八十七条的规定，用人单位应当支付赔偿金。"}
    assert "劳动合同法|第87条" in _keys(state)


def test_collects_bare_statute_with_full_prefix() -> None:
    state = {"draft": "依中华人民共和国劳动合同法第87条，赔偿标准为二倍。"}
    assert "劳动合同法|第87条" in _keys(state)


def test_collects_from_strategy_field() -> None:
    """咨询与案件分析的交付物在 strategy 里 —— 这一处此前完全不扫。"""
    state = {
        "intent": "consult",
        "strategy": ["我方攻点：以 中华人民共和国劳动法第21条 作为请求权基础。"],
    }
    assert "劳动法|第21条" in _keys(state)


def test_collects_from_structured_basis() -> None:
    state = {
        "issues": [{"title": "试用期", "basis": ["中华人民共和国劳动合同法第83条"]}],
        "limitation_check": {"basis": ["《劳动争议调解仲裁法》第二十七条"]},
    }
    keys = _keys(state)
    assert "劳动合同法|第83条" in keys
    assert "劳动争议调解仲裁法|第27条" in keys


def test_collects_case_no() -> None:
    """案号键会剥掉括号 —— 与证据池用同一个 ``_norm_case_no``，全半角括号必须归一。"""
    state = {"report": "参见（2023）京01民终12345号民事判决书。"}
    assert "case|2023京01民终12345号" in _keys(state)


def test_case_no_fullwidth_and_halfwidth_same_key() -> None:
    """全角与半角括号写法不同，但必须是同一条引用。"""
    a = _keys({"draft": "参见（2023）京01民终12345号。"})
    b = _keys({"draft": "参见(2023)京01民终12345号。"})
    assert a == b


def test_ignores_non_statute_document() -> None:
    """《房屋租赁合同》第三条是合同条款，不是法规引用。"""
    state = {"draft": "《房屋租赁合同》第三条约定的租金支付方式如下。"}
    assert not _keys(state)


@pytest.mark.parametrize("text", [
    "上述规定第3条应当优先适用",          # 行文回指
    "前述办法第2条另有约定的除外",
    "本法第5条",                          # 自指
    "相关条例第12条",
    "根据法律规定办理",                    # 没有条号
    "本合同第8条约定的违约金过高",
    "《XX协议》第2条",
])
def test_no_false_positive_on_back_references(text: str) -> None:
    assert not _keys({"draft": text}), f"误拦了正常行文：{text}"


def test_bare_regex_does_not_double_match_bracketed() -> None:
    """带书名号的写法不能被裸写法正则再匹配一次，否则同一引用会重复入账。"""
    text = "依据《劳动合同法》第八十七条"
    assert len(BARE_STATUTE_RE.findall(text)) == 0


def test_dedupes_same_citation_written_two_ways() -> None:
    """同一个引用用两种写法出现，只能算一条。"""
    state = {"draft": "《劳动合同法》第八十七条与劳动合同法第87条表述一致。"}
    assert len(_mentions(state)) == 1


# --------------------------------------------------------------------------- 结果不变式
def test_blocking_invariant() -> None:
    """``passed`` 必须等价于「没有任何拦截」。

    这条不变式是整套终审的地基：``passed`` 恒为 True 时，
    ``citation_check`` 永远写 "passed"，下游全按"通过"处理，
    而日志里却明明有拦截记录 —— 最危险的一类静默失效。
    """
    r = ValidationResult()
    assert r.passed is True
    CitationValidator._reject(r, "第一条不可信引用")
    assert r.blocking == 1
    assert r.passed is False
    CitationValidator._reject(r, "第二条不可信引用")
    assert r.blocking == 2
    assert len(r.rejected) == 2
    assert r.to_summary()["passed"] is False


async def test_tool_unavailable_does_not_block() -> None:
    """法规库/类案库不可用 ≠ 引用是假的。工具抖动不该让整个产品无法出结论。"""
    state = {
        "draft": "依据《中华人民共和国劳动合同法》第八十七条。",
        "statutes": [{"doc_name": "中华人民共和国劳动合同法", "article_no": "第八十七条",
                      "status": "现行有效"}],
        "cases": [],
    }
    result = await _validator(state).validate()
    # 池里有这条证据 → 可溯源；工具不可用只应降级提示，不应拦截
    assert result.blocking == 0, f"工具不可用却产生了拦截：{result.rejected}"
    assert result.passed is True


async def test_out_of_pool_citation_is_blocked() -> None:
    """红线一：正文引用了本次检索证据池里没有的条文 → 必拦。"""
    state = {
        "draft": "依据《中华人民共和国劳动合同法》第九十九条的规定……",
        "statutes": [{"doc_name": "中华人民共和国劳动合同法", "article_no": "第八十七条",
                      "status": "现行有效"}],
    }
    result = await _validator(state).validate()
    assert result.passed is False
    assert result.blocking >= 1
    assert any("第九十九条" in r or "99" in r for r in result.rejected), result.rejected


# --------------------------------------------------------------------------- 辅助
@pytest.mark.parametrize("value,expected", [
    (["《民法典》第585条"], ["《民法典》第585条"]),
    ([{"name": "劳动合同法", "article": "第83条"}], ["劳动合同法 第83条"]),
    ([{"doc_name": "劳动合同法", "article_no": "第八十三条"}], ["劳动合同法 第八十三条"]),
    ([None, "", "有效"], ["有效"]),
    ([], []),
])
def test_as_text_list(value, expected) -> None:
    assert _as_text_list(value) == expected


# --------------------------------------------------------------------------- knowledge 引用回溯
async def test_knowledge_out_of_pool_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """红线一扩展：knowledge 引用必须回溯本次 knowledges 证据池。

    「标题不在池中 = 无来源引用」与 statute / case 同口径、必拦 ——
    真机上《社会保险条例》这类本地语料不存在的"指引"就是被这道闸拦下。
    """
    state = {"knowledges": [{"title": "劳动合同解除指引", "doc_type": "办案指引"}]}
    v = _validator(state)
    fake_mentions = [
        {"kind": "knowledge", "name": "社会保险条例解读", "raw": "社会保险条例解读"},
        {"kind": "knowledge", "name": "劳动合同解除指引", "raw": "劳动合同解除指引"},
    ]
    monkeypatch.setattr(v, "_collect_mentions", lambda: fake_mentions)  # type: ignore[method-assign]
    result = await v.validate()
    assert result.blocking >= 1
    assert any("社会保险条例解读" in r and "无来源引用" in r
               for r in result.rejected), result.rejected
    # 池内标题必须**不被拦**（正反两组都给，防止把判据放宽到恒拦截）
    assert not any("劳动合同解除指引" in r for r in result.rejected)
    kinds = [c.get("type") for c in result.citations]
    assert kinds.count("knowledge") == 1
    assert all(c.get("verified") is False for c in result.citations
               if c.get("type") == "knowledge")
