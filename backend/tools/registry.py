"""MCP Tool 注册表。

按方案第五章的 14 个工具统一登记为 :class:`ToolSpec`，供 Harness 的 Router 与
Tool Executor 消费，并通过 :func:`mcp_manifest` 暴露给 MCP / FastMCP 服务端。

**与方案表格的差异**：另登记了方案表格外的工具：
``knowledge_search``（知识类）与 ``case_crawl``（采集类）。

- ``knowledge_search``：把方案里 Knowledge Agent 的"直接 RAG"收编到
  MCP 通道上 —— 否则知识库检索会绕过权限校验、审计留痕、Trace 记账与配额约束；
- ``case_crawl``：把 Playwright 外部采集（``backend/crawler/``）收进同一条
  通道，理由相同。它默认关闭（``CRAWL_ENABLED=false``），未启用时返回**降级结果**
  并给出 ``fallback_note``，而不是静默返回空。

差异在管理后台的 MCP 工具页会显示为「知识类」「采集类」，可据此判断是否越权扩展。

设计要点（为什么是这样）：
1. **注册表是唯一真源**：工具的次数配额、幂等性、超时、降级路径都在这里声明。
   Agent 节点不直接 import 工具函数，只通过 ``get_tool(name)`` 拿声明再交给
   ``ToolExecutor.run``，这样限额与审计无法被绕过。
2. **幂等性决定是否重试**：检索类（幂等）失败可安全重试；写入类（
   ``case_progress_record`` / ``review_checklist_export``）标 ``idempotent=False``，
   由 Executor 强制 ``max_retry=0``，避免"重试一次记两条进度"。
3. **降级必须显式**：法律场景宁可缩小结论范围也不假装有证据，所以关键检索类工具都
   提供真实的 fallback（纯 BM25 通道 / 纯文本抽取），失败时返回
   ``degraded=True`` + ``fallback_note``，由上层写入 ``evidence_gap``。
"""

from __future__ import annotations

import logging
from typing import Any

from backend.app.config import settings
from backend.tools.base import ToolResult, ToolSpec, param, tool_executor

logger = logging.getLogger("legal_agent.tools.registry")


# --------------------------------------------------------------------------- 分类
CATEGORIES: dict[str, str] = {
    "statute": "法规类",
    "case": "类案类",
    "material": "材料类",
    "document": "文书类",
    "business": "业务类",
    # 「知识类」是方案第五章 14 工具表之外的**超集登记**。
    # 原表把 Knowledge Agent 描述为"直接做 RAG"，没有单列工具；但直接调 RAG 会绕过
    # Harness 的权限校验、审计留痕、Trace 记账、次数配额四条约束。所以把它登记成工具，
    # 让它和 statute_search / case_search 走同一条通道。配额上不设限（只读检索类）。
    "knowledge": "知识类",
    # 「计算类」也是超集登记：方案第五章的 14 工具表里没有它，但项目总纲是
    # 「凡是能算出来的一律由代码算」（`template_service._PROMPT` 规则 8）。
    # 登记成工具走同一条通道，才有配额、审计与留痕 —— 否则金额是模型自己算的，
    # 交付物里出现两个互相打脸的算式时无从复盘。
    "calc": "计算类",
    # 「采集类」同样是表外登记：把浏览器采集收进 MCP 通道，
    # 让它与其它工具一样受权限校验/审计留痕/次数配额约束。
    # 默认关闭（CRAWL_ENABLED=false），未启用时该工具返回**降级结果**而非静默为空。
    "crawl": "采集类",
    # 「技能类」是方案 B 新增：把技能市场（skill_registry）中用户已启用的
    # 技能注册为可调工具，使启用/关闭真实进入 AI 回答链路。
    # 只读、幂等：不写数据，仅把技能能力卡片交给回答流程作为作业指导。
    "skill": "技能类",
}

