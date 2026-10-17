"""KnowledgeNode：律所知识库检索（方案 4.1 Knowledge Agent）。

方案对 Knowledge Agent 的约束只有一句话：**标注指引版本**。
这句话值得单独一个节点，是因为「指引不标版本」是个很隐蔽的坑：
律师看到「按本所《买卖合同审查清单》第 3 条」会默认那是当前有效的作业标准，
但如果清单去年改过版，他可能正在用已经废弃的标准做尽调。

所以本节点做三件事：
1. 检索知识库（走 ``knowledge_search`` MCP 工具，与法条/类案同一条通道）；
2. **对版本缺失的条目独立记证据缺口** —— 不是把 ``degraded`` 笼统标一下，
   而是明确写"这几份指引未标版本，引用前请确认是否最新"；
3. 把检索到的模板/清单片段回填到 State，供 ReviewNode 的逐条扫描与
   DraftNode 的格式规范化使用。

与 StatuteNode 的关键差别：知识库**不是法定证据**，只影响"怎么做"，
不影响"依据什么"。所以本节点失败时降级为"按通用作业标准执行"，
不阻断流程，也不得进入引用清单。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from backend.agents.harness import Harness
from backend.agents.retrieval_policy import effective_domain, enrich_query
from backend.agents.state import LegalState, focus_of

logger = logging.getLogger("legal_agent.nodes.knowledge")

AGENT = "knowledge"

# 不同意图关心的知识库文档类型：查什么类型的指引由意图决定，
# 而不是把所有指引都捞回来（否则 top-k 会被无关模板占满）。
#
# **取值必须是索引侧的机器码（checklist / guideline / template），不能写中文标签。**
# 这里曾经写的是「审查清单」「办案指引」，而入库时 ``KnowledgeSource.doc_type``
# 存的是 "checklist" / "guideline" —— 过滤条件永远不命中，
# 表现为一句看起来完全正常的降级提示「本所知识库未检索到相关指引或审查清单」，
# 于是每个任务都带着一条假的证据缺口，而知识库其实装满了。
# 「两侧取值同名不同形」是静默失效里最难查的一类：日志、结果、格式全都正常。
DOC_TYPE_BY_INTENT: dict[str, str] = {
    "contract_review": "checklist",
    "doc_draft": "guideline",
    "case_analysis": "guideline",
    "consult": "guideline",
}

# 导入时自检：取值必须与建索引侧一致。宁可启动即报错，也不要静默空转。
from backend.corpus.guidelines import DOC_TYPES as _INDEX_DOC_TYPES  # noqa: E402

_unknown = sorted(set(DOC_TYPE_BY_INTENT.values()) - set(_INDEX_DOC_TYPES))
if _unknown:  # pragma: no cover - 配置写错才会走到
    raise RuntimeError(
        f"KnowledgeNode 的 doc_type 取值与索引侧不一致：{_unknown}；"
        f"索引侧可用值：{sorted(_INDEX_DOC_TYPES)} —— "
        f"不一致会导致 doc_type 过滤永远不命中，知识库检索静默失效"
    )


async def knowledge_node(state: LegalState, harness: Harness) -> LegalState:
    """律所知识库检索 + 版本标注。"""
    harness.step("KnowledgeNode")

    # A2A 委派方（如 AnalysisNode 发现没取到本所办案指引）可指定**专项检索式**。
    # 就地读、不弹出 —— 理由同 ``agents.state.focus_of``。
    focus = focus_of(state)
    queries = [focus] if focus else harness.queries_for("KnowledgeNode")
    queries = [enrich_query(state, q) for q in queries]
    if not queries:
        harness.gaps("knowledge", "无可用检索式，未检索本所知识库",
                     "本次结论未参考本所作业标准与审查清单")
        return state

    doc_type = DOC_TYPE_BY_INTENT.get(state.get("intent") or "", "")
    domain = effective_domain(state)
    hits: list[dict[str, Any]] = []
    version_notes: list[str] = []

    # 节点内多条检索式彼此独立（都只是往证据池追加命中），并行执行：
    # 串行实测 knowledge_search 三条要 91+28+11=130s（还叠加了 30s 超时重试），
    # 并行后整体耗时收敛到最慢一条，且互不影响命中结果与合并顺序
    # （_dedupe_by_title 与 harness.merge 都是集合语义，无顺序依赖）。
    if harness.invoker.handler.should_skip("knowledge_search"):
        harness.tracer.warn("KnowledgeNode", "skip", "知识库检索工具已熔断，停止后续检索")
    else:
        results = await asyncio.gather(
            *(
                harness.tool("knowledge_search", AGENT, query=q, top_k=5,
                             doc_type=doc_type, domain=domain)
                for q in queries
            ),
            return_exceptions=True,
        )
        for q, res in zip(queries, results):
            if isinstance(res, Exception):
                harness.gaps("knowledge", f"知识库检索失败：{res}",
                             "本次按通用作业标准执行，未对照本所审查清单")
                harness.tracer.warn("KnowledgeNode", "failed", f"知识库检索失败：{res}")
                continue
            if not res.ok:
                harness.gaps("knowledge", f"知识库检索失败：{res.error}",
                             "本次按通用作业标准执行，未对照本所审查清单")
                harness.tracer.warn("KnowledgeNode", "failed", f"知识库检索失败：{res.error}")
                continue

            data = res.data if isinstance(res.data, dict) else {}
            got = data.get("knowledges") or []
            if res.degraded and res.fallback_note:
                # 工具层的 note（含"未标注版本"提示）原样带到 Trace，不二次加工
                version_notes.append(res.fallback_note)
                harness.tracer.warn("KnowledgeNode", "degraded", res.fallback_note)
            if not got:
                harness.tracer.info("KnowledgeNode", "no_hit",
                                    f"知识库无命中（doc_type={doc_type or '不限'}）：{q[:60]}")
            hits.extend(got)

    added = harness.merge(state, "knowledges", _dedupe_by_title(hits))
    harness.tracer.info("KnowledgeNode", "retrieved",
                        f"新增知识片段 {added} 条（doc_type={doc_type or '不限'}）")

    if not state.get("knowledges"):
        harness.gaps("knowledge",
                     "本所知识库未检索到相关指引或审查清单",
                     "本次按通用作业标准执行，结论未对照本所既有办案经验")
        harness.snapshot()
        return state

    await _mark_versions(state, harness, version_notes)

    harness.snapshot()
    return state


# --------------------------------------------------------------------------- 版本
async def _mark_versions(state: LegalState, harness: Harness,
                         tool_notes: list[str]) -> None:
    """标注指引版本，并把缺失版本的条目单列为证据缺口。

    为什么不直接对缺失版本的条目调 ``knowledge_search`` 再取一次：
    版本号是文档元数据，检索接口拿不到就是原始入库时没写 —— 再检一次也一样。
    所以这里的正确动作是**如实标注 + 提示人工确认**，而不是重试。
    """
    items = list(state.get("knowledges") or [])
    missing = [k for k in items if not (k.get("version") or "").strip()]

    if not missing:
        harness.tracer.info("KnowledgeNode", "version_ok",
                            f"{len(items)} 份指引均已标注版本")
        return

    titles = "、".join((k.get("title") or "未命名") for k in missing[:3])
    more = f"等 {len(missing)} 份" if len(missing) > 3 else ""
    note = (f"{len(missing)} 份指引未标注版本（{titles}{more}），"
            "引用其作业标准前须人工确认是否为最新版")
    # 单独登记一条：不与工具层的泛化 note 合并，因为这条直接影响律师的作业判断
    harness.gaps("knowledge", note, "按过期版本的审查清单作业可能导致风险点遗漏")
    harness.tracer.warn("KnowledgeNode", "version_missing", note)

    for k in missing:
        k["version_confidence"] = "unknown"
    for k in items:
        k.setdefault("version_confidence", "known" if (k.get("version") or "").strip() else "unknown")


def _dedupe_by_title(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一份指引的不同片段在合并前先去重相同 chunk。

    ``harness.merge`` 会按 ``title|chunk_id`` 去重，这里再按 chunk_id 兜一层，
    是因为同一份指引被不同检索式命中的往往是**同一个 chunk**，
    让 merge 收到重复项只会白白消耗它的循环。
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for h in hits:
        key = h.get("chunk_id") or f"{h.get('title')}|{h.get('snippet', '')[:60]}"
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def knowledge_digest(state: LegalState) -> dict[str, Any]:
    """知识库命中的摘要，供 ReviewNode 判断"有没有本所清单可用"。

    分类必须按**类型码**判，不能拿中文子串去匹配 ``doc_type`` ——
    索引里存的是 "checklist"，``"清单" in "checklist"`` 恒为 False，
    于是"有没有清单可用"永远答"没有"，ReviewNode 会退化成通用审查。
    """
    from backend.corpus.guidelines import DOC_TYPES

    items = state.get("knowledges") or []
    labels = {k: DOC_TYPES[v] for k, v in DOC_TYPES.items()}
    return {
        "count": len(items),
        "titles": [k.get("title") or "" for k in items],
        "checklists": [k for k in items if k.get("doc_type") == "checklist"],
        "templates": [k for k in items if k.get("doc_type") == "template"],
        "type_labels": labels,
        "unversioned": [k.get("title") or "" for k in items if not (k.get("version") or "").strip()],
    }
