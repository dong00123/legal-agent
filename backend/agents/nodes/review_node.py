"""ReviewNode：合同审查（方案 4.1 Review Agent / 第六章合同审查 Skill）。

本节点承担 Skill 的第 5 步（逐条风险扫描）与第 7 步（风险点清单），并触发
第 8 步（修改对照稿）。第 1、2 步在 IntakeNode，3、4、6 步在检索节点，
第 9 步在 ValidatorNode —— 这是**刻意的**：审查的作业路径横跨多个节点，
Skill 的价值正是把这些分散的步骤用统一的判据串起来，并在最后如实报告
哪几步没走成（见 ``backend/skills/contract_review.py``）。

三条实现纪律：

1. **风险点的法律依据只能来自检索证据**。给 LLM 的 Prompt 里强制带
   「参考资料」（法条/类案/指引），并在合并前**过滤掉不在证据池里的依据**——
   模型偶尔仍会写出库里没有的条文号，这一层过滤是最后一道闸。
2. **扫过就要留痕**。即使一份合同零风险，也要写 ``scanned`` 事件 ——
   Skill 的第 5 步判据靠它区分"扫了没发现问题"与"根本没扫"，这是假阴性的来源。
3. **降级要声张**。LLM 掉到规则引擎时，风险扫描仍然有效（规则版逐条扫描是
   真实实现，不是占位符），但覆盖维度更窄，必须写进 ``evidence_gap``。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from pathlib import Path
from typing import Any

from backend.agents.harness import Harness
from backend.agents.state import LegalState
from backend.app.config import settings
from backend.llm.client import llm_client
from backend.llm.prompts import (
    CONTRACT_REVIEW_PROMPT,
    CONTRACT_REVIEW_SECOND_PASS_PROMPT,
    LEGAL_SYSTEM_PROMPT,
    build_messages,
    evidence_blocks as _evidence_blocks,
    system_with_persona,
)
from backend.skills import skill_context

logger = logging.getLogger("legal_agent.nodes.review")

AGENT = "review"

# 风险等级排序，供输出时按严重度排（律师先看必须改的）
LEVEL_ORDER = {"high": 0, "medium": 1, "low": 2}

# 甲方/乙方立场：决定"往哪边改"。缺省中立，风险点按双向呈现。
CLIENT_ROLE_RE = re.compile(r"(甲方|乙方|出租人|承租人|用人单位|劳动者|买受人|出卖人|委托人)")


async def review_node(state: LegalState, harness: Harness) -> LegalState:
    """合同逐条审查：风险扫描 → 风险点清单 → 修改对照稿。"""
    harness.step("ReviewNode")

    contract_text = state.get("material_text") or ""
    if not contract_text.strip():
        harness.gaps("material", "无合同正文可供审查",
                     "审查无法进行；请通过材料解析或直接粘贴合同文本")
        harness.tracer.error("ReviewNode", "no_text", "缺少合同正文，审查中止")
        return state

    clauses = state.get("clauses") or []
    contract_type = _contract_type(state, contract_text)
    client_role = _client_role(state)
    result = await _scan(state, harness, contract_text, clauses, contract_type, client_role)

    # 第 5 步留痕：无论有没有风险点都必须记，"零风险"与"没扫"必须可区分
    harness.tracer.info("ReviewNode", "scanned",
                        f"已扫描 {len(clauses) or '未拆条'} 条款，识别风险点 {len(result.get('risk_points') or [])} 个")

    fresh = [rp for rp in (result.get("risk_points") or []) if isinstance(rp, dict)]

    # 回环重扫：**本轮报到的条款以本轮结论为准**，撤下上一轮同条款的旧条目。
    # 为什么必须撤：引用终审不过会回环重跑，而 ReviewNode 在重跑尾巴上
    # （graph.LOOP_RERUN_TAIL），于是同一份合同被逐条审两遍，两遍的清单在
    # ``harness.merge`` 里**追加**。合并键是"条款号 + 风险描述前 60 字"，
    # 而两遍的措辞几乎不可能逐字相同 —— 挡不住，律师看到的就是每条报两遍
    # （实测 22 条 = 11 + 11，task_9bc893f93fe5）。
    # 为什么不按相似度去重：两遍的相似度分布是 0.07~0.89 的连续谱，没有可分的
    # 阈值（真重复能低到 0.07，同一条款上两个**不同**风险能到 0.17）——
    # 阈值一低就把真风险点静默删掉，这在合同审查里是最坏的一种错。
    # 条款号是**确定性的**，所以按条款取舍。
    # 本轮没报到的条款一律保留：重扫没提不等于风险消失，宁可多报不可漏报。
    # 本轮一条都没报（重扫失败/降级）时不动旧清单 —— 否则一次失败的扫描
    # 会把上一轮的有效结论整份抹掉。
    rerun = int(harness.node_runs.get("ReviewNode", 0)) > 1
    if rerun and fresh:
        previous = state.get("risk_points") or []
        kept, touched = _supersede_rescanned(previous, fresh)
        if len(kept) < len(previous):
            state["risk_points"] = kept
            harness.tracer.info(
                "ReviewNode", "rescan_replaced",
                f"回环重扫：本轮报到的 {len(touched)} 个条款以本轮结论为准，"
                f"撤下上一轮同条款的 {len(previous) - len(kept)} 条，保留 {len(kept)} 条")

    added = harness.merge(state, "risk_points", fresh)
    # 合并只保证"不重复"，不保证顺序：回环那趟是「上一轮没被本轮覆盖的 + 本轮的」
    # 拼起来，直接呈现就是"先看到几条上一轮的、再看到本轮的"。清单的最终顺序
    # 在这里定死，下游（风险卡 / 对照稿 / 任务详情）拿到的就是合同顺序。
    state["risk_points"] = sort_risks(list(state.get("risk_points") or []), state)
    # 双模型交叉复核：初审「广撒网」→ 第二模型「严核查」（去误报 / 降虚高 / 补漏报）。
    # 放在排序之后、对照稿与抽检统计之前：下游拿到的必须是**收敛后**的清单。
    state["risk_points"] = await _second_pass(
        state, harness, contract_text, clauses, contract_type, client_role,
        list(state.get("risk_points") or []))
    miss = harness.merge(state, "missing_clauses", result.get("missing_clauses") or [])
    harness.tracer.info("ReviewNode", "merged",
                        f"新增风险点 {added} 个，缺失条款提示 {miss} 条")

    if not state.get("risk_points"):
        harness.gaps("review", "本次风险扫描未识别出明显风险点",
                     "建议人工复核全文；零风险不等于已审尽")

    await _revision_draft(state, harness, contract_text, contract_type)
    _record_notes(state, harness, contract_type, client_role, contract_text)
    _maybe_spot_check(state, harness)
    _store_confidence(state, result)

    harness.snapshot()
    return state


def _supersede_rescanned(previous: list[Any],
                         fresh: list[dict[str, Any]]) -> tuple[list[Any], set[str]]:
    """回环重扫时按**条款号**取舍，返回 ``(保留的上一轮条目, 本轮报到的条款号)``。

    判据只有条款号这一个确定性字段（理由见 :func:`review_node` 处的注释）：
    本轮报到的条款，上一轮那些条目全部撤下；本轮没报到的条款，上一轮条目原样保留。

    ⚠ 空条款号（``clause_no`` 为空 = 模型没把风险挂到具体条款上）**按同一个桶处理**：
    本轮只要报了一条"全文性"风险，上一轮的全文性风险就让位 —— 否则最坏情况下
    两个空条款号的条目会同时挂在卡片列表尾部，看上去像同一句话说了两遍。
    """
    touched = {(f.get("clause_no") or "").strip() for f in fresh if isinstance(f, dict)}
    kept = [p for p in previous
            if not (isinstance(p, dict) and (p.get("clause_no") or "").strip() in touched)]
    return kept, touched


# --------------------------------------------------------------------------- 范本
# 范本按字数截断的上限。一份合同动辄上万字，而这段是**每块都带**的
# （见 ``_scan`` 里 system 的说明）—— 不封顶的话长合同 × N 块的 token
# 会成倍地烧在重复的范本正文上。
TEMPLATE_MAX_CHARS = 20_000


def _template_block(state: LegalState) -> str:
    """有范本时返回追加到 ``system`` 的比对基准段；没有则返回空串。

    **范本不是证据。** 所以它进 ``system``，不进 ``_evidence_blocks()``
    —— 那条通路是给法条与案例用的，混进去会让模型把范本条款当法律依据引用，
    而 Citation Validator 只认法规库和案例库，最后要么引用校验不过、
    要么更糟：它"过了"，而正文里写的是"依据范本合同第 3 条"。

    同理，**指出条款位置时一律用待审合同的条款号**：两份合同的编号是两套，
    模型很容易顺手写范本的号，律师照着去待审合同里翻会翻到别处。
    """
    text = (state.get("template_text") or "").strip()
    if not text:
        return ""
    truncated = len(text) > TEMPLATE_MAX_CHARS
    body = text[:TEMPLATE_MAX_CHARS]
    warn = ("\n（注：范本过长已截断，以上仅为前 %d 字，"
            "缺失条款的判断可能因此不完整，需律师补看全文。）" % TEMPLATE_MAX_CHARS) if truncated else ""
    return (
        "\n\n【范本合同（比对基准）】\n"
        f"{body}{warn}\n\n"
        "比对要求：\n"
        "1. 以上范本是本次审查的**基准**，不是待审合同的一部分，也不是法律依据。\n"
        "2. 待审合同相对范本**缺失**的条款，逐条记入 missing_clauses。\n"
        "3. 待审合同相对范本**被改写**的条款（责任加重、期限缩短、范围扩大、"
        "义务转移等），在风险点里写明「相对范本条款有改动」并说明改动对待审合同"
        "一方的影响。\n"
        "4. 范本本身**不得**作为引用来源；citations 只能引法条与案例。\n"
        "5. 指出条款位置时**一律用待审合同的条款号**——两份合同的编号是两套，"
        "写成范本的号会让律师翻错地方。\n"
    )


# --------------------------------------------------------------------------- 扫描
async def _scan(state: LegalState, harness: Harness, contract_text: str,
                clauses: list[dict[str, Any]], contract_type: str,
                client_role: str) -> dict[str, Any]:
    """调 LLM 做逐条风险扫描，并对输出做证据过滤。

    **分块并发**：整单审查里最慢的一步就是这里 —— 模型要读完所有条款再吐一份
    结构化 JSON，实测 25 条单次调用 56.8s（占整单 106s 的一半以上），而且瓶颈在
    输出 token（3300 tokens ÷ ~56 tok/s），不是检索。所以按条款切块并发调用：
    墙钟≈最慢的一块，而不是各块之和；块数不够多时（≤ REVIEW_SCAN_CHUNK_CLAUSES）
    仍走单次调用，避免为几条款付多次往返。
    """
    blocks = _evidence_blocks(state)
    # 四段拼装，各管一件事：
    # - LEGAL_SYSTEM_PROMPT：角色与引用纪律（不能越出「参考资料」）
    # - CONTRACT_REVIEW_PROMPT：输出 Schema 与审查维度（要求输出哪些字段）
    # - skill_context：本所作业路径（必须按哪 9 步走，哪几步是必要的）
    # - 范本段（可选）：比对基准
    # 分开的好处：改 Skill 步骤不用动 Prompt 文本，改输出 Schema 不用动 Skill。
    #
    # 范本挂 ``system`` 而不是 ``user``：``system`` 会被**原样传进** ``_scan_chunked``，
    # 于是单次调用（135 行）、分块并发（211 行）、全局判缺失（258 行）三条路径
    # 一处改全覆盖。挂 ``user`` 就得改三遍，漏一处的表现是"分块时范本悄悄没生效"
    # —— 而分块只在条款多时才触发，正好是最需要范本的长合同。
    system = (f"{system_with_persona(state.get('expert_persona', ''))}\n\n{CONTRACT_REVIEW_PROMPT}\n\n"
              f"{skill_context('contract_review')}{_template_block(state)}")

    chunk_size = int(getattr(settings, "REVIEW_SCAN_CHUNK_CLAUSES", 0) or 0)
    if chunk_size > 0 and len(clauses) > chunk_size:
        return await _scan_chunked(state, harness, contract_text, clauses,
                                   contract_type, client_role, system, blocks)

    clause_dump = "\n".join(
        f"【{c.get('clause_no') or f'第{i + 1}条'}】{c.get('clause_title') or ''}\n"
        f"{(c.get('clause_text') or '')[:1200]}"
        for i, c in enumerate(clauses[:40])
    ) or contract_text[:8000]

    user = (
        f"合同类型：{contract_type}\n"
        f"审查立场：{client_role or '中立（双向提示风险）'}\n\n"
        f"【合同条款】\n{clause_dump}"
    )
    payload = {
        "contract_text": contract_text,
        "clauses": clauses,
        "statutes": state.get("statutes") or [],
        "knowledges": state.get("knowledges") or [],
        "cases": state.get("cases") or [],
        "contract_type": contract_type,
        "client_role": client_role,
    }

    data, llm_res = await llm_client.chat_json(
        build_messages(system, user, blocks),
        task="contract_review",
        payload=payload,
        default={"contract_type": contract_type, "risk_points": [], "missing_clauses": [],
                 "confidence": 0.4},
    )

    if llm_res.degraded:
        note = ("风险扫描由确定性规则引擎完成：覆盖违约金/定金/租赁期限/解除权/管辖等固定维度，"
                "未覆盖行业特有条款与非常规表述，需律师人工补充")
        harness.gaps("review", note, "风险识别的覆盖面窄于完整模型审查")
        harness.tracer.warn("ReviewNode", "llm_degraded", note)

    risk_points = _filter_by_evidence(data.get("risk_points") or [], state, harness)
    return {
        "risk_points": risk_points,
        "missing_clauses": data.get("missing_clauses") or [],
        "confidence": data.get("confidence"),
    }


async def _scan_chunked(state: LegalState, harness: Harness, contract_text: str,
                        clauses: list[dict[str, Any]], contract_type: str,
                        client_role: str, system: str, blocks: list[Any]) -> dict[str, Any]:
    """把条款切块并发扫描，再合并成一份风险点清单。

    两条不能省的纪律：

    1. **每块都带全条款目录**。只把「正文」按块切开，条款号与标题的完整清单始终给全 ——
       否则「与第 X 条重复」这类判断会因为看不全合同而系统性漏判。
       ⚠️ 但「缺失条款」**不在块内判**：它只依赖条款目录、与正文切块无关，
       让 N 块各判一遍等于同一结论付 N 次输出 token，再靠字符串去重拼回去。
       现改为与各块**并发**的一个全局调用只判一次（``one_missing``），
       墙钟不变、token 从 N 份降到 1 份。
    2. **合并要去重**。相邻块的边界条款（如"争议解决"同时出现在两块）被判两次很正常，
       按 条款号+风险描述 去重，避免同一风险在清单里出现两遍、让律师怀疑模型失控。
    """
    size = max(2, int(settings.REVIEW_SCAN_CHUNK_CLAUSES))
    chunks = [clauses[i:i + size] for i in range(0, len(clauses), size)]
    concurrency = max(1, int(getattr(settings, "REVIEW_SCAN_CONCURRENCY", 3) or 3))
    sem = asyncio.Semaphore(concurrency)
    # 全条款目录：跨条款判断（缺失、重复、体系冲突）必须看得到全局
    index_line = "；".join(
        f"{c.get('clause_no') or f'第{i + 1}条'} {(c.get('clause_title') or '').strip()}".strip()
        for i, c in enumerate(clauses)
    )

    async def one(idx: int, chunk: list[dict[str, Any]]) -> dict[str, Any] | None:
        async with sem:
            clause_dump = "\n".join(
                f"【{c.get('clause_no') or f'第{i + 1}条'}】{c.get('clause_title') or ''}\n"
                f"{(c.get('clause_text') or '')[:1200]}"
                for i, c in enumerate(chunk)
            )
            user = (
                f"合同类型：{contract_type}\n"
                f"审查立场：{client_role or '中立（双向提示风险）'}\n\n"
                f"【合同全部条款目录】（用于判断条款间冲突，正文只给出本轮条款）\n"
                f"{index_line}\n\n"
                f"【本轮需逐条审查的条款（第 {idx + 1}/{len(chunks)} 批）】\n{clause_dump}\n\n"
                f"注意：本条正文只是分批送审的一部分，不是合同全文；"
                f"与其它条款的冲突请以目录为准。"
                f"**缺失条款由单独的全局判断负责，本批只输出 risk_points，"
                f"不要输出 missing_clauses。**"
            )
            payload = {
                "contract_text": contract_text,
                "clauses": chunk,
                "statutes": state.get("statutes") or [],
                "knowledges": state.get("knowledges") or [],
                "cases": state.get("cases") or [],
                "contract_type": contract_type,
                "client_role": client_role,
            }
            try:
                data, llm_res = await llm_client.chat_json(
                    build_messages(system, user, blocks),
                    task="contract_review",
                    payload=payload,
                    default={"contract_type": contract_type, "risk_points": [],
                             "missing_clauses": [], "confidence": 0.4},
                )
            except Exception as exc:  # noqa: BLE001 - 单块失败不该拖垮整轮扫描
                logger.warning("第 %d 块扫描失败：%s", idx + 1, exc)
                harness.gaps("review", f"第 {idx + 1} 批条款扫描失败：{type(exc).__name__}",
                             "该批条款未经过模型审查，需人工补审")
                harness.tracer.warn("ReviewNode", "chunk_failed",
                                    f"第 {idx + 1}/{len(chunks)} 批失败：{type(exc).__name__}")
                return None
            got = len(data.get("risk_points") or [])
            harness.tracer.info("ReviewNode", "scan_chunk",
                                f"第 {idx + 1}/{len(chunks)} 批完成：{len(chunk)} 条 → 风险点 {got} 个"
                                + ("（已降级为规则引擎）" if llm_res.degraded else ""))
            data["_degraded"] = bool(llm_res.degraded)
            return data

    async def one_missing() -> dict[str, Any] | None:
        """全局判一次「缺失条款」：只依赖条款目录，一次调用覆盖整份合同。

        与各块并发跑，所以墙钟不变；输出只有缺失清单本身，
        替代原先"每块判一遍再字符串去重"的 N 份重复输出。
        """
        async with sem:
            user = (
                f"合同类型：{contract_type}\n"
                f"审查立场：{client_role or '中立（双向提示风险）'}\n\n"
                f"【合同全部条款目录】\n{index_line}\n\n"
                f"请只依据上面的全部条款目录，判断这份合同**缺失了哪些应当具备的条款**。\n"
                f"只输出 JSON："
                f'{{"missing_clauses": [{{"name": "缺失条款名", "reason": "为什么建议补", '
                f'"level": "medium"}}]}}，不要输出 risk_points。'
            )
            payload = {
                "contract_text": contract_text,
                "clauses": clauses,
                "statutes": state.get("statutes") or [],
                "knowledges": state.get("knowledges") or [],
                "cases": state.get("cases") or [],
                "contract_type": contract_type,
                "client_role": client_role,
            }
            try:
                data, _ = await llm_client.chat_json(
                    build_messages(system, user, blocks),
                    task="contract_review",
                    payload=payload,
                    default={"missing_clauses": []},
                )
                return data
            except Exception as exc:  # noqa: BLE001 - 缺失判断失败不拖垮风险扫描
                logger.warning("缺失条款全局判断失败：%s", exc)
                harness.gaps("review", "缺失条款判断未完成：全局判断调用失败",
                             "缺失条款未经模型判断，交付时需律师对照必备条款清单人工补查")
                harness.tracer.warn("ReviewNode", "missing_failed",
                                    f"缺失条款全局判断失败：{type(exc).__name__}")
                return None

    # 并发跑：块与块之间没有依赖，串行跑等于把等待时间累加；
    # 缺失条款的全局判断与各块无依赖，一并并发。
    gathered = await asyncio.gather(*(one(i, c) for i, c in enumerate(chunks)), one_missing())
    results, missing_data = gathered[:-1], gathered[-1]

    risk_points: list[dict[str, Any]] = []
    missing: list[Any] = []
    seen_rp: set[str] = set()
    confidences: list[float] = []
    degraded_any = False
    failed = 0

    for data in results:
        if not data:
            failed += 1
            continue
        degraded_any = degraded_any or bool(data.get("_degraded"))
        for rp in data.get("risk_points") or []:
            if not isinstance(rp, dict):
                continue
            # 去重键：条款号 + 风险描述前缀。同一风险在两块里各被判一次很常见。
            key = f"{(rp.get('clause_no') or '').strip()}|{(rp.get('risk') or '')[:60]}"
            if key in seen_rp:
                continue
            seen_rp.add(key)
            risk_points.append(rp)
        c = data.get("confidence")
        if isinstance(c, (int, float)):
            confidences.append(float(c))

    # 缺失条款只来自那一次全局判断（块内已明确要求不输出）；
    # 仍按文本去重一次，防御个别网关不守指令的输出。
    seen_miss: set[str] = set()
    for mc in (missing_data or {}).get("missing_clauses") or []:
        text = mc if isinstance(mc, str) else json.dumps(mc, ensure_ascii=False)
        if text in seen_miss:
            continue
        seen_miss.add(text)
        missing.append(mc)

    if degraded_any:
        note = ("风险扫描由确定性规则引擎完成：覆盖违约金/定金/租赁期限/解除权/管辖等固定维度，"
                "未覆盖行业特有条款与非常规表述，需律师人工补充")
        harness.gaps("review", note, "风险识别的覆盖面窄于完整模型审查")
        harness.tracer.warn("ReviewNode", "llm_degraded", note)
    if failed:
        # 部分块失败时**不能假装扫描完整**：这一批条款等于没看，必须写进证据缺口
        harness.gaps("review", f"{failed}/{len(chunks)} 批条款未完成模型审查",
                     "这部分条款的风险未覆盖，交付时须一并呈现并人工补审")

    risk_points = _filter_by_evidence(risk_points, state, harness)
    risk_points = sort_risks(risk_points, state)   # 合同顺序（见 :func:`sort_risks`）
    # 置信度取各块最低值：分块后"最弱的那一块"才是整单的可信度上界
    confidence = min(confidences) if confidences else None
    return {
        "risk_points": risk_points,
        "missing_clauses": missing,
        "confidence": confidence,
    }



# --------------------------------------------------------------------------- 双模型交叉复核
# 初审（ReviewNode._scan）广撒网、宁可多报；复审（第二个模型）严核查：去误报、
# 降虚高、补漏报。复审是增强项，模型没配/失败都原样返回初审清单，绝不丢结论。
def _downgrade(level: str) -> str:
    """等级降一档。high→medium→low，low 不动。"""
    order = ("high", "medium", "low")
    i = order.index(level) if level in order else 1
    return order[min(i + 1, len(order) - 1)]


def _second_pass_feedback() -> str:
    """读复审分歧的历史统计与最近样本，回灌成复审提示词里的一段纪律（自我改进）。

    读不到文件 / 文件损坏 → 返回空串：首次复审没有历史，不该因此失败。
    """
    path = getattr(settings, "REVIEW_SECOND_PASS_STATE", "")
    if not path or not Path(path).is_file():
        return ""
    try:
        blob = json.loads(Path(path).read_text(encoding="utf-8", errors="ignore"))
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(blob, dict):
        return ""
    runs = int(blob.get("runs") or 0)
    examples = blob.get("examples") or []
    # 只有 runs 和 examples 都空才是"没有历史"；人工抽查只写 examples 不写 runs，
    # 那时也要把人工样本读回来（自我改进闭环）。
    if runs <= 0 and not examples:
        return ""
    lines: list[str] = []
    if runs > 0:
        lines += [
            "历史复审纪律（自我改进，来自既往复审的统计）：",
            "累计复审 %d 轮，初审风险点中 confirmed %d / downgraded %d / removed %d，"
            "复审补漏 %d 条。" % (
                runs, int(blob.get("confirmed") or 0), int(blob.get("downgraded") or 0),
                int(blob.get("removed") or 0), int(blob.get("added") or 0)),
            "初审倾向多报：复审时既要敢于降级/删除误报，也要认真找漏报。",
        ]
    if examples:
        lines.append("最近的分歧样本（供校准你的核查口径）：")
        for ex in examples[:6]:
            if not isinstance(ex, dict):
                continue
            level = str(ex.get("level") or "")
            lines.append(
                "- 初审报「%s」%s→ %s：%s" % (
                    str(ex.get("risk") or "")[:60],
                    ("（" + level + "）" if level else ""),
                    str(ex.get("action") or ""),
                    str(ex.get("reason") or "")))
    return "\n".join(lines)


def _record_second_pass(counts: dict[str, int], examples: list[dict[str, str]]) -> None:
    """把本轮复审的分歧写进持久化文件（自我改进的数据来源）。

    只增不减、可重跑：读旧 → 累加统计 → 裁剪示例窗口 → 写回。
    写失败只记日志，绝不让"记分歧"这件事拖垮审查本身。
    """
    path = getattr(settings, "REVIEW_SECOND_PASS_STATE", "")
    if not path:
        return
    p = Path(path)
    blob: dict[str, Any] = {}
    if p.is_file():
        try:
            blob = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
        except Exception:  # noqa: BLE001
            blob = {}
    if not isinstance(blob, dict):
        blob = {}
    blob["runs"] = int(blob.get("runs") or 0) + 1
    for key in ("confirmed", "downgraded", "removed", "added"):
        blob[key] = int(blob.get(key) or 0) + int(counts.get(key) or 0)
    kept = list(blob.get("examples") or [])
    kept.extend(examples)
    blob["examples"] = kept[-20:]          # 只留最近 20 条，文件不无限膨胀
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("复审分歧落盘失败：%s", exc)


async def _second_pass(state: LegalState, harness: Harness, contract_text: str,
                       clauses: list[dict[str, Any]], contract_type: str,
                       client_role: str, risk_points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """用第二个模型复审初审风险点，返回**收敛后**的清单。

    复审模型（``LLM_REVIEW_*``）未配置 / 调用失败 / 输出解析失败时，**原样返回
    初审清单**并登记缺口 —— 复审是增强项，不是硬依赖，绝不能把初审有效结论丢掉。

    复审模型用 ``#N`` 指代初审风险点（条款号 + 描述 的字符串匹配不可靠），
    判定的三种动作落到每条风险点的 ``review`` 字段（confirmed/downgraded/added），
    removed 的条目直接移除。补漏报的 additions 同样过证据过滤（防编条文号）。
    """
    from backend.llm.client import extract_json as _extract_json
    from backend.llm.client import review_provider

    provider = review_provider()
    if provider is None:
        harness.gaps("review", "复审模型未配置（LLM_REVIEW_*），本轮风险点未经第二模型复核",
                     "已按初审结果交付；配置 LLM_REVIEW_* 后自动启用双模型交叉复核")
        harness.tracer.info("ReviewNode", "second_pass_skip", "复审模型未配置，跳过")
        return risk_points

    numbered = list(enumerate(risk_points, start=1))
    index_dump = "\n".join(
        "#%d [%s] 等级=%s 风险=%s" % (
            i, (rp.get("clause_no") or "全文").strip(), rp.get("level") or "medium",
            (rp.get("risk") or "")[:120])
        for i, rp in numbered
    ) or "（初审未报告任何风险点）"
    clause_dump = "\n".join(
        "【%s】%s\n%s" % (
            c.get("clause_no") or f"第{i + 1}条", c.get("clause_title") or "",
            (c.get("clause_text") or "")[:800])
        for i, c in enumerate(clauses[:60])
    ) or contract_text[:8000]

    blocks = _evidence_blocks(state)
    # 用 ``.replace`` 填占位符而不是 ``.format``：Prompt 里那串 JSON 示例自带花括号，
    # ``.format`` 会把它们当成格式字段直接 KeyError。
    system = (f"{system_with_persona(state.get('expert_persona', ''))}\n\n"
              f"{CONTRACT_REVIEW_SECOND_PASS_PROMPT.replace('{feedback}', _second_pass_feedback())}")
    user = (
        f"合同类型：{contract_type}\n"
        f"审查立场：{client_role or '中立（双向提示风险）'}\n\n"
        f"【合同条款】\n{clause_dump}\n\n"
        f"【初审风险点清单】\n{index_dump}"
    )

    try:
        res = await provider.chat(
            build_messages(system, user, blocks),
            json_mode=True,
            max_tokens=max(2048, int(getattr(settings, "LLM_MAX_TOKENS", 2048) or 2048)),
        )
        data = _extract_json(res.text) or {}
    except Exception as exc:  # noqa: BLE001
        harness.gaps("review", f"复审模型调用失败：{type(exc).__name__}，本轮未复审",
                     "已按初审结果交付；复审模型恢复后自动启用")
        harness.tracer.warn("ReviewNode", "second_pass_failed", f"{type(exc).__name__}: {exc}")
        return risk_points

    decisions: dict[int, dict[str, Any]] = {}
    for d in data.get("decisions") or []:
        if isinstance(d, dict) and isinstance(d.get("no"), int):
            decisions[int(d["no"])] = d

    counts = {"confirmed": 0, "downgraded": 0, "removed": 0, "added": 0}
    examples: list[dict[str, str]] = []
    out: list[dict[str, Any]] = []
    for i, rp in enumerate(risk_points, start=1):
        d = decisions.get(i)
        if not d:
            rp.setdefault("review", "confirmed")
            out.append(rp)
            continue
        action = d.get("action") or "confirmed"
        if action == "removed":
            counts["removed"] += 1
            examples.append({"risk": str(rp.get("risk") or "")[:60],
                             "level": rp.get("level") or "", "action": "removed",
                             "reason": str(d.get("reason") or "")[:120]})
            continue                      # 不进入 out = 移除
        if action == "downgraded":
            rp["level"] = d.get("to_level") if d.get("to_level") in LEVEL_ORDER \
                else _downgrade(rp.get("level") or "medium")
            counts["downgraded"] += 1
            examples.append({"risk": str(rp.get("risk") or "")[:60],
                             "level": rp.get("level") or "", "action": "downgraded",
                             "reason": str(d.get("reason") or "")[:120]})
            rp["review"] = "downgraded"
            if d.get("reason"):
                rp["review_note"] = d.get("reason")
            out.append(rp)
            continue
        counts["confirmed"] += 1
        rp.setdefault("review", "confirmed")
        if d.get("reason"):
            rp["review_note"] = d.get("reason")
        out.append(rp)

    for add in data.get("additions") or []:
        if not isinstance(add, dict):
            continue
        add["level"] = add.get("level") if add.get("level") in LEVEL_ORDER else "medium"
        add["review"] = "added"
        add["review_note"] = "复审补充"
        out.append(add)
        counts["added"] += 1

    # 补漏报同样过证据过滤（复审模型也可能编条文号），再按合同顺序排回
    out = _filter_by_evidence(out, state, harness)
    out = sort_risks(out, state)

    if counts["removed"] or counts["downgraded"] or counts["added"]:
        harness.tracer.info(
            "ReviewNode", "second_pass",
            "复审完成：维持 %d / 降级 %d / 删除 %d / 补漏 %d" % (
                counts["confirmed"], counts["downgraded"], counts["removed"], counts["added"]))
    # 摘要挂进 State：接口层把它带进结果体，前端风险卡据此显示「已复核/降级/补充」。
    # 只有真跑过才写 —— 没配复审模型时保持缺省，前端不渲染这块。
    state["second_pass"] = {"ran": True, "model": provider.model, **counts}
    _record_second_pass(counts, examples)
    return out


def _filter_by_evidence(risk_points: list[dict[str, Any]], state: LegalState,
                        harness: Harness) -> list[dict[str, Any]]:
    """丢掉引用了证据池之外条文的 ``basis`` 项。

    为什么必须做：Prompt 里写了"只能引用参考资料"，但模型仍会偶发地写出
    《民法典》第 585 条 —— 而本次检索根本没命中这条。这类依据看起来极其合理，
    律师几乎不会怀疑，但一旦对方律师去核就会发现引用不存在，
    属于最典型的幻觉。所以宁可在依据列留一句"（需补充法律依据）"。
    """
    pool = _citation_pool(state)
    out: list[dict[str, Any]] = []
    dropped = 0

    for rp in risk_points:
        if not isinstance(rp, dict):
            continue
        raw_basis = rp.get("basis") or []
        kept = [b for b in raw_basis if _in_pool(b, pool)]
        dropped += len(raw_basis) - len(kept)
        rp["basis"] = kept
        rp["level"] = rp.get("level") if rp.get("level") in LEVEL_ORDER else "medium"
        rp.setdefault("citations", [])
        rp.setdefault("clause_index", _find_clause_index(state, rp.get("clause_no") or ""))
        # 条款原文以**后端拆条时存下的原文**为准：模型偶尔回抄的 clause_text 片段
        # 可能截断或走样，而 IntakeNode 的原文是权威的（revise_service 同一口径）。
        # 对照稿表格的「原文」列（doc_tools.revision_draft_generate / _fallback_revision）
        # 读的就是这个字段 —— 这里回填后，那两处无需再关心原文来自谁。
        backend_text = _clause_text_of(state, rp.get("clause_no") or "")
        if backend_text:
            rp["clause_text"] = backend_text
        out.append(rp)

    out = sort_risks(out, state)   # 合同顺序，同一条款内严重程度在前
    if dropped:
        harness.gaps("review", f"{dropped} 处法律依据不在本次检索证据内，已剔除",
                     "这些风险点的依据需律师自行补充条文后再引用")
        harness.tracer.warn("ReviewNode", "basis_dropped",
                            f"剔除 {dropped} 条无来源依据（不在检索证据池内）")
    return out


def _citation_pool(state: LegalState) -> set[str]:
    """证据池：本次检索到的法条与类案，规范化为 ``法规名|条文号`` 形式的比较键。

    条文号走统一的 ``normalize_article_no``（中文数字→阿拉伯），否则
    「民法典第八百五十五条」与「民法典第855条」会被判成两条不同法条。
    """
    from backend.parser.structure import normalize_article_no

    pool: set[str] = set()
    for s in state.get("statutes") or []:
        name = _plain_law(s.get("doc_name") or "")
        art = normalize_article_no(s.get("article_no") or "")
        if name:
            pool.add(f"{name}|{art}")
            # 法规级通配：只提法规名不提条号的引用（罕见但合法）也算有来源
            pool.add(f"{name}|")
    for c in state.get("cases") or []:
        if c.get("case_no"):
            # **算式必须放到 f-string 外面。** ``f"{re.sub(r'[\s（）()]', ...)}"``
            # 这种写法只有在 Python **3.12+** 才合法（PEP 701 放宽了 f-string 里的引号
            # 与反斜杠限制），在 3.11 上是
            # ``SyntaxError: f-string expression part cannot include a backslash``。
            # 而 Docker 镜像用的正是 3.11（torch 轮子最全）—— 于是本机 3.13 跑得好好的，
            # **容器里 import 阶段就崩**，连一行日志都来不及打。
            normalized = re.sub(r"[\s（）()]", "", c["case_no"])
            pool.add(f"case|{normalized}")
        if c.get("case_name"):
            pool.add(f"case|{c['case_name']}")
    return pool


def _plain_law(name: str) -> str:
    """法规名规范化：剥书名号与空白，并去掉前缀「中华人民共和国」。

    为什么要去这个前缀：全称「《中华人民共和国民法典》第五百八十五条」与简称
    「《民法典》第585条」指的是同一条，但严格字符串比对会判成两个法规，
    于是**正确引用被拦**。前缀去掉是无损的（它不携带区分信息），所以合并是安全的。

    **刻意不做子串模糊匹配**：看起来能多兼容几种简称，实际会产生危险的假阳性 ——
    「合同法」是「劳动合同法」的子串，而《合同法》已于 2021 年废止并入《民法典》，
    放行它等于让一条失效法律冒充有效法律通过终审。
    匹配不上全称的其它简称（如司法解释的常用缩写）一律判"不可溯源"，
    让律师补全称 —— 这比猜错法规安全得多。
    """
    cleaned = re.sub(r"[《》\s　]", "", name or "")
    for prefix in ("中华人民共和国", "最高人民法院"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break
    return cleaned


def _in_pool(basis: str, pool: set[str]) -> bool:
    """依据是否可溯源。

    拆成两部分判定，**不支持"只对上法规名就算通过"** —— 那等于对民法典任何
    条文都放行，与不校验没有区别。必须法规名与条文号同时对上；
    只给法规名不给条号的情形走 ``法规名|`` 这一条通配键。
    """
    from backend.parser.structure import normalize_article_no

    text = (basis or "").strip()
    if not text:
        return False

    m = re.match(r"《([^》]+)》\s*(第[^条]{0,12}条)?", text)
    if not m:
        # 不带书名号的写法：可能是案号或案例名。
        # 注意必须包含 `case|` 前缀的键 —— 此前把它们排除在比对之外，
        # 结果类案的依据一律被判"无来源"，等于类案证据白检了。
        norm = re.sub(r"[\s（）()]", "", text)
        return any(k.split("|", 1)[-1] in (norm, text)
                   for k in pool if k.startswith("case|"))

    name = _plain_law(m.group(1))
    art = normalize_article_no(m.group(2) or "")
    if not art:
        return f"{name}|" in pool
    return f"{name}|{art}" in pool


def _find_clause_index(state: LegalState, clause_no: str) -> int:
    """按条款号定位在原文条款序列中的下标，供 EXE 端逐条高亮。"""
    if not clause_no:
        return -1
    for i, c in enumerate(state.get("clauses") or []):
        if (c.get("clause_no") or "") == clause_no:
            return int(c.get("index", i))
    return -1


def _clause_positions(state: LegalState) -> dict[str, int]:
    """条款号 → 在原文条款序列中的下标。与 :func:`_find_clause_index` 同一口径。"""
    out: dict[str, int] = {}
    for i, c in enumerate(state.get("clauses") or []):
        no = (c.get("clause_no") or "").strip()
        if no and no not in out:
            out[no] = int(c.get("index", i))
    return out


def sort_risks(items: list[dict[str, Any]], state: LegalState) -> list[dict[str, Any]]:
    """把风险点排成**合同顺序**（第一条 → 最后一条），同一条款内严重程度在前。

    为什么不是按严重程度排：原来是 ``LEVEL_ORDER`` 打头，屏幕上就是
    「第二十三条、第二十五条、第八条、第九条…」——律师是**拿着合同从头往下核**的，
    这种顺序每看一条都要回原文里翻位置（2026-09-29 用户：「风险按照顺序来」）。
    等级没有丢：每条卡片右侧的标签照旧写「高（必须改）/中（建议改）/低（可斟酌）」，
    对照稿也是一条一条对着合同改的，顺序一致才不会漏改。

    下标一律**自己按条款号重算**，不信条目里那个 ``clause_index``：那个字段允许
    模型自己给（``setdefault`` 只兜底），模型偶尔会写一个对不上的数字，拿它排序
    会把卡片排得莫名其妙。算不出下标的（没有条款号 = 全文性风险）排在最后，
    它们本来就不属于"某一条"。
    """
    pos = _clause_positions(state)
    return sorted(items, key=lambda rp: _risk_sort_key(rp, pos))


def _risk_sort_key(rp: dict[str, Any], pos: dict[str, int]) -> tuple[int, int]:
    level = LEVEL_ORDER.get(rp.get("level") or "medium", 1)
    no = (rp.get("clause_no") or "").strip()
    if not no:
        # 没有条款号 = 全文性风险，不属于"某一条"，排在最后。
        # ⚠ 这里**不能**回落到 ``clause_index``：模型给全文性风险填 0 是常态，
        # 一回落它就跑到第一条前面去了。
        return 10 ** 6, level
    idx = pos.get(no, -1)
    if idx < 0:
        # 条款号在拆条结果里找不到（写成「第4条」「第 四条」这类）时，
        # 才回头看条目自己那个下标 —— 它多数时候是对的，但只是兜底。
        raw = rp.get("clause_index")
        idx = int(raw) if isinstance(raw, int) and 0 <= raw < 10 ** 6 else 10 ** 6
    return idx, level


def _clause_text_of(state: LegalState, clause_no: str) -> str:
    """按条款号取**后端拆条时存下的条款原文**（IntakeNode 产出，权威来源）。"""
    if not clause_no:
        return ""
    for c in state.get("clauses") or []:
        if (c.get("clause_no") or "") == clause_no:
            return (c.get("clause_text") or "").strip()
    return ""


# --------------------------------------------------------------------------- 对照稿
async def _revision_draft(state: LegalState, harness: Harness,
                          contract_text: str, contract_type: str) -> None:
    """第 8 步：生成修改对照稿。

    优先走 ``revision_draft_generate`` 工具（三列对照表，便于律师直接改稿）；
    工具不可用则回落到 LLM 在扫描阶段已给出的 ``revision_draft``，
    两者都没有时登记缺口 —— 不让流程因为一个渲染工具的故障而空转。
    """
    risk_points = state.get("risk_points") or []
    if not risk_points:
        harness.tracer.info("ReviewNode", "revision_skip", "无风险点，跳过对照稿生成")
        return

    res = await harness.tool("revision_draft_generate", AGENT,
                             contract_text=contract_text,
                             risk_points=risk_points,
                             contract_type=contract_type)
    if res.ok:
        draft = _pick_draft(res.data)
        if draft:
            state["revision_draft"] = draft
            harness.tracer.info("ReviewNode", "revision_ok",
                                f"修改对照稿已生成（{len(draft)} 字，来源：工具）")
            return
        why = "对照稿工具返回空内容"
    else:
        why = f"对照稿工具不可用（{res.error}）"

    # 工具没成，但兜底稿是一份**真实可交付**的对照稿。缺口要说的是"这份稿子的来源
    # 是降级路径"，而不是"没有稿子"——原先一律写「修改对照稿生成失败」，
    # 与 Skill 第 8 步判为"已完成"直接矛盾。两句都不是谎话（稿子确实在、也确实是兜底的），
    # 但摆在一起律师会认为系统在自相矛盾，进而不再相信任何一条缺口。
    harness.tracer.warn("ReviewNode", "revision_fallback", why)
    harness.gaps("review", f"{why}，已按风险点清单整理兜底对照稿",
                 "兜底稿的「修改后」表述直接取自风险点建议，未结合交易背景润色，需律师确认")
    if not state.get("revision_draft"):
        state["revision_draft"] = _fallback_revision(risk_points, contract_type)


# 工具返回体的正文键名清单。**必须逐个列全**：工具给的是 ``revision_draft``，
# 此处原先只找 markdown/draft/content —— 于是工具每次都成功、每次都被判成
# "返回空内容"，白跑一趟并留下一句误导性的缺口，而真正的产出（工具版对照稿，
# 含三列对照表）被丢弃换成了兜底稿。
DRAFT_KEYS = ("revision_draft", "draft", "markdown", "content", "text")


def _pick_draft(data: Any) -> str:
    """从工具返回体里取对照稿正文；取不到返回空串。"""
    if isinstance(data, str):
        return data.strip()
    if not isinstance(data, dict):
        return ""
    for key in DRAFT_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _fallback_revision(risk_points: list[dict[str, Any]], contract_type: str) -> str:
    """工具不可用时的兜底对照稿：把风险点清单按对照表格式展开。

    内容全部来自已生成的风险点，不新增任何判断 —— 所以它是**安全的兜底**，
    而不是"猜一份稿子"。
    """
    lines = [f"# {contract_type or '合同'}修改对照稿（由风险点清单整理）", "",
             "> 说明：本稿由风险点清单直接展开，未经过对照稿生成工具；",
             "> 「修改后」表述需结合交易背景与客户商业诉求最终确认。", "",
             "| # | 条款 | 等级 | 原文 | 修改后 | 依据 |", "|---|---|---|---|---|---|"]
    for i, rp in enumerate(risk_points, start=1):
        level = {"high": "高（必须改）", "medium": "中（建议改）", "low": "低（提示性）"}.get(
            rp.get("level"), rp.get("level") or "")
        lines.append(
            f"| {i} | {_cell(rp.get('clause_no') or '全文')} | {level} | "
            f"{_cell((rp.get('clause_text') or '（缺失该条款）')[:120])} | "
            f"{_cell(rp.get('suggestion') or '')} | {_cell('；'.join(rp.get('basis') or []) or '（需补充法律依据）')} |"
        )
    return "\n".join(lines)


def _cell(value: Any) -> str:
    """Markdown 表格单元格转义：竖线与换行会破坏表格结构。"""
    return str(value or "").replace("|", "\\|").replace("\n", " ")


# --------------------------------------------------------------------------- 辅助
def _contract_type(state: LegalState, text: str) -> str:
    """合同类型：优先用 State 里已有的，其次用规则判定（不用 LLM，保证可复现）。"""
    existing = (state.get("doc_type") or "")
    from backend.llm.rule_engine import _guess_contract_type

    return _guess_contract_type(text) if existing in ("", "contract", "unknown") else existing


def _client_role(state: LegalState) -> str:
    """审查立场。**显式传入的优先**，其次从提问里正则抓，都拿不到就中立。

    显式那一路是给界面用的（结果页点「按乙方立场重审」）：下面那条正则只认
    ``甲方|乙方|出租人|……`` 这几个词，而用户在界面上选立场时，提问串里
    根本不会出现这些字 —— 只靠正则的话，界面选了乙方、后端照样按**中立**跑，
    而且从报告到日志一切正常，没有一处会报错。这种"选了没生效"只能靠
    显式传值从根上避免，不能在正则上打补丁。

    这条决定了"往哪边改"：站在乙方角度，违约金条款是"过高可争取下调"；
    站在甲方角度则是"过低约束不足"。同一个条款两种相反的建议，
    所以立场不能猜错 —— 猜不到时宁可双向提示，也不要默认某一方。
    """
    explicit = (state.get("client_role") or "").strip()
    if explicit:
        return explicit
    query = state.get("query") or ""
    m = CLIENT_ROLE_RE.search(query)
    return m.group(1) if m else ""


def _record_notes(state: LegalState, harness: Harness, contract_type: str,
                  client_role: str, text: str) -> None:
    """把审查口径记进 notes，让最终报告能说清"按什么立场、什么范围审的"。"""
    notes = state.get("notes") or []
    notes.append(f"合同审查口径：类型={contract_type}，立场={client_role or '中立'}，"
                 f"条款数={len(state.get('clauses') or [])}，正文长度={len(text)}")
    if not client_role:
        notes.append("未识别到审查立场，本次风险建议按双向提示给出，需律师按委托方立场取舍")
    state["notes"] = notes
    harness.tracer.info("ReviewNode", "scope", notes[-1])


def _store_confidence(state: LegalState, result: dict[str, Any]) -> None:
    """把审查自身的置信度记进 citation_summary，最终置信度由 harness.finish() 统一计算。

    不直接写 ``state["confidence"]``：置信度是多因素合成的结果，
    让单个节点覆写它会导致后跑的节点把前序证据的分量抹掉。
    """
    summary = state.get("citation_summary") or {}
    if result.get("confidence") is not None:
        summary["review_confidence"] = result["confidence"]
    summary["risk_count"] = len(state.get("risk_points") or [])
    summary["high_risk"] = sum(1 for r in (state.get("risk_points") or []) if r.get("level") == "high")
    state["citation_summary"] = summary


def _maybe_spot_check(state: LegalState, harness: Harness) -> None:
    """按抽样比例随机把本轮标记为「待人工抽查」。比例 0 或抽不中都不标记。

    命中后除了在结果里打标，还把任务写进**待抽查队列**（spot_check_service）——
    那里是人工审核的入口：律师逐条核，点「通过 / 有误」，结论再回灌自我改进。
    """
    rate = float(getattr(settings, "REVIEW_SPOT_CHECK_RATE", 0) or 0)
    if rate <= 0:
        return
    if random.random() >= rate:
        return
    state["spot_check"] = {"needed": True, "rate": rate}
    harness.tracer.info("ReviewNode", "spot_check",
                        f"本轮审查按 {rate:.0%} 抽样比例被标记为待人工抽查")
    from backend.app.services.spot_check_service import record_pending

    record_pending(
        state.get("task_id") or "",
        state.get("query") or "",
        len(state.get("risk_points") or []),
        state.get("case_id") or "",
    )


def review_digest(state: LegalState) -> dict[str, Any]:
    """审查结果摘要，供 ReportNode 组装结论。"""
    rps = state.get("risk_points") or []
    return {
        "total": len(rps),
        "by_level": {lv: sum(1 for r in rps if r.get("level") == lv) for lv in LEVEL_ORDER},
        "missing_clauses": len(state.get("missing_clauses") or []),
        "has_revision": bool(state.get("revision_draft")),
    }