# --------------------------------------------------------------------------- 降级实现
async def _fb_statute_search(query: str, top_k: int = 6, **kwargs: Any) -> ToolResult:
    """法条检索降级：绕过向量库与 Rerank，只走本地 BM25 索引。

    混合检索失败通常来自 Milvus / Embedding 不可用，此时 BM25 索引仍在本地磁盘，
    是真正独立的第二条路。返回结果照常带效力状态，仍然满足"无来源不出引用"。
    """
    import asyncio

    from backend.parser.structure import check_statute_status, StatuteMeta
    from backend.rag.domain import filter_statutes_by_domain
    from backend.rag.retriever.bm25 import get_bm25

    domain = str(kwargs.get("domain") or "").strip()

    try:
        idx = get_bm25("statute")
        hits = await asyncio.to_thread(idx.search, query, top_k)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(
            ok=False, tool="statute_search",
            error=f"BM25 降级通道亦失败：{exc}",
            degraded=True, fallback_note="法规检索完全不可用，需律师自行核查法条",
        )

    statutes: list[dict[str, Any]] = []
    for h in hits:
        meta = h.get("metadata") or {}
        status = meta.get("status") or check_statute_status(
            StatuteMeta(
                effective_date=_d(meta.get("effective_date")),
                repealed_date=_d(meta.get("repealed_date")),
            )
        )
        statutes.append({
            "doc_name": meta.get("doc_name") or "",
            "article_no": meta.get("article_no") or "",
            "text": h.get("text") or "",
            "status": status,
            "level": meta.get("level") or "",
            "publisher": meta.get("publisher") or "",
            "effective_date": meta.get("effective_date") or "",
            "source_path": meta.get("source_path") or "",
            "score": float(h.get("score") or 0.0),
            "retriever": "bm25-fallback",
        })

    if domain:
        statutes = filter_statutes_by_domain(statutes, domain)

    return ToolResult(
        ok=True, tool="statute_search",
        data={"query": query, "count": len(statutes), "statutes": statutes,
              "trace": {"vector_hits": 0, "bm25_hits": len(statutes),
                        "fused": len(statutes), "elapsed_ms": 0,
                        "degraded": True, "notes": ["向量检索不可用，已降级为纯 BM25"]}},
        degraded=True,
        fallback_note="向量检索不可用，法条结果由纯 BM25 通道返回，召回率可能下降",
    )


async def _fb_case_search(query: str, top_k: int = 5, **kwargs: Any) -> ToolResult:
    """类案检索降级：纯 BM25 通道，并明确标注类案证据可能不完整。"""
    import asyncio

    from backend.rag.retriever.bm25 import get_bm25

    try:
        idx = get_bm25("case")
        hits = await asyncio.to_thread(idx.search, query, top_k)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(
            ok=False, tool="case_search",
            error=f"BM25 降级通道亦失败：{exc}",
            degraded=True,
            fallback_note="本次类案检索不可用，分析意见仅基于法规检索结果",
        )

    cases = []
    for h in hits:
        meta = h.get("metadata") or {}
        cases.append({
            "case_no": meta.get("case_no") or "",
            "case_name": meta.get("case_name") or meta.get("title") or "",
            "court": meta.get("court") or "",
            "judge_date": meta.get("judge_date") or "",
            "cause_of_action": meta.get("cause_of_action") or "",
            "gist": meta.get("gist") or "",
            "result": meta.get("result") or "",
            "score": float(h.get("score") or 0.0),
            "retriever": "bm25-fallback",
        })

    return ToolResult(
        ok=True, tool="case_search",
        data={"query": query, "count": len(cases), "cases": cases},
        degraded=True,
        fallback_note="向量检索不可用，类案结果由纯 BM25 通道返回，可能遗漏语义相近判例",
    )


async def _fb_material_parse(path: str, **kwargs: Any) -> ToolResult:
    """材料解析降级：只做纯文本抽取 + 正则要素提取，不做结构/VLM/表格。"""
    import asyncio

    from backend.parser.text_extractor import extract

    try:
        # extract 是同步函数且返回 ExtractionResult（含 .text/.tables/.images）
        result = await asyncio.to_thread(extract, path)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(ok=False, tool="material_parse", error=str(exc),
                          degraded=True, fallback_note="材料解析不可用")

    text = getattr(result, "text", "") or ""
    from backend.tools.material_tools import DATE_RE, MONEY_RE, TERM_RE

    return ToolResult(
        ok=True, tool="material_parse",
        data={
            "path": path,
            "doc_type": "unknown",
            "text": text,
            "clauses": [],
            "parties": {},
            "tables": [],
            "stats": {"chars": len(text), "clauses": 0, "tables": 0, "images": 0},
            "amounts": sorted({m.group(0).strip() for m in MONEY_RE.finditer(text)})[:20],
            "dates": sorted({m.group(0) for m in DATE_RE.finditer(text)})[:20],
            "terms": [m.group(0) for m in TERM_RE.finditer(text)][:10],
            "degraded_pipeline": True,
        },
        degraded=True,
        fallback_note="结构化解析不可用，仅返回纯文本与正则抽取要素；条款拆分与表格解析缺失",
    )


