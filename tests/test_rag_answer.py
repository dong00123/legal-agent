"""纯 RAG 问答（AI 助手页）的引用回溯与追问改写。

**这个文件里的用例全部是纯函数**：不打网络、不连 Milvus、不调模型。
理由不是"跑得快"，而是这些断言要求**确定性** —— 「模型这次恰好没编条文号」
不是一条能回归的结论，只有"给定这段正文与这个证据池，输出必须是这样"才是。

最要紧的两组：

1. **无来源不出引用。** 模型会写参考资料里没有的条文号（实测跑一次就命中了
   《劳动合同法》第四十七条 —— 它是被检索到的第八十七条正文里提到的，但自己没被召回）。
   这些条号必须被剥掉换成"相应条款"，而不是原样留在正文里冒充有出处。
2. **追问只改写检索式，不得把上一轮结论当证据。** 否则"本次结论的依据来自上一个问题"，
   而上一个问题本身没有出处，整条引用链就断了。
"""

from __future__ import annotations

import json
import re

from backend.app.config import settings
from backend.app.services import rag_answer
from backend.llm import prompts

# 库里的写法：全称 + 中文条号。模型常见写法是简称 + 阿拉伯数字，两边必须能对上。
_POOL = rag_answer.pool_of(
    [
        {"doc_name": "中华人民共和国劳动合同法", "article_no": "第四十条",
         "text": "劳动者患病或者非因工负伤，在规定的医疗期满后不能从事原工作……",
         "status": "现行有效", "publisher": "全国人大常委会",
         "source_path": "C:\\secret\\data\\raw\\劳动合同法.pdf", "chunk_id": "s1"},
        {"doc_name": "中华人民共和国合同法", "article_no": "第五十二条",
         "text": "有下列情形之一的，合同无效……", "status": "已废止",
         "source_path": "/app/data/raw/合同法.pdf", "chunk_id": "s2"},
    ],
    [
        {"case_no": "（2023）京01民终12345号", "case_name": "某某诉某某劳动争议案",
         "court": "北京市第一中级人民法院", "gist": "违法解除应支付二倍赔偿",
         "source_path": "D:\\srv\\data\\raw\\case.pdf", "chunk_id": "c1"},
    ],
)


