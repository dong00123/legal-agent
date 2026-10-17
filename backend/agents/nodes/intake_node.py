"""IntakeNode（材料解析 + 要素提取）。

方案 4.1：解析案件材料/合同结构，提取主体、事实、争点要素，输出结构化要素供下游复用。

两条输入路径：
1. **有材料文件**（``state.material_paths``）→ 走 ``material_parse`` MCP 工具做结构化解析；
2. **只有自然语言描述**（``state.query``）→ 不调工具，从描述里抽要素并**明确标注
   "要素来自用户陈述，未经材料核验"**。

第 2 条很容易被忽略，但它是本节点最重要的降级声明：律师如果不知道"事实是用户说的
不是材料证明的"，会拿它当已核实事实用。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from backend.agents.harness import Harness
from backend.agents.state import LegalState
from backend.parser.structure import extract_parties

logger = logging.getLogger("legal_agent.nodes.intake")

AGENT = "intake"

# 金额、日期、期限的粗抽取（用于"事实"要素化）
AMOUNT_RE = re.compile(r"[¥￥]?\s*([0-9][0-9,，]*(?:\.[0-9]{1,2})?)\s*(万元|元|个月|天|日|周|月|年)")
DATE_RE = re.compile(r"((?:19|20)\d{2})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*日?")
DURATION_RE = re.compile(r"(\d+)\s*(个?月|年|天|日|周)")

# 事实句的特征：陈述性 + 含时间/动作
FACT_VERBS = ("入职", "离职", "签订", "约定", "支付", "拖欠", "解除", "辞退", "受伤",
              "加班", "请假", "通知", "履行", "违约", "损坏", "交付", "转让", "终止",
              "未", "被", "要求", "主张", "拒绝", "调岗", "降薪", "考核")

ISSUE_HINT_RE = re.compile(
    r"(争议焦点|核心问题|关键问题|主要争议|争点)[：:是为]?\s*([^。；\n]{4,120})"
)


async def intake_node(state: LegalState, harness: Harness) -> LegalState:
    """材料解析 + 要素提取。"""
    harness.step("IntakeNode")

    paths = list(state.get("material_paths") or [])
    parsed_any = False

    # ---------------------------------------------------------------- 路径 1：解析文件
    for path in paths[:5]:  # 单任务最多解析 5 份材料，防止 max_steps 被材料耗光
        harness.ctx.audit_material(path)
        res = await harness.tool("material_parse", AGENT, path=path)
        if not res.ok:
            harness.gaps("knowledge", f"材料解析失败：{path}（{res.error}）",
                         "该材料内容未参与分析，结论可能遗漏关键事实")
            continue

        data = res.data or {}
        parsed_any = True
        state["material_text"] = ((state.get("material_text") or "") + "\n" + (data.get("text") or ""))[:200000]
        harness.merge(state, "clauses", data.get("clauses") or [])
        harness.merge(state, "tables", data.get("tables") or [])

        parties = data.get("parties") or {}
        harness.merge(state, "parties", _parties_to_list(parties))
        if data.get("doc_type") and data["doc_type"] != "unknown":
            state["doc_type"] = data["doc_type"]  # type: ignore[typeddict-item]

        if res.degraded and res.fallback_note:
            harness.gaps("knowledge", res.fallback_note, "材料要素提取不完整，需人工复核")
        harness.tracer.info("IntakeNode", "parsed",
                            f"{path}｜{data.get('stats', {}).get('chars', 0)} 字｜"
                            f"条款 {len(data.get('clauses') or [])}｜表格 {len(data.get('tables') or [])}")

    # ---------------------------------------------------------------- 路径 2：从文本抽要素
    text = state.get("material_text") or state.get("query") or ""

    if not parsed_any and state.get("query"):
        # 没有材料文件时，把用户描述本身作为"事实来源"，但必须标注未核验
        harness.gaps(
            "knowledge",
            "本次未提供案件材料文件，事实要素来自用户陈述，未经材料核验",
            "事实认定可能不准确，需律师核对材料后确认",
        )

    if text:
        if not state.get("parties"):
            harness.merge(state, "parties", _parties_to_list(extract_parties(text)))
        harness.merge(state, "facts", _extract_facts(text, source=_source_of(state, parsed_any)))
        harness.merge(state, "issues", _extract_issues(text, state))
        if not state.get("clauses") and _should_split(state):
            clauses = _split_clauses(text)
            harness.merge(state, "clauses", clauses)
            harness.tracer.info("IntakeNode", "clauses",
                                f"条款拆分 {len(clauses)} 条（纯文本路径）")

    harness.tracer.info("IntakeNode", "elements",
                        f"主体 {len(state.get('parties') or [])}｜"
                        f"事实 {len(state.get('facts') or [])}｜"
                        f"争点 {len(state.get('issues') or [])}")

    # ---------------------------------------------------------------- 文书类型
    if state.get("draft_type") and not state.get("draft"):
        harness.tracer.info("IntakeNode", "draft_type", state["draft_type"])

    harness.snapshot()
    return state


# --------------------------------------------------------------------------- 工具
def _should_split(state: LegalState) -> bool:
    """是否按条款切分正文。

    **只对合同类切**：裁判文书/起诉状按「第X条」切会得到一堆噪声段落
    （"依照《民法典》第X条判决如下"这类正文会被切成条款），
    反而污染下游的条款定位与逐条高亮。
    """
    return state.get("intent") == "contract_review" or (state.get("doc_type") or "") == "contract"


def _split_clauses(text: str) -> list[dict[str, Any]]:
    """拆分合同条款（复用 ``parser.structure.split_clauses``）。

    路径 1（有材料文件）由 ``material_parse`` 工具拆条，路径 2 此前**根本不拆**，
    后果是纯文本提交的合同 ``clauses`` 恒为 0：

    - 风险点全部只能落在"全文"上，``clause_index`` 全是 -1，前端无法逐条定位；
    - Skill 第 1 步「合同结构解析」只看正文长度就判为完成 —— 这一步实际从未执行，
      属于"校验在说谎"；
    - 规则引擎只好在内部再拆一遍，同一件事两处实现，早晚会不一致。

    拆条能力来自 ``parser/structure.py``：PDF 抽取的文本里「第X条」常与正文同行，
    所以那里的匹配不要求行尾结束（此前只切出 18 条劳动合同法条文，元凶就是行尾锚点）。
    """
    from backend.parser.structure import split_clauses

    out: list[dict[str, Any]] = []
    for c in split_clauses(text or ""):
        no = (c.clause_no or "").strip()
        # 自然段兜底会产出「段落3」这类编号，不是真正的条款定位，
        # 混进 clauses 会让"条款数"这个指标虚高，也会让风险点的 clause_index 指向噪声。
        if no.startswith("段落"):
            continue
        out.append({
            "clause_no": no,
            "clause_title": c.clause_title or "",
            "clause_text": c.clause_text or "",
            "index": c.index,
        })
    return out


def _source_of(state: LegalState, parsed_any: bool) -> str:
    if parsed_any and state.get("material_paths"):
        return str(state["material_paths"][0])
    return "用户陈述（未经材料核验）"


def _parties_to_list(parties: Any) -> list[dict[str, Any]]:
    """``extract_parties`` 可能返回 dict（角色→姓名）或 list，统一成 list。"""
    out: list[dict[str, Any]] = []
    if isinstance(parties, dict):
        for role, value in parties.items():
            if isinstance(value, (list, tuple)):
                for v in value:
                    out.append({"role": role, "name": str(v)})
            elif value:
                out.append({"role": role, "name": str(value)})
    elif isinstance(parties, list):
        for p in parties:
            if isinstance(p, dict):
                merged: dict[str, Any] = {"name": "", "role": "", "kind": ""}
                merged.update({k: v for k, v in p.items() if v is not None})
                out.append(merged)
            elif p:
                out.append({"role": "", "name": str(p), "kind": ""})
    return [p for p in out if p.get("name")]


FACT_SPLIT_RE = re.compile(r"[。；;\n]+")


def _extract_facts(text: str, *, source: str) -> list[dict[str, Any]]:
    """把文本切成"事实句"。

    只保留含事实动词或时间/金额要素的句子 —— 否则会把"根据《劳动合同法》…"这类
    法条引用句也当成案件事实，污染下游的争点归纳。
    """
    facts: list[dict[str, Any]] = []
    for raw in FACT_SPLIT_RE.split(text or ""):
        s = (raw or "").strip()
        if not (10 <= len(s) <= 400):
            continue
        if s.startswith("《") or "本法" in s or "第" in s[:4]:
            continue  # 法条正文
        has_verb = any(v in s for v in FACT_VERBS)
        has_detail = bool(AMOUNT_RE.search(s) or DATE_RE.search(s) or DURATION_RE.search(s))
        if not (has_verb or has_detail):
            continue
        confidence = 0.9 if (has_verb and has_detail) else (0.7 if has_verb else 0.6)
        facts.append({"text": s, "source": source, "confidence": confidence,
                      "page": None})
        if len(facts) >= 25:
            break
    return facts


def _extract_issues(text: str, state: LegalState) -> list[dict[str, Any]]:
    """抽争议焦点。

    优先级：显式标注的"争议焦点：xxx" > 问句 > 无法确定时**不编造**（留空由
    AnalysisNode 依据检索结果归纳）。
    """
    issues: list[dict[str, Any]] = []

    for m in ISSUE_HINT_RE.finditer(text or ""):
        issues.append({"title": m.group(2).strip(), "description": "材料中显式标注的争议焦点",
                       "severity": "medium", "basis": []})

    if not issues:
        # 问句形态的争点（"能否…"、"是否…"、"多少…"）
        for raw in re.split(r"[。？！?\n]+", text or ""):
            s = raw.strip()
            if 6 <= len(s) <= 100 and re.search(r"(能否|是否|算不算|有没有|多少|多长时间|该不该)", s):
                issues.append({"title": s, "description": "由用户提问推断的争点",
                               "severity": "medium", "basis": []})
            if len(issues) >= 8:
                break

    # 仍无争点：不为凑数而编造，交由 AnalysisNode 基于检索证据归纳
    return issues[:8]