async def _fb_format_normalize(text: str, doc_type: str = "complaint", **kwargs: Any) -> ToolResult:
    """格式规范化降级：原样返回，但明确声明未套用模板。"""
    return ToolResult(
        ok=True, tool="format_normalize",
        data={"doc_type": doc_type, "normalized": text, "applied": False, "changes": []},
        degraded=True,
        fallback_note="格式规范化不可用，已原样返回；交付前需人工套用律所模板",
    )


def _d(value: Any):
    from datetime import date

    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


# --------------------------------------------------------------------------- 注册
def _build_registry() -> dict[str, ToolSpec]:
    # 延迟 import：避免 tools 包内循环依赖，也避免未用到的重依赖在启动时加载
    from backend.tools import (
        business_tools,
        calc_tools,
        case_tools,
        crawl_tools,
        doc_tools,
        knowledge_tools,
        material_tools,
        skill_tools,
        statute_tools,
    )

    specs: list[ToolSpec] = [
        # ---------------------------------------------------------- 法规类
        ToolSpec(
            name="statute_search",
            category="statute",
            description="法条检索：按关键词/条文号/效力层级检索法规，结果强制带效力状态",
            handler=statute_tools.statute_search,
            idempotent=True,
            timeout=settings.TOOL_TIMEOUT,
            fallback=_fb_statute_search,
            params={
                "query": param("str", "检索式（关键词或「《法名》第X条」）"),
                "top_k": param("int", "返回条数", 6),
                "category": param("str", "业务领域过滤", ""),
                "level": param("str", "效力层级过滤（法律/行政法规/地方性法规…）", ""),
                "only_valid": param("bool", "只返回现行有效的条文", False),
                "domain": param("str", "法律领域过滤（labor/tax/env/civil/unknown），空值不过滤", ""),
            },
        ),
        ToolSpec(
            name="statute_validity_check",
            category="statute",
            description="法规时效性校验：现行有效/已修订/已废止/尚未生效",
            handler=statute_tools.statute_validity_check,
            idempotent=True,
            params={
                "items": param("list[dict]", "批量校验的检索结果（含 doc_name/article_no）", None),
                "doc_name": param("str", "单条校验：法规名称", ""),
                "article_no": param("str", "单条校验：条文号", ""),
            },
        ),
        ToolSpec(
            name="statute_version_compare",
            category="statute",
            description="法规版本比对：新旧条文差异（新增/删除/修改）",
            handler=statute_tools.statute_version_compare,
            idempotent=True,
            timeout=settings.TOOL_TIMEOUT * 3,  # 需重新解析两个版本的全文
            params={
                "doc_name": param("str", "法规名称（库内需存在两个版本）"),
                "article_no": param("str", "只比对指定条文，留空比对全文", ""),
            },
        ),

        # ---------------------------------------------------------- 类案类
        ToolSpec(
            name="case_search",
            category="case",
            description="类案检索：按案由/争议焦点/关键词检索指导性案例与典型判例",
            handler=case_tools.case_search,
            idempotent=True,
            timeout=settings.TOOL_TIMEOUT,
            fallback=_fb_case_search,
            params={
                "query": param("str", "检索式"),
                "top_k": param("int", "返回条数", 5),
                "cause_of_action": param("str", "案由过滤（如劳动合同纠纷）", ""),
                "court_level": param("str", "法院层级过滤", ""),
                "keywords": param("list[str]", "关键词列表（用于加权）", None),
                "domain": param("str", "法律领域过滤（跨领域错配治理）", ""),
            },
        ),
        ToolSpec(
            name="case_summarize",
            category="case",
            description="判例结构化摘要：提取裁判要旨、争议焦点、裁判结果、裁判理由",
            handler=case_tools.case_summarize,
            idempotent=True,
            params={
                "text": param("str", "判例全文", ""),
                "case_no": param("str", "案号（用于从库中补齐全文）", ""),
                "case_name": param("str", "案例名称", ""),
            },
        ),
        ToolSpec(
            name="case_no_verify",
            category="case",
            description="案号真实性校验（Citation Validator 依赖）：核对案号/案例名是否真实存在",
            handler=case_tools.case_no_verify,
            idempotent=True,
            params={
                "case_no": param("str", "案号", ""),
                "case_name": param("str", "案例名称（无案号时按名称 + 来源核验）", ""),
            },
        ),

        # ---------------------------------------------------------- 材料类
        ToolSpec(
            name="material_parse",
            category="material",
            description="合同/文书结构化解析：条款拆分、当事人与要素提取、表格结构化",
            handler=material_tools.material_parse,
            idempotent=True,
            timeout=settings.TOOL_TIMEOUT * 4,  # 大 PDF + VLM 调用
            fallback=_fb_material_parse,
            params={
                "path": param("str", "服务端文件路径"),
                "do_vision": param("bool", "是否启用 VLM 图像理解，None=按配置自动判定", None),
            },
        ),
        ToolSpec(
            name="material_ocr",
            category="material",
            description="案件材料 OCR 与要素提取：扫描卷宗的文本识别与要素回插",
            handler=material_tools.material_ocr,
            idempotent=True,
            timeout=settings.TOOL_TIMEOUT * 4,
            params={
                "path": param("str", "服务端文件路径"),
                "hint": param("str", "识别提示（文档类型提示可提升准确率）", ""),
            },
        ),
        ToolSpec(
            name="material_cross_compare",
            category="material",
            description="多文档交叉比对：合同 vs 补充协议 vs 函件的当事人/金额/日期/期限一致性",
            handler=material_tools.material_cross_compare,
            idempotent=True,
            timeout=settings.TOOL_TIMEOUT * 3,
            params={"paths": param("list[str]", "待比对文件路径列表（≥2 个）")},
        ),

        # ---------------------------------------------------------- 文书类
        ToolSpec(
            name="draft_generate",
            category="document",
            description="文书草稿生成：起诉状/答辩状/代理意见书/合同修改稿（引用必须来自检索证据）",
            handler=doc_tools.draft_generate,
            idempotent=True,
            timeout=settings.TOOL_TIMEOUT * 4,  # LLM 长文本生成
            params={
                "doc_type": param("str", "complaint/defense/opinion/arbitration_application/revision", "complaint"),
                "facts": param("list[dict]", "案件事实（每条须带 source）", None),
                "issues": param("list[dict]", "争议焦点与请求事项", None),
                "statutes": param("list[dict]", "已通过时效校验的法条（法律依据章节的唯一来源）", None),
                "cases": param("list[dict]", "可参照的类案", None),
                "parties": param("list[dict]", "当事人", None),
                "query": param("str", "用户的原始诉求", ""),
                "templates": param("list[dict]", "律所模板片段（来自知识库）", None),
            },
        ),
        ToolSpec(
            name="revision_draft_generate",
            category="document",
            description="合同修改对照稿生成：原文/修改后/修改理由三列对照",
            handler=doc_tools.revision_draft_generate,
            idempotent=True,
            timeout=settings.TOOL_TIMEOUT * 4,
            params={
                "contract_text": param("str", "合同原文"),
                "risk_points": param("list[dict]", "风险点清单（含 clause_no/clause_text/suggestion）"),
                "contract_type": param("str", "合同类型", ""),
            },
        ),
        ToolSpec(
            name="format_normalize",
            category="document",
            description="文书格式规范化：套用律所模板（标题/当事人/正文/落款/日期）",
            handler=doc_tools.format_normalize,
            idempotent=True,
            fallback=_fb_format_normalize,
            params={
                "text": param("str", "待规范化的文书正文"),
                "doc_type": param("str", "文书类型，决定套用哪套模板", "complaint"),
            },
        ),

        # ---------------------------------------------------------- 知识类
        ToolSpec(
            name="knowledge_search",
            category="knowledge",
            description="律所知识库检索：办案指引、审查清单、合同模板、文书规范（结果标注指引版本）",
            handler=knowledge_tools.knowledge_search,
            idempotent=True,
            # 知识库检索实测正常 11~28s，波动可超 30s；全局 TOOL_TIMEOUT=30 会让
            # 单次检索被误判超时并白费 2 轮重试（实测一条检索被拖成 91s/attempts=3）。
            # 单独放宽到 60s 并降为最多 1 次重试：正常路径一次成功（~35s），
            # 抖动可恢复，不再为确定性慢检索付出双倍重试代价。
            timeout=60,
            max_retry=1,
            # 不配 fallback：知识库是"本所作业标准"，不是法定证据。
            # 降级成纯 BM25 会让律师以为检索过了全部指引 —— 宁可明确报"未检索到"。
            params={
                "query": param("str", "检索式"),
                "top_k": param("int", "返回条数", 5),
                "doc_type": param("str", "过滤文档类型（审查清单/办案指引/合同模板/文书规范）", ""),
                "domain": param("str", "法律领域过滤（labor/tax/env/civil，跨领域错配治理）", ""),
            },
        ),

        # ---------------------------------------------------------- 计算类
        ToolSpec(
            name="damages_calc",
            category="calc",
            description="法定金额测算：经济补偿 N / 违法解除赔偿金 2N / 代通知金 / 加班费，"
                        "按法定折算规则确定性计算",
            handler=calc_tools.damages_calc,
            idempotent=True,
            # 不配 fallback：确定性计算的"降级"只能是算错，没有第二条路可走。
            # 缺关键事实时工具自会返回 ok=False + missing，由上层如实写进证据缺口。
            params={
                # 参数名与 handler 签名逐字一致，`_audit_signatures` 在 import 时校验。
                "scenario": param("str", "测算场景：illegal_termination / economic_compensation / "
                                         "notice_pay / overtime / termination_bundle"),
                "monthly_wage": param("float", "解除或终止前十二个月的平均工资（元）", None),
                "years": param("float", "工作年限（年）；给了起止日期时以日期为准", None),
                "start_date": param("str", "入职日期 YYYY-MM-DD", ""),
                "end_date": param("str", "离职日期 YYYY-MM-DD", ""),
                "cap_monthly": param("float", "当地上年度职工月平均工资的三倍（封顶线），"
                                             "留空则不做封顶判断", None),
                "overtime": param("dict", "加班费要素 {hourly_base|monthly_wage, "
                                          "weekday_hours, restday_hours, holiday_hours}", None),
            },
        ),

        # ---------------------------------------------------------- 业务类
        ToolSpec(
            name="case_progress_record",
            category="business",
            description="案件进度记录：写入案件进度流水（与案件管理系统对接）",
            handler=business_tools.case_progress_record,
            idempotent=False,  # 写入类：绝不重试
            params={
                "case_id": param("str", "案件编号"),
                "stage": param("str", "办理阶段（须为 PROGRESS_STAGES 之一）", ""),
                "note": param("str", "进度说明", ""),
                "operator": param("str", "操作人", ""),
                "task_id": param("str", "关联任务号", ""),
                "result": param("str", "本阶段成果", ""),
            },
        ),
        ToolSpec(
            name="review_checklist_export",
            category="business",
            description="审查意见清单导出：生成 Markdown/HTML 交付文件（可直接另存 Word/PDF）",
            handler=business_tools.review_checklist_export,
            idempotent=False,
            timeout=settings.TOOL_TIMEOUT * 2,
            params={
                "content": param("str", "正文（与结构化字段二选一）", ""),
                "title": param("str", "文档标题", "审查意见清单"),
                "export_type": param("str", "review/analysis/draft", "review"),
                "case_id": param("str", "关联案件", ""),
                "risk_points": param("list[dict]", "风险点清单", None),
                "missing_clauses": param("list", "缺失条款提示", None),
                "citations": param("list[dict]", "引用清单", None),
                "evidence_gap": param("list", "证据缺口", None),
                "confidence": param("float", "置信度 0~1", None),
                "format": param("str", "导出格式 md/html", "md"),
            },
        ),

        # ---------------------------------------------------------- 采集类
        ToolSpec(
            name="case_crawl",
            category="crawl",
            description="外部采集：用 Playwright 抓取指定文书详情页并入库（案号/法院/日期自动抽取）",
            handler=crawl_tools.case_crawl,
            idempotent=True,  # 存储层 doc_id + 正文哈希双去重，重复执行不产生重复数据
            # 一次要起浏览器 + 导航 + 随机延迟 3~8s，比纯检索慢一个量级
            timeout=settings.TOOL_TIMEOUT * 6,
            params={
                "url": param("str", "文书详情页 URL"),
                "source": param("str", "来源标识（同时决定登录态文件与单日配额归属）", "default"),
                "selectors": param("str", "DOM 选择器，形如 body=div#content,title=h1；留空走正文正则兜底", ""),
            },
        ),

        # ---------------------------------------------------------- 技能类
        ToolSpec(
            name="skill_list",
            category="skill",
            description="技能列表：列出用户已启用的市场技能（能力名称/分类/描述），供回答链路了解可用技能",
            handler=skill_tools.skill_list,
            idempotent=True,
            params={
                "user_id": param("str", "用户标识；携带时仅返回该用户已启用的技能", ""),
            },
        ),
        ToolSpec(
            name="skill_invoke",
            category="skill",
            description="技能调用：按技能ID/名称取回技能能力卡片（名称/分类/描述/标签/启用态），"
                        "回答流程据此按该技能的能力范围组织内容",
            handler=skill_tools.skill_invoke,
            idempotent=True,
            params={
                "skill_id": param("str", "技能ID（如 sk-xxxxxxxxxx）", ""),
                "skill_name": param("str", "技能名称（与 skill_id 二选一）", ""),
                "query": param("str", "本次任务描述，供技能卡片与回答上下文关联", ""),
                "user_id": param("str", "用户标识；携带时仅允许命中已启用技能", ""),
            },
        ),
    ]

    registry: dict[str, ToolSpec] = {}
    for spec in specs:
        if spec.name in registry:
            raise RuntimeError(f"工具名重复注册：{spec.name}")
        registry[spec.name] = spec

    _audit_signatures(registry)
    return registry