def _find_key(obj, key: str, path: str = "") -> list[str]:
    """递归找键（任意深度），照 ``test_no_path_leak.py`` 的写法。"""
    found: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                found.append(f"{path}.{k}")
            found += _find_key(v, key, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found += _find_key(v, key, f"{path}[{i}]")
    return found


# --------------------------------------------------------------------------- 引用回溯
def test_pool_marker_is_kept_and_becomes_a_card() -> None:
    text, cards, ref = rag_answer.extract_cites("违法解除应当支付二倍赔偿。[S1]", _POOL)

    assert "[S1]" in text, "池内的标记不该被改动"
    assert len(cards) == 1
    assert cards[0]["marker"] == "S1"
    assert cards[0]["type"] == "statute"
    assert cards[0]["status"] == "现行有效", "效力状态必须跟着卡片走，界面要按它上色"
    assert ref["ok"] is True and ref["unsupported"] == []


def test_same_statute_cited_three_times_yields_one_card() -> None:
    """同一法条在正文里出现多次 → **只出一张卡片**，但出现次数要记准。"""
    text, cards, _ = rag_answer.extract_cites(
        "第一处 [S1]，第二处 [S1]，第三处 [S1]。", _POOL)

    assert len(cards) == 1, f"同一法条出多张卡会让参考法规列表重复：{cards}"
    assert cards[0]["occurrences"] == 3
    assert text.count("[S1]") == 3, "正文里的标记不该被合并掉"


def test_out_of_range_marker_is_removed_from_text() -> None:
    """``[S9]`` 池里只有 2 条 → 从正文删掉，并记进 unsupported。

    留着它等于在正文里摆一个点不开的标签 —— 界面说有这条依据，参考法规里找不到。
    """
    text, cards, ref = rag_answer.extract_cites("结论如下。[S9]", _POOL)

    assert "[S9]" not in text
    assert cards == []
    assert ref["ok"] is False
    assert any("超出" in u["reason"] for u in ref["unsupported"])


def test_marker_without_law_name_is_backfilled() -> None:
    """正文写《劳动合同法》第四十条但漏了标记 → 按 ``法规名|条号`` 对齐后回填。

    两种写法都要能对上：库里是「中华人民共和国劳动合同法 / 第四十条」，
    正文可能写简称与阿拉伯数字。
    """
    for mention in ("《劳动合同法》第四十条", "《劳动合同法》第40条",
                    "《中华人民共和国劳动合同法》第四十条"):
        text, cards, ref = rag_answer.extract_cites(f"依据{mention}解除。", _POOL)
        assert "[S1]" in text, f"{mention} 没被回填标记"
        assert ref["backfilled"] == 1
        assert len(cards) == 1, "回填也必须建出卡片，否则标签点不开"


def test_article_outside_pool_is_stripped() -> None:
    """池外的条文号 → 换成「相应条款」。**这是红线「无来源不出引用」的落地。**"""
    text, cards, ref = rag_answer.extract_cites(
        "还应当依据《劳动合同法》第八十五条加付赔偿金。", _POOL)

    assert "第八十五条" not in text, f"池外条文号被留在正文里：{text}"
    assert "相应条款" in text
    assert cards == [], "剥掉条号之后不该再产出引用卡"
    assert any(u["action"] == "stripped_article_no" for u in ref["unsupported"])


def test_non_statute_book_title_is_untouched() -> None:
    """《房屋租赁合同》这类书名号不是法规引用，**不能误伤**。

    误剥比漏剥更伤信任：律师看到一处被改坏的正常表述，整份意见的可信度就没了。
    """
    text, cards, ref = rag_answer.extract_cites("详见《房屋租赁合同》第三条约定。", _POOL)

    assert "第三条" in text and "相应条款" not in text
    assert ref["unsupported"] == []


def test_repealed_status_survives_into_the_card() -> None:
    """已废止的法条要**带着状态**进卡片 —— 界面靠它上警示色，不能吞掉。"""
    _, cards, _ = rag_answer.extract_cites("依据《合同法》第五十二条。[S2]", _POOL)

    assert len(cards) == 1
    assert cards[0]["status"] == "已废止"


def test_case_marker_becomes_a_case_card() -> None:
    _, cards, _ = rag_answer.extract_cites("同类判决见 [C1]。", _POOL)

    assert len(cards) == 1
    assert cards[0]["type"] == "case"
    assert cards[0]["case_no"] == "（2023）京01民终12345号"
    assert "source_path" not in cards[0]


def test_no_source_path_anywhere_in_output() -> None:
    """``source_path`` 不得出现在任何一层 —— 它会暴露服务端目录结构。

    顺带钉住"不就地修改原对象"：``slim()`` 若把传进来的 dict 改了，
    别处（落库快照、Trace）拿到的就是残缺数据。
    """
    text, cards, ref = rag_answer.extract_cites(
        "依据《劳动合同法》第四十条 [S1]，同类见 [C1]，另有 [S2]。", _POOL)
    blob = json.dumps({"text": text, "cards": cards, "ref": ref}, ensure_ascii=False)

    assert _find_key({"cards": cards, "ref": ref}, "source_path") == []
    for leak in ("C:\\secret", "/app/data", "D:\\srv", "data/raw"):
        assert leak not in blob, f"输出里泄漏了路径：{leak}"
    # 原对象必须保持完整
    assert _POOL["statutes"][0]["source_path"].startswith("C:\\secret")


def test_slim_keeps_only_whitelisted_fields() -> None:
    """白名单序列化：**不是"排除 source_path"，而是"只放行这些"**。

    黑名单迟早会漏一个字段出去（命中里现在有 chunk_id，将来会有别的）。
    """
    row = rag_answer.slim([{
        "doc_name": "劳动合同法", "article_no": "第四十条", "status": "现行有效",
        "source_path": "/secret/x.pdf", "vector": [0.1, 0.2], "score": 0.93,
        "internal_debug": "不该出去",
    }], "S")

    assert row == [{"doc_name": "劳动合同法", "article_no": "第四十条", "status": "现行有效"}]


# --------------------------------------------------------------------------- 追问改写
def test_anaphoric_followup_is_rewritten() -> None:
    """「那加班费怎么算」单独拿去检索几乎没有有效关键词，必须补上上一轮。"""
    standalone, rewritten = rag_answer.resolve_followup(
        "那加班费怎么算", [{"q": "公司违法辞退我，2N怎么算", "a": "……"}])

    assert rewritten is True
    assert "2N" in standalone, f"改写后应含上一轮的关键词：{standalone}"
    assert "加班费" in standalone


def test_standalone_question_is_not_rewritten() -> None:
    """完整的新问题不能被改写 —— 拿一个不相关的旧问题去检索会**召回过错误的法条**，
    比召回弱严重得多。判据因此刻意保守。"""
    for q in ("违约金约定过高会被法院调整吗？",
              "竞业限制补偿金的最低标准是多少？",
              "公司没跟我签劳动合同，我能主张哪些权利？"):
        standalone, rewritten = rag_answer.resolve_followup(
            q, [{"q": "公司违法辞退我，2N怎么算", "a": "……"}])
        assert rewritten is False, f"完整问题被判成追问：{q}"
        assert standalone == q


def test_followup_without_history_is_untouched() -> None:
    standalone, rewritten = rag_answer.resolve_followup("那加班费怎么算", [])
    assert rewritten is False and standalone == "那加班费怎么算"


# --------------------------------------------------------------------------- Prompt 装配
def test_history_block_is_marked_as_not_evidence() -> None:
    """历史对话块**必须写明"不是证据"**，否则模型会据上一轮的回答引条文号。"""
    msgs = prompts.build_rag_messages(
        "SYS", "本轮问题", [("S1", "劳动合同法第四十条｜效力状态：现行有效")],
        [{"q": "上一个问题", "a": "上一个答案"}])

    assert [m["role"] for m in msgs] == ["system", "user", "user", "user"]
    assert "【参考资料】" in msgs[1]["content"], "参考资料必须仍在问题之前"
    hist = msgs[2]["content"]
    assert "不是证据" in hist, "历史块没标注它不是证据"
    assert "上一个问题" in hist
    assert msgs[3]["content"] == "本轮问题", "本轮问题必须排在最后"


def test_history_is_capped() -> None:
    """多轮上下文不设限会让 Prompt 线性膨胀，把参考资料挤出模型注意力。"""
    turns = [{"q": f"问题{i}", "a": "答" * 5000} for i in range(6)]
    msgs = prompts.build_rag_messages("SYS", "本轮", [("S1", "x")], turns)
    hist = msgs[-2]["content"]

    assert "问题0" not in hist and "问题3" not in hist, "只应保留最近两轮"
    assert "问题5" in hist
    assert len(hist) < 2000, f"历史块没有被截断：{len(hist)} 字"


def test_answer_prompt_bans_the_report_scaffolding() -> None:
    """四节结构是硬要求，报告脚手架是硬禁止 —— 两者都靠这段 Prompt 落地。"""
    p = prompts.RAG_ANSWER_PROMPT
    for section in ("（一）核心结论", "（二）具体分析", "（三）通俗化输出", "（四）额外思考"):
        assert section in p, f"Prompt 少了章节：{section}"
    for banned in ("引用清单", "证据缺口", "综合置信度"):
        assert banned in p, f"Prompt 没有明确禁止「{banned}」"
    assert "{disclaimer}" in p, "免责声明占位符丢了"


def test_disclaimer_is_forced_when_model_omits_it() -> None:
    """免责声明不能靠模型自觉 —— 法律场景里它是必须项。"""
    out = rag_answer.ensure_disclaimer("结论：可以主张。")

    assert settings.DISCLAIMER in out
    # 已经带了就不重复追加
    assert rag_answer.ensure_disclaimer(out) == out


# --------------------------------------------------------------------------- 端到端（全 mock）
async def _run_answer(monkeypatch, statutes, cases, model_text="结论 [S1]"):
    """跑一遍 ``answer()``，检索与模型都换掉。

    返回 ``(结果, seen)``；``seen["msgs"]`` 是发给模型的消息序列，
    ``seen["kw"]`` 是 ``llm_client.chat`` 实际收到的关键字参数（task / payload）。
    """
    from backend.llm import client as llm_client_mod
    from backend.rag import pipeline

    class _Trace:
        vector_hits = 3
        bm25_hits = 2
        fused = 4
        returned = len(statutes) + len(cases)
        elapsed_ms = 7
        degraded = False
        notes: list = []

    async def _statutes(q, top_k=6):
        return statutes, _Trace()

    async def _cases(q, top_k=5):
        return cases, _Trace()

    class _R:
        text = model_text
        provider = "fake"
        model = "fake"
        elapsed_ms = 1
        usage: dict = {}
        degraded = False

    seen: dict = {}

    async def _chat(msgs, **kw):
        seen["msgs"] = msgs
        seen["kw"] = kw
        return _R()

    monkeypatch.setattr(pipeline, "retrieve_statutes", _statutes)
    monkeypatch.setattr(pipeline, "retrieve_cases", _cases)
    monkeypatch.setattr(llm_client_mod.llm_client, "chat", _chat)

    out = await rag_answer.answer("公司违法辞退我，2N 怎么算")
    return out, seen


async def test_answer_sends_the_rag_contract_to_the_model(monkeypatch) -> None:
    """**必须真的把 ``RAG_ANSWER_PROMPT`` 发出去。**

    这条测试的由来：Prompt 常量写好了、内容单测也过了，但 ``answer()`` 的 system
    传的是 ``LEGAL_SYSTEM_PROMPT`` 单独一段 —— 四节结构与那几条禁令一个字都没到模型那儿，
    实测输出直接长回了报告的样子（``### 一、`` + 「证据缺口提示」）。
    **测"常量内容对"是不够的，要测"它进了消息序列"。**
    """
    _, seen = await _run_answer(monkeypatch, [_POOL["statutes"][0]], [])
    msgs = seen["msgs"]

    system = msgs[0]["content"]
    assert "（一）核心结论" in system, "四节结构没发给模型"
    assert "引用清单" in system, "报告脚手架的禁令没发给模型"
    assert settings.DISCLAIMER in system, "免责声明要求没发给模型"
    assert "【引用纪律】" in system, "通用法务纪律丢了"


async def test_answer_passes_evidence_as_payload(monkeypatch) -> None:
    """``answer()`` 必须把证据装进 ``payload`` 交给 ``llm_client.chat``。

    这是降级路径的**唯一证据通道** —— 规则引擎拿不到 messages 里的结构化字段。

    为什么要直接钉调用契约、而不是只测最终输出：下游还有一道 messages 兜底
    （``test_rule_fallback_recovers_evidence_from_messages``），漏传 payload 时症状
    会被兜底盖住。实测过：把 payload 整段删掉，端到端用例**仍然全绿** ——
    只测输出的话，这条回归可以一路溜过去，直到兜底也失效。
    """
    _, seen = await _run_answer(
        monkeypatch, [_POOL["statutes"][0]], [_POOL["cases"][0]])
    payload = seen["kw"].get("payload")

    assert seen["kw"].get("task") == "rag_answer", "task 名必须与规则引擎的处理器登记一致"
    assert payload, "没有传 payload —— 降级时问题与来源会一起丢"
    assert payload["query"] == "公司违法辞退我，2N 怎么算"
    assert payload["statutes"][0]["article_no"] == "第四十条"
    assert payload["cases"][0]["case_no"] == "（2023）京01民终12345号"
    assert payload["disclaimer"] == settings.DISCLAIMER
    # 送出去的必须是白名单裁过的对象：``source_path`` 一旦进了 payload，
    # 规则引擎把它当正文条款渲染出去，服务端目录结构就跟着回答外泄了。
    assert "source_path" not in payload["statutes"][0]


async def test_answer_reports_degraded_when_retrieval_is_empty(monkeypatch) -> None:
    """检索为空 → 标记降级，**并且明确告诉模型"没有资料，不许写条文号"**。

    模型看不到这句话会用自己的记忆作答，写出的条文号既没有出处也无法回溯。
    """
    out, seen = await _run_answer(monkeypatch, [], [], model_text="未检索到相关规定。")
    msgs = seen["msgs"]

    assert out.degraded is True
    assert out.citations == [] and out.ref_check["total"] == 0
    ctx = msgs[1]["content"]
    assert "未命中" in ctx, "没有把'检索为空'告诉模型"
    assert "不得写出任何条文号" in ctx


async def test_answer_strips_model_invented_article(monkeypatch) -> None:
    """全链路跑一遍编造条文号的处置：模型写了池外的条号 → 正文里被换掉。"""
    out, _ = await _run_answer(
        monkeypatch, [_POOL["statutes"][0]], [],
        model_text="依据《劳动合同法》第八十五条，应当加付赔偿金。")

    assert "第八十五条" not in out.answer
    assert "相应条款" in out.answer
    assert out.ref_check["ok"] is False


# --------------------------------------------------------------------------- 降级路径
async def _run_degraded(monkeypatch, statutes, cases):
    """跑一遍 ``answer()``，检索换掉、**模型走真实降级路径**（规则引擎），返回结果。

    与 :func:`_run_answer` 的区别：那个把 ``llm_client.chat`` 整个换掉（测的是
    "发出去的消息对不对"），这个不换 chat —— 只把 provider 钉死在规则引擎上，
    于是能测到"模型不可用时，这条链路最终交付给用户的是什么"。
    """
    from backend.llm import client as llm_client_mod
    from backend.rag import pipeline

    class _Trace:
        vector_hits = 3
        bm25_hits = 2
        fused = 4
        returned = len(statutes) + len(cases)
        elapsed_ms = 7
        degraded = False
        notes: list = []

    async def _statutes(q, top_k=8):
        return statutes, _Trace()

    async def _cases(q, top_k=4):
        return cases, _Trace()

    monkeypatch.setattr(pipeline, "retrieve_statutes", _statutes)
    monkeypatch.setattr(pipeline, "retrieve_cases", _cases)
    # ``LLM_PROVIDER=mock`` 时候选里只剩 RuleProvider（见 ``LLMClient._candidates``），
    # 于是链路必然降级 —— 与云端 403、断网、额度耗尽时的实际行为一致。
    monkeypatch.setattr(settings, "LLM_PROVIDER", "mock")
    monkeypatch.setattr(llm_client_mod.llm_client, "_provider", None)
    monkeypatch.setattr(llm_client_mod.llm_client, "_selected", "")

    return await rag_answer.answer("违约金约定过高会被法院调整吗？")


async def test_degraded_answer_keeps_question_and_sources(monkeypatch) -> None:
    """模型不可用时，回答必须**保留问题与已检索到的来源**，且不许谎报。

    这条钉住一个实测事故（2026-09-28）：云端网关对整把 key 返回
    ``AccessDenied.Unpurchased``，每次调用都降级 —— 而 ``answer()`` 当时调
    ``llm_client.chat(msgs, task="rag_answer")`` **没传 payload**，规则引擎拿不到
    证据，于是检索命中 8 法条 + 4 类案，界面却显示：

        「针对问题「」，检索结果如下……未检索到相关来源。」

    问题、来源一起丢，还把"有据"说成了"无据"。法律场景里这是最不能接受的一类错误。
    """
    out = await _run_degraded(
        monkeypatch, [dict(_POOL["statutes"][0])], [dict(_POOL["cases"][0])])

    assert out.provider == "rule", "这条用例要测的就是降级路径，没走到就没有意义"
    assert out.degraded is True
    assert "违约金约定过高会被法院调整吗" in out.answer, "问题又变成空的了"
    assert "未检索到相关来源" not in out.answer, "检索明明命中了，却说没检索到"
    assert "中华人民共和国劳动合同法第四十条" in out.answer, "命中的法条没进正文"
    assert "（2023）京01民终12345号" in out.answer, "命中的类案没进正文"


async def test_degraded_answer_carries_markers_so_cards_are_built(monkeypatch) -> None:
    """降级正文要带 ``[Sn]`` / ``[Cn]`` 标记 —— 否则「参考法规」栏是空的。

    ``extract_cites`` 按标记建引用卡；不带标记时正文有依据、卡片栏却没有，
    界面显示成"这个回答没有依据"。标记编号还必须落在证据池范围内，越界会被删。
    """
    out = await _run_degraded(
        monkeypatch, [dict(_POOL["statutes"][0])], [dict(_POOL["cases"][0])])

    assert "[S1]" in out.answer and "[C1]" in out.answer
    assert out.ref_check["unsupported"] == [], "标记越界了（编号与证据池没对齐）"
    markers = sorted(c["marker"] for c in out.citations)
    assert markers == ["C1", "S1"], f"引用卡没建出来：{markers}"
    assert out.citations[0].get("source_path") is None, "卡片里不该有服务端路径"


def test_rule_fallback_strips_the_repeated_law_name_line() -> None:
    """法条正文开头的「《法规名》第X条」要剥掉。

    库里每条法条的 ``text`` 都以法规名+条号开头，而渲染时行首已经有
    ``[S1] 中华人民共和国民法典第五百八十五条`` 了 —— 不剥的话法规名在一行里
    出现两次，而且正文里那个带书名号的写法会被 ``extract_cites`` 当成漏标的引用
    回填一个标记（见下面那条端到端用例）。
    """
    from backend.llm import rule_engine

    text = rule_engine.run_task("rag_answer", [], {
        "query": "违约金过高",
        "statutes": [{"doc_name": "中华人民共和国民法典", "article_no": "第五百八十五条",
                      "status": "现行有效",
                      "text": "《中华人民共和国民法典》第五百八十五条\n【违约金】当事人可以约定违约金。"}],
    })

    assert text.count("第五百八十五条") == 1, f"法规名重复了：{text}"
    assert "【违约金】当事人可以约定违约金。" in text, "正文被剥没了"


async def test_degraded_answer_does_not_double_mark(monkeypatch) -> None:
    """摘录正文里不该出现「同一段文字挂两个编号」。

    ``extract_cites`` 会给"写了《法规名》第X条却没带标记"的正文**补**一个标记 ——
    那是给模型写的正文用的。摘录正文若保留了库里那个法规名行，就会被补出一个标记，
    正文变成 ``- [S1] …第五百八十五条[S1]``，同一段挂上两个号。
    """
    statute = dict(_POOL["statutes"][0])
    statute["text"] = "《中华人民共和国劳动合同法》第四十条\n劳动者患病或者非因工负伤……"
    out = await _run_degraded(monkeypatch, [statute], [])

    markers = re.findall(r"\[([SCK]\d+)\]", out.answer)
    assert markers == ["S1"], f"标记应当只有一个，实际是 {markers}：{out.answer}"


def test_rule_fallback_renders_payload_evidence_without_lies() -> None:
    """给规则引擎的 payload 里有证据时，逐条渲染并带标记。"""
    from backend.llm import rule_engine

    payload = {
        "query": "违约金约定过高会被法院调整吗？",
        "statutes": [{"doc_name": "《中华人民共和国民法典》", "article_no": "第五百八十五条",
                      "status": "现行有效", "text": "约定的违约金过分高于造成的损失的……",
                      "source_path": "C:\\secret\\x.pdf"}],
        "cases": [],
        "disclaimer": settings.DISCLAIMER,
    }
    text = rule_engine.run_task("rag_answer", [], payload)

    assert "违约金约定过高会被法院调整吗" in text
    assert "[S1]" in text
    assert "未检索到相关来源" not in text
    # doc_name 自带的书名号要剥掉：``STATUTE_CITE_RE`` 只认带书名号的写法，
    # 带上就会触发 extract_cites 的"漏标记回填"，在行内再补一个标记。
    assert "《中华人民共和国民法典》" not in text
    assert "C:\\secret" not in text, "source_path 泄漏到了降级输出里"


def test_rule_fallback_recovers_evidence_from_messages() -> None:
    """payload 漏传时，证据还在【参考资料】块里 —— **不许谎报"未检索到"**。

    这是第二道防线：``answer()`` 已经会传 payload，但调用方漏传的代价是
    "把有据说成无据"，而 messages 里本来就有证据，捞得回来就接着渲染。
    """
    from backend.llm import rule_engine

    msgs = prompts.build_rag_messages(
        "SYS", "违约金约定过高会被法院调整吗？",
        [("S1", "中华人民共和国民法典第五百八十五条｜效力状态：现行有效\n条文：约定的违约金过分高于……")],
        None)
    text = rule_engine.run_task("rag_answer", msgs, {})

    assert "违约金约定过高会被法院调整吗" in text, "问题没从 messages 里兜回来"
    assert "未检索到相关来源" not in text, "证据就在参考资料块里，却说没检索到"
    assert "[S1]" in text


def test_rule_fallback_says_no_source_only_when_truly_empty() -> None:
    """两种"空"要分开表述：真没检索到才说没检索到。

    检索为空时如实告知是**正确行为**（红线：无来源不出引用），不能因为上面几条
    测试就把它一并去掉。
    """
    from backend.llm import rule_engine

    msgs = prompts.build_rag_messages("SYS", "一个没有资料的问题", [("无", "本次检索未命中任何法条或类案。")], None)
    text = rule_engine.run_task("rag_answer", msgs, {"query": "一个没有资料的问题"})

    assert "未检索到相关来源" in text
    assert "一个没有资料的问题" in text