# --------------------------------------------------------------------------- 契约自检
def _audit_signatures(registry: dict[str, ToolSpec]) -> None:
    """用 handler 的真实签名对拍 ``params`` 声明。

    为什么要在导入时就做这件事：工具调用是 ``handler(**kwargs)``，
    声明的参数名与函数签名不一致时，报错发生在**流程跑到一半**的时候
    （``TypeError: unexpected keyword argument 'level'``），
    而此时可能已经消耗掉了检索配额与若干次 LLM 调用。
    本项目在整改前就存在这类问题：``case_search`` 声明 ``level`` 而函数要
    ``court_level``、``draft_generate`` 声明 ``case_facts`` 而函数要 ``facts``、
    ``revision_draft_generate`` 声明 ``original`` 而函数要 ``contract_text``。

    处理策略：
    - 声明了但签名里没有 → **删除该声明并告警**（留着必然引发 TypeError）；
    - 签名里有但没声明 → **自动补上**（按注解推断类型，无默认值即为必填），
      避免"能用但没登记"的隐式参数导致 MCP 客户端看不到它。
    """
    import inspect

    problems: list[str] = []
    for spec in registry.values():
        try:
            sig = inspect.signature(spec.handler)
        except (TypeError, ValueError):  # pragma: no cover
            continue

        actual: dict[str, inspect.Parameter] = {
            n: p for n, p in sig.parameters.items()
            if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        }

        for name in [n for n in spec.params if n not in actual]:
            spec.params.pop(name)
            problems.append(f"{spec.name}.{name} 声明了但函数签名中不存在，已移除")

        for name, p in actual.items():
            if name in spec.params:
                continue
            type_ = _infer_type(p.annotation)
            if p.default is p.empty:
                spec.params[name] = param(type_, f"（未显式声明，由函数签名推断：{name}）")
                problems.append(f"{spec.name}.{name} 未声明，已按签名补为**必填**（type={type_}）")
            else:
                # 注意：`do_vision: bool | None = None` 这类默认值本身就是 None，
                # 不能把"默认值是 None"当成"没有默认值"。判据必须是 p.default is p.empty。
                spec.params[name] = param(type_, f"（未显式声明，由函数签名推断：{name}）", p.default)
                problems.append(f"{spec.name}.{name} 未声明，已按签名补为可选（type={type_}）")

    if problems:
        logger.warning("Tool 参数声明与函数签名不一致，已自动修正 %d 处：\n  - %s",
                       len(problems), "\n  - ".join(problems))


def _infer_type(annotation: Any) -> str:
    """从类型注解推断简写类型名（``list[dict[str, Any]]`` -> ``list[dict]``）。"""
    text = str(annotation)
    if text in ("<class 'inspect._empty'>", "inspect._empty", "None", "NoneType"):
        return "str"
    if "list[" in text:
        inner = "dict" if "dict" in text else "str"
        return f"list[{inner}]"
    if "dict[" in text:
        return "dict"
    for simple in ("int", "float", "bool", "str"):
        if simple in text:
            return simple
    return "str"



TOOL_REGISTRY: dict[str, ToolSpec] = _build_registry()


# --------------------------------------------------------------------------- 查询
def get_tool(name: str) -> ToolSpec:
    """按名取工具声明；不存在时抛 KeyError，由 Router 转成显式错误（不静默跳过）。"""
    if name not in TOOL_REGISTRY:
        raise KeyError(f"未注册的工具：{name}；可用工具：{', '.join(TOOL_REGISTRY)}")
    return TOOL_REGISTRY[name]


def has_tool(name: str) -> bool:
    return name in TOOL_REGISTRY


def tool_names(category: str | None = None) -> list[str]:
    if category is None:
        return list(TOOL_REGISTRY)
    return [n for n, s in TOOL_REGISTRY.items() if s.category == category]


def list_tools(category: str | None = None) -> list[dict[str, Any]]:
    """工具清单（含类别中文名与配额）。"""
    out = []
    for name, spec in TOOL_REGISTRY.items():
        if category and spec.category != category:
            continue
        d = spec.to_schema()
        d["category_label"] = CATEGORIES.get(spec.category, spec.category)
        if name == "statute_search":
            d["quota"] = settings.STATUTE_SEARCH_MAX_CALLS
            d["quota_scope"] = "每次任务"
        elif name == "case_search":
            d["quota"] = settings.CASE_SEARCH_MAX_CALLS
            d["quota_scope"] = "每次任务"
        out.append(d)
    return out


def categorize() -> dict[str, list[str]]:
    """按类别分组，供管理后台「MCP 工具」页展示。"""
    groups: dict[str, list[str]] = {c: [] for c in CATEGORIES}
    for name, spec in TOOL_REGISTRY.items():
        groups.setdefault(spec.category, []).append(name)
    return {k: v for k, v in groups.items() if v}


def mcp_manifest() -> dict[str, Any]:
    """MCP ``tools/list`` 响应体。"""
    return {
        "protocol": "mcp",
        "version": "1.0",
        "server": "legal-agent-mcp",
        "tools": [
            {
                "name": spec.name,
                "description": spec.description,
                "category": spec.category,
                "inputSchema": spec.input_schema(),
                "annotations": {
                    "title": spec.description.split("：")[0],
                    "idempotent": spec.idempotent,
                    "timeout": spec.timeout or settings.TOOL_TIMEOUT,
                    "requires_auth": spec.requires_auth,
                    "requires_audit": spec.requires_audit,
                    # MCP 规范里的只读提示：让外部客户端知道哪些工具不会改数据
                    "readOnlyHint": spec.idempotent,
                    "destructiveHint": not spec.idempotent,
                },
            }
            for spec in TOOL_REGISTRY.values()
        ],
    }


# --------------------------------------------------------------------------- 调用
async def call_tool(name: str, /, *, task_id: str = "", **kwargs: Any) -> ToolResult:
    """统一调用入口：查声明 → Executor 执行（超时/重试/限额/降级）。

    ``task_id`` 是**关键字专用**参数，不会混进 handler 的 kwargs。
    限额是任务级约束（见 :class:`~backend.tools.base.ToolExecutor` 的说明），
    靠它区分任务 —— 少了它，限额会退回"按进程累计"。
    """
    try:
        spec = get_tool(name)
    except KeyError as exc:
        return ToolResult(ok=False, tool=name, error=str(exc), degraded=True,
                          fallback_note="工具不存在，已跳过")

    result = await tool_executor.run(spec, task_id=task_id, **kwargs)
    logger.info("工具调用 %s ok=%s degraded=%s %dms",
                name, result.ok, result.degraded, result.elapsed_ms)
    return result


def executor_stats() -> dict[str, Any]:
    return tool_executor.stats()


def registry_status() -> dict[str, Any]:
    """注册表运行态，供 /health 与管理后台展示。"""
    return {
        "total": len(TOOL_REGISTRY),
        "categories": {CATEGORIES.get(k, k): len(v) for k, v in categorize().items()},
        "quotas": {
            "statute_search": settings.STATUTE_SEARCH_MAX_CALLS,
            "case_search": settings.CASE_SEARCH_MAX_CALLS,
        },
        "with_fallback": [n for n, s in TOOL_REGISTRY.items() if s.fallback is not None],
        "idempotent": [n for n, s in TOOL_REGISTRY.items() if s.idempotent],
        "executor": executor_stats(),
    }
