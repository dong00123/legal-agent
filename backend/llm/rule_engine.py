"""确定性规则引擎（无外部模型时的降级路径）。

定位说明（重要）：
这不是"假模型"，而是**降级路径 + 引用纪律的代码级兜底**。它不生成内容，只做两件事：
1. 从已检索到的证据里抽取、归纳、按固定 Schema 组装；
2. 需要法律依据时，**只在检索结果中真实存在该条文的情况下才写入引用**，
   否则显式写「（需补充法律依据）」。

因此它天然满足「无来源不出引用」的红线——这是生产环境断网/模型服务故障时的安全网，
也是 LLM 输出异常时的同构兜底。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("legal_agent.llm.rule")


# --------------------------------------------------------------------------- 契约
@dataclass
class RiskRule:
    id: str
    name: str
    level: str
    risk: str
    suggestion: str
    # 期望的法律依据：(法规名关键词, 条文号)
    cite: tuple[str, str] | None = None
    # 补充依据：同一条风险由多个条文共同支撑时用（如逾期付款既涉违约金约定亦涉赔偿范围）
    cites: tuple[tuple[str, str], ...] = ()
    # 按合同类型**替换**依据。key 与 ``contract`` 同构（子串匹配）。
    # 为什么必须能替换而不是一律追加：保密义务在劳动合同下依《劳动合同法》第二十三条，
    # 在其他合同下依《民法典》第五百零一条 —— 两条同时写上去，其中必有一条是错的，
    # 而**引错法比不引法更糟**：不引法只是缺依据，引错法会被对方律师直接拿去打。
    cites_by_contract: dict[str, tuple[str, str]] = field(default_factory=dict)
    trigger: str = ""          # 命中该正则时触发
    absent: str = ""           # 全文缺失该正则时触发
    scope: str = "clause"      # clause / document
    contract: tuple[str, ...] = ()   # 适用的合同类型关键词（空 = 全部类型通用）

    def cites_for(self, contract_type: str) -> list[tuple[str, str]]:
        """该规则在该类合同下的期望依据（去重保序）。"""
        ct = contract_type or ""
        special = [v for k, v in self.cites_by_contract.items() if k and k in ct]
        out: list[tuple[str, str]] = list(special) if special else ([self.cite] if self.cite else [])
        if not special:
            out.extend(self.cites)
        seen: set[tuple[str, str]] = set()
        return [c for c in out if not (c in seen or seen.add(c))]


CONTRACT_RISK_RULES: list[RiskRule] = [
    RiskRule(
        id="penalty_too_high",
        name="违约金比例畸高",
        level="high",
        risk="违约金约定比例明显高于可预见损失，存在被法院或仲裁机构酌减的风险；"
             "同时过高的违约金可能被认定为变相惩罚，反而不利于守约方主张。",
        suggestion="建议调整为「以实际损失为基础，最高不超过合同总金额的30%」或"
                   "约定「按日万分之X计算逾期违约金」，并保留举证实际损失的证据链。",
        cite=("民法典", "第五百八十五条"),
        trigger=r"违约金[^。\n]{0,40}?(?:合同总(?:金额|额|价款)|总金额|价款总额)[^。\n]{0,10}?(?:的)?\s*(\d{2,3})\s*%",
    ),
    RiskRule(
        id="penalty_percent_high",
        name="违约金比例偏高",
        level="medium",
        risk="约定的违约金比例高于司法实践中通常认可的参考上限（实际损失的30%）。",
        suggestion="建议下调比例，或改为「以实际损失为限，由守约方举证」的表述。",
        cite=("民法典", "第五百八十五条"),
        trigger=r"违约金[^。\n]{0,30}?(\d{2,3})\s*%",
    ),
    RiskRule(
        id="deposit_over_20",
        name="定金比例超过法定上限",
        level="high",
        risk="定金数额超过主合同标的额20%的部分不产生定金效力，超额部分仅能按预付款处理。",
        suggestion="建议将定金比例调整为不超过主合同标的额的20%，超额部分改为预付款并在合同中区分表述。",
        cite=("民法典", "第五百八十六条"),
        trigger=r"定金[^。\n]{0,30}?(\d{2,3})\s*%",
    ),
    RiskRule(
        id="lease_term_over_20y",
        name="租赁期限超过20年",
        level="high",
        risk="租赁期限超过20年的，超过部分无效。",
        suggestion="建议将租赁期限调整为20年以内，长期使用需求可通过约定到期后的续租权（同等条件优先承租）解决。",
        cite=("民法典", "第七百零五条"),
        trigger=r"(?:租赁期|租期)[^。\n]{0,20}?(\d{2,3})\s*年",
        contract=("租赁",),
    ),
    RiskRule(
        id="no_termination",
        name="未约定合同解除条件",
        level="medium",
        risk="全文未见解除权约定，一旦发生根本违约，只能依赖法定解除，程序成本与不确定性都会上升。",
        suggestion="建议增加解除条款：明确根本违约情形、催告期（建议15-30日）、解除通知的送达方式与生效时点。",
        cite=("民法典", "第五百六十三条"),
        absent=r"解除",
        scope="document",
    ),
    RiskRule(
        id="no_force_majeure",
        name="缺失不可抗力条款",
        level="low",
        risk="未约定不可抗力条款，发生政策变化、自然灾害等情形时责任分担依据不足。",
        suggestion="建议增加不可抗力条款：定义范围、通知义务与期限、证明文件、免责范围及合同处理方式（变更或解除）。",
        cite=("民法典", "第五百九十条"),
        absent=r"不可抗力",
        scope="document",
    ),
    RiskRule(
        id="no_confidentiality",
        name="缺失保密条款",
        level="low",
        risk="未约定保密义务，商业信息、客户资料、技术资料泄露时缺乏合同救济依据。",
        suggestion="建议增加保密条款：明确保密信息范围、保密期限（建议合同终止后继续有效2-3年）、违约责任。",
        cite=("民法典", "第五百零一条"),
        cites_by_contract={"劳动": ("劳动合同法", "第二十三条")},
        absent=r"保密",
        scope="document",
    ),
    RiskRule(
        id="no_service_address",
        name="缺失送达地址确认条款",
        level="low",
        risk="未约定送达地址与电子送达方式，诉讼或仲裁阶段可能出现送达不能、影响审理效率。",
        suggestion="建议增加送达地址确认条款，明确地址、联系人、电话、电子邮箱，并约定地址变更的通知义务与"
                   "「按约定地址寄送即视为送达」的效力。",
        cite=("民事诉讼法", "第九十条"),
        absent=r"送达",
        scope="document",
    ),
    RiskRule(
        id="jurisdiction_vague",
        name="管辖约定不明",
        level="medium",
        risk="管辖条款未明确指向具体法院或仲裁委员会，可能导致管辖约定无效、适用法定管辖。",
        suggestion="建议明确表述为「由XX人民法院管辖」或「提交XX仲裁委员会仲裁」，并注意不得同时约定诉讼与仲裁。",
        cite=("民事诉讼法", "第三十五条"),
        trigger=r"(?:争议|纠纷)[^。\n]{0,30}?(?:管辖|解决方式)[^。\n]{0,30}?(?!人民法院)(?!仲裁委员会)",
    ),
    RiskRule(
        id="trial_period_over_6m",
        name="试用期超过法定期限",
        level="high",
        risk="试用期期限超过法定上限，超出部分无效，用人单位可能面临按已履行试用期期限支付赔偿金的风险。",
        suggestion="按合同期限调整试用期：3个月以上不满1年→不超过1个月；1年以上不满3年→不超过2个月；"
                   "3年以上及无固定期限→不超过6个月；同一用人单位与同一劳动者只能约定一次试用期。",
        cite=("劳动合同法", "第十九条"),
        trigger=r"试用期[^。\n]{0,20}?([7-9]|1[0-2])\s*个?月",
        contract=("劳动",),
    ),
    RiskRule(
        id="noncompete_no_compensation",
        name="竞业限制未约定经济补偿",
        level="high",
        risk="约定竞业限制但未约定经济补偿，劳动者履行竞业限制义务后仍可主张补偿，用人单位处于被动地位。",
        suggestion="建议明确竞业限制的期限（不超过2年）、范围、地域，并约定按月支付经济补偿的标准"
                   "（建议不低于离职前12个月平均工资的30%且不低于当地最低工资标准）。",
        cite=("劳动合同法", "第二十四条"),
        trigger=r"竞业限制",
        contract=("劳动",),
    ),
    RiskRule(
        id="labor_penalty",
        name="约定劳动者承担违约金",
        level="high",
        risk="除服务期与竞业限制两种情形外，用人单位不得与劳动者约定由劳动者承担违约金，该约定可能被认定无效。",
        suggestion="建议删除对劳动者的一般违约金条款，改为约定损失赔偿的计算方式或纪律处分措施。",
        cite=("劳动合同法", "第二十五条"),
        trigger=r"劳动者[^。\n]{0,20}?(?:支付|承担)[^。\n]{0,10}违约金",
        contract=("劳动",),
    ),
    RiskRule(
        id="wage_delay_no_interest",
        name="逾期付款未约定利息或违约金",
        level="medium",
        risk="约定了付款义务但未约定逾期付款的利息或违约金，主张损失时需要额外举证。",
        suggestion="建议约定「逾期付款按全国银行间同业拆借中心公布的贷款市场报价利率（LPR）计算利息」"
                   "或约定明确的日万分之X违约金。",
        # 两条依据各支撑建议的一半：585 支撑"约定违约金"，584 支撑"损失赔偿额的范围与举证"。
        cite=("民法典", "第五百八十五条"),
        cites=(("民法典", "第五百八十四条"),),
        trigger=r"(?:付款|支付|价款)[^。\n]{0,40}?(?!逾期)(?!违约)(?!利息)",
    ),
]

RISK_LEVEL_ORDER = {"high": 0, "medium": 1, "low": 2}


# --------------------------------------------------------------------------- 条文需求
def rules_for_contract(contract_type: str) -> list[RiskRule]:
    """该类合同适用的风险规则（把合同专属规则筛出来）。

    为什么要筛：``no_termination`` / ``no_force_majeure`` 这类 ``absent`` 规则
    对任何合同都会触发，而 ``trial_period_over_6m`` 只在劳动合同上成立。
    不筛的后果不是"多报几条"，而是 ``required_cites`` 会把
    **劳动合同法的条文塞进房屋租赁合同的检索式**，检索回来的法条与本次审查
    毫无关系，却因为"在证据池里"而被放行引用。
    """
    ct = contract_type or ""
    return [r for r in CONTRACT_RISK_RULES
            if not r.contract or any(k in ct for k in r.contract)]


def required_cites(contract_type: str) -> list[tuple[str, str]]:
    """该类合同审查**将要引用**的条文（规则声明的期望依据），去重保序。

    这是"检索式"的唯一事实来源。为什么必须由它来生成检索式：
    只给「违约责任 违约金 调整」这类关键词时，向量检索命中的是"最像的"条文
    （实测命中民法典第592条），而规则真正要用的是第五百八十五条 ——
    「最像的」≠「要用的」。结果是风险点全部落成「需补充法律依据」，
    引用终审因为无引用可校而 ``checked=0`` 却 ``passed=True``，
    整条反幻觉红线看着在工作、实际空转。
    """
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for r in rules_for_contract(contract_type):
        for c in r.cites_for(contract_type):
            if c not in seen:
                seen.add(c)
                out.append(c)
    return out


# --------------------------------------------------------------------------- 工具
def _parse_messages(messages: list[dict[str, str]]) -> tuple[str, str]:
    system = ""
    user = ""
    for m in messages:
        if m.get("role") == "system" and not system:
            system = m.get("content") or ""
        elif m.get("role") == "user":
            user = (user + "\n" + (m.get("content") or "")).strip()
    return system, user


def _parse_context(messages: list[dict[str, str]]) -> list[tuple[str, str]]:
    """从参考资料块里解析出 [S1]/[C2]/[K3] 证据。"""
    blocks: list[tuple[str, str]] = []
    for m in messages:
        content = m.get("content") or ""
        if "【参考资料】" in content:
            for line in content.splitlines():
                mm = re.match(r"^\[([SCK]\d+)\]\s*(.*)$", line.strip())
                if mm:
                    blocks.append((mm.group(1), mm.group(2)))
    return blocks


def _statute_index(statutes: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(法规名, 条文号) 索引，用于「有来源才引用」判定。"""
    out: list[tuple[str, str]] = []
    for s in statutes or []:
        name = (s.get("doc_name") or "").strip("《》")
        art = (s.get("article_no") or "").strip()
        if name or art:
            out.append((name, art))
    return out


def _plain_law(name: str) -> str:
    """法规名规范化：剥书名号与空白，去掉「中华人民共和国」前缀。

    与 ``review_node._plain_law`` 同一套规则 —— 这里**不能**用子串匹配：
    「劳动合同法」和「劳动法」是两部不同的法律，而「劳动合同法」是
    ……法 的子串关系方向也不一致。两处各写一套的结果是同一个引用在
    「依据能不能落到风险点」和「终审能不能溯源」两个环节得到不同结论。
    """
    cleaned = re.sub(r"[《》\s　]", "", name or "")
    for prefix in ("中华人民共和国", "最高人民法院"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break
    return cleaned


def _find_citation(statutes: list[dict[str, Any]], law_kw: str, article: str) -> str | None:
    """在检索结果中查找指定条文；找不到返回 None（上层写「需补充法律依据」）。

    **条文号必须走 ``article_key`` 归一后再比**，不能拿字符串直接比：
    规则表里声明的是「第五百八十五条」（人手写法），而检索结果里存的是
    「第585条」（入库时归一过的写法）。两者永远不等 —— 表现是**每一条**
    风险点的 ``basis`` 都为空、正文写着「需补充法律依据」，
    而引用终审因为无引用可校而 ``checked=0`` 却 ``passed=True``。
    整条「无来源不出引用」的红线在合同审查路径上等于没接线。
    """
    from backend.parser.structure import article_key

    want_art = article_key(article)
    want_law = _plain_law(law_kw)
    for s in statutes or []:
        name = _plain_law(s.get("doc_name") or "")
        if want_law and want_law not in name:
            continue
        if want_art and article_key(s.get("article_no") or "") != want_art:
            continue
        return f"{s.get('doc_name') or law_kw}{s.get('article_no') or ''}"
    return None


def _status_of(statutes: list[dict[str, Any]], cite: str) -> str:
    """按 ``_find_citation`` 返回的引用串回查效力状态。

    比较同样走归一：``cite`` 里带的是检索结果里的原始写法，
    而这里要重算一次键，任何一处漏归一都会让效力状态静默变成空字符串 ——
    报告里那条法条就不带「（现行有效）」，看上去只是少了个注记，
    实际是「时效性校验」红线在该条上没被执行。
    """
    from backend.parser.structure import article_key

    m = re.match(r"^(.*?)(第\s*[一二三四五六七八九十百零两\d]+\s*条(?:之[一二三四五六七八九十]+)?)?$", cite or "")
    if not m:
        return ""
    want_law = _plain_law(m.group(1))
    want_art = article_key(m.group(2) or "")
    for s in statutes or []:
        name = _plain_law(s.get("doc_name") or "")
        art = article_key(s.get("article_no") or "")
        if name == want_law and art == want_art:
            return s.get("status") or ""
    return ""


# --------------------------------------------------------------------------- handlers
def _h_intent(payload: dict[str, Any], messages: list[dict[str, str]]) -> str:
    from backend.parser.doc_type import judge_doc_type

    query = (payload.get("query") or "").strip()
    text = payload.get("text") or ""
    doc = payload.get("doc_type") or ""

    rules: list[tuple[str, str]] = [
        ("contract_review", r"审(查|阅|核)?[^。]{0,10}(合同|协议)|合同(风险|条款)|(审查|修改)合同|帮我审"),
        ("doc_draft", r"起草|写一份|拟一份|生成(起诉状|答辩状|意见书|申请书)|(文书|起诉状|答辩状|意见书)草稿"),
        ("case_search", r"类案|判例|裁判要旨|相似案例|检索案例|找案例"),
        ("statute_lookup", r"第[一二三四五六七八九十百零两\d]+条|法条|条文|法规检索|《[^》]+》.*条"),
        ("case_analysis", r"案件分析|分析(这个|该)?案件|争议焦点|诉讼策略|攻防|诉讼时效"),
        ("consult", r"请问|如何|是否|能不能|可以吗|怎么办"),
    ]
    for intent, pattern in rules:
        if re.search(pattern, query):
            return json.dumps(
                {"intent": intent, "doc_type": doc or "unknown", "confidence": 0.75,
                 "reason": f"规则匹配：{pattern[:20]}"},
                ensure_ascii=False,
            )

    if text:
        dt = judge_doc_type(text)
        if dt.doc_type == "contract":
            return json.dumps({"intent": "contract_review", "doc_type": "contract", "confidence": 0.7,
                               "reason": "提供了合同文本"}, ensure_ascii=False)
        if dt.doc_type == "litigation":
            return json.dumps({"intent": "case_analysis", "doc_type": "litigation", "confidence": 0.7,
                               "reason": "提供了诉讼材料"}, ensure_ascii=False)
    return json.dumps({"intent": "consult", "doc_type": doc or "consult", "confidence": 0.5,
                       "reason": "默认按法律咨询处理"}, ensure_ascii=False)


def _h_extract(payload: dict[str, Any], messages: list[dict[str, str]]) -> str:
    from backend.parser.structure import extract_parties, split_clauses

    text = payload.get("text") or ""
    clauses = split_clauses(text)
    parties = extract_parties(text)
    from backend.parser.structure import extract_facts

    facts = extract_facts(text)
    from backend.parser.doc_type import judge_doc_type

    dt = judge_doc_type(text, payload.get("filename", ""))
    return json.dumps(
        {
            "doc_type": dt.doc_type,
            "parties": [{"name": p.name, "role": p.role, "kind": p.kind} for p in parties],
            "facts": facts,
            "clauses": [
                {"clause_no": c.clause_no, "clause_title": c.clause_title,
                 "clause_text": c.clause_text[:600]}
                for c in clauses
            ],
        },
        ensure_ascii=False,
    )


def _h_issues(payload: dict[str, Any], messages: list[dict[str, str]]) -> str:
    facts = payload.get("facts") or []
    statutes = payload.get("statutes") or []
    cases = payload.get("cases") or []

    issues: list[dict[str, Any]] = []
    fact_text = " ".join(f.get("text", "") if isinstance(f, dict) else str(f) for f in facts)

    patterns = [
        (r"解除|辞退|离职|开除", "解除劳动合同是否合法及赔偿/补偿责任", "high"),
        (r"加班|工时|休息|年休假", "加班事实认定与加班费计算基数", "medium"),
        (r"工资|报酬|拖欠|欠付", "劳动报酬支付义务与逾期责任", "high"),
        (r"社保|社会保险|工伤", "社会保险缴纳义务与待遇损失赔偿责任", "high"),
        (r"违约金", "违约金约定是否过高及可否酌减", "medium"),
        (r"竞业限制|保密", "竞业限制的效力与补偿标准", "medium"),
        (r"时效|仲裁时效|诉讼时效", "请求是否超过仲裁/诉讼时效", "high"),
        (r"租赁|租金|房屋", "租赁合同履行与违约责任", "medium"),
        (r"货款|买卖|交付|验收", "买卖合同履行、交付与质量责任", "medium"),
    ]
    for pattern, title, level in patterns:
        if re.search(pattern, fact_text):
            basis = []
            for s in statutes[:2]:
                basis.append(f"{(s.get('doc_name') or '').strip('《》')}{s.get('article_no') or ''}")
            issues.append(
                {"title": title,
                 "description": f"依据案件事实中与「{pattern}」相关的内容归纳。",
                 "severity": level, "basis": [b for b in basis if b.strip()]}
            )
    if not issues and statutes:
        issues.append(
            {
                "title": "适用法律与请求权基础",
                "description": "需结合检索到的法条进一步明确请求权基础。",
                "severity": "medium",
                "basis": [f"{(s.get('doc_name') or '').strip('《》')}{s.get('article_no') or ''}" for s in statutes[:3]],
            }
        )
    if cases:
        issues.append(
            {
                "title": "类案裁判倾向",
                "description": "参照检索到的类案裁判要旨判断裁判倾向。",
                "severity": "medium",
                "basis": [c.get("case_name") or c.get("case_no") or "" for c in cases[:3]],
            }
        )
    return json.dumps({"issues": issues}, ensure_ascii=False)


def _h_contract_review(payload: dict[str, Any], messages: list[dict[str, str]]) -> str:
    """规则版合同审查：逐条风险扫描，依据只在检索证据中存在时才引用。"""
    text = payload.get("contract_text") or ""
    clauses = payload.get("clauses") or []
    statutes = payload.get("statutes") or []
    knowledges = payload.get("knowledges") or []
    contract_type = payload.get("contract_type") or _guess_contract_type(text)

    if not clauses:
        from backend.parser.structure import split_clauses

        clauses = [
            {"clause_no": c.clause_no, "clause_title": c.clause_title, "clause_text": c.clause_text, "index": c.index}
            for c in split_clauses(text)
        ]

    risk_points: list[dict[str, Any]] = []
    used_rule_ids: set[str] = set()

    # 只跑该类合同适用的规则：劳动合同的"试用期上限"规则套到房屋租赁合同上，
    # 是一条凭空的伪造风险 —— 律师据此改合同会改错地方。
    for rule in rules_for_contract(contract_type):
        if rule.scope == "document":
            hit = bool(rule.absent) and not re.search(rule.absent, text)
            target_clause = {"clause_no": "全文", "clause_title": "", "clause_text": "", "index": -1}
        else:
            hit = False
            target_clause = None
            for c in clauses:
                body = c.get("clause_text") or ""
                if rule.trigger and re.search(rule.trigger, body):
                    hit = True
                    target_clause = c
                    break
        if not hit or target_clause is None:
            continue

        basis, basis_note = [], ""
        wanted = rule.cites_for(contract_type)
        for law_kw, article in wanted:
            cite = _find_citation(statutes, law_kw, article)
            if not cite:
                continue
            status = _status_of(statutes, cite)
            basis.append(f"{cite}（{status}）" if status else cite)
        if wanted and not basis:
            basis_note = "（需补充法律依据：本次法规检索未命中对应条文）"

        risk_points.append(
            {
                "clause_no": target_clause.get("clause_no") or "",
                "clause_title": target_clause.get("clause_title") or "",
                "clause_text": (target_clause.get("clause_text") or "")[:500],
                "risk": rule.risk + (basis_note and " " + basis_note or ""),
                "level": rule.level,
                "suggestion": rule.suggestion,
                "basis": basis,
                "clause_index": target_clause.get("index", -1),
            }
        )
        used_rule_ids.add(rule.id)

    risk_points.sort(key=lambda r: RISK_LEVEL_ORDER.get(r["level"], 9))

    # 缺失条款：由审查清单（知识库）比对得出
    missing = _missing_clauses(contract_type, text, knowledges)

    revision = _revision_draft(contract_type, risk_points)
    confidence = 0.62 if risk_points else 0.45
    return json.dumps(
        {
            "contract_type": contract_type,
            "risk_points": risk_points,
            "missing_clauses": missing,
            "revision_draft": revision,
            "confidence": confidence,
        },
        ensure_ascii=False,
    )


CONTRACT_TYPE_RULES = [
    (r"房屋租赁|租赁合同|出租人|承租人|租金", "房屋租赁合同"),
    (r"劳动合同|用人单位|劳动者|试用期|竞业限制", "劳动合同"),
    (r"买卖|供货|货款|买受人|出卖人", "买卖合同"),
    (r"服务|技术服务|咨询|委托", "服务合同"),
    (r"借款|借贷|利息|本金", "借款合同"),
    (r"建设工程|施工|工程款", "建设工程施工合同"),
]


def _guess_contract_type(text: str) -> str:
    for pattern, name in CONTRACT_TYPE_RULES:
        if re.search(pattern, text or ""):
            return name
    return "通用合同"


MUST_HAVE_CLAUSES = {
    "房屋租赁合同": ["租赁物与用途", "租赁期限", "租金及支付方式", "押金", "维修责任", "违约责任", "争议解决", "送达"],
    "买卖合同": ["标的与质量标准", "价款与支付", "交付与验收", "风险转移", "违约责任", "争议解决"],
    "劳动合同": ["工作内容与地点", "劳动报酬", "社会保险", "试用期", "解除与终止", "竞业限制", "送达"],
    "服务合同": ["服务内容与交付物", "服务费与支付", "知识产权归属", "保密", "验收", "违约责任"],
    "通用合同": ["标的", "价款", "履行期限", "违约责任", "争议解决", "不可抗力"],
}


def _missing_clauses(contract_type: str, text: str, knowledges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name in MUST_HAVE_CLAUSES.get(contract_type, MUST_HAVE_CLAUSES["通用合同"]):
        key = name.replace("与", "").replace("及", "")[:4]
        if key and key not in (text or ""):
            out.append(
                {
                    "name": name,
                    "reason": f"合同类型「{contract_type}」的常规必备条款中未见「{name}」，建议补充。"
                              + (f"（对照本所审查清单：{knowledges[0].get('title')}）" if knowledges else ""),
                    "level": "medium" if len(out) < 2 else "low",
                }
            )
    return out[:6]


def _revision_draft(contract_type: str, risk_points: list[dict[str, Any]]) -> str:
    lines = [f"# {contract_type}修改对照稿", "", "> 说明：以下为按风险点逐条给出的修改建议，", 
             "> 「修改后」表述需结合交易背景与客户商业诉求最终确认。", ""]
    if not risk_points:
        lines.append("本次规则化扫描未识别出明显风险点，建议人工复核全文。")
        return "\n".join(lines)
    for i, rp in enumerate(risk_points, start=1):
        lines.append(f"## {i}. {rp.get('clause_no') or '全文'} {rp.get('clause_title') or ''}".rstrip())
        lines.append(f"**风险等级**：{ {'high':'高（必须改）','medium':'中（建议改）','low':'低（提示性）'}.get(rp['level'], rp['level']) }")
        lines.append(f"**原文**：{(rp.get('clause_text') or '（缺失该条款）')[:200]}")
        lines.append(f"**修改后**：{rp.get('suggestion')}")
        basis = [b for b in (rp.get("basis") or []) if b]
        lines.append(f"**依据**：{'；'.join(basis) if basis else '（需补充法律依据）'}")
        lines.append("")
    return "\n".join(lines)


def _h_case_analysis(payload: dict[str, Any], messages: list[dict[str, str]]) -> str:
    facts = payload.get("facts") or []
    statutes = payload.get("statutes") or []
    cases = payload.get("cases") or []
    facts_text = " ".join(f.get("text", "") if isinstance(f, dict) else str(f) for f in facts)

    issues_raw = json.loads(_h_issues(payload, messages))
    issues = issues_raw.get("issues", [])

    strategy: list[str] = []
    if statutes:
        cites = [f"{(s.get('doc_name') or '').strip('《》')}{s.get('article_no') or ''}" for s in statutes[:3]]
        strategy.append("我方攻点：以 " + "、".join([c for c in cites if c.strip()]) + " 作为请求权基础，"
                        "主张对方承担相应责任。")
    else:
        strategy.append("我方攻点：本次未检索到有效法条支撑，需先补齐法规检索再确定攻点。")
    if cases:
        strategy.append(f"类案参照：检索到 {len(cases)} 个同类案例，"
                        f"可参照「{cases[0].get('case_name') or cases[0].get('case_no')}」的裁判要旨。")
    else:
        strategy.append("类案参照：本次类案检索未取得有效支撑，攻防要点暂缺类案依据（见证据缺口）。")
    strategy.append("对方可能的抗辩：① 主张事实不成立，需核查证据链完整性；"
                    "② 主张时效届满，需核查起算点与中断事由；③ 主张责任比例或数额过高。")
    strategy.append("应对：提前固定证据原件与电子数据的完整性，形成时间轴；"
                    "对时效问题准备中断/中止的证据材料。")

    limitation: dict[str, Any] = {"period": "", "start_point": "", "conclusion": "", "basis": []}
    if re.search(r"劳动|用人单位|劳动者", facts_text) or any("劳动" in (s.get("doc_name") or "") for s in statutes):
        limitation["period"] = "劳动仲裁时效 1 年"
        limitation["start_point"] = "自当事人知道或应当知道其权利被侵害之日起计算"
        cite = _find_citation(statutes, "劳动争议调解仲裁法", "第二十七条") or _find_citation(statutes, "劳动争议调解仲裁法", "")
        limitation["basis"] = [cite] if cite else []
        limitation["conclusion"] = "需核实具体侵权时间点与是否存在中断事由；" + ("依据：" + cite if cite else "本次未命中对应条文，需补充检索")
    else:
        limitation["period"] = "诉讼时效 3 年"
        limitation["start_point"] = "自权利人知道或应当知道权利受到损害以及义务人之日起计算"
        cite = _find_citation(statutes, "民法典", "第一百八十八条")
        limitation["basis"] = [cite] if cite else []
        limitation["conclusion"] = "需核实起算点；" + ("依据：" + cite if cite else "本次未命中对应条文，需补充检索")

    gaps: list[str] = []
    # 措辞取自 ``agents.state`` 的规范常量，与 AnalysisNode 登记的**逐字一致**，
    # 这样 ``add_evidence_gap`` 的 kind|reason 去重才能真正生效。
    # 各写各的措辞时，同一件事会在缺口清单里并列出现两三条。
    from backend.agents.state import GAP_NO_CASES, GAP_NO_FACTS, GAP_NO_STATUTES

    if not cases:
        gaps.append(GAP_NO_CASES)
    if not statutes:
        gaps.append(GAP_NO_STATUTES)
    if not facts:
        gaps.append(GAP_NO_FACTS)

    return json.dumps(
        {
            "issues": issues,
            "strategy": strategy,
            "limitation_check": limitation,
            "evidence_gap_notes": gaps,
            "confidence": 0.6 if statutes else 0.4,
        },
        ensure_ascii=False,
    )


DRAFT_TYPE_MAP = {
    "complaint": ("民事起诉状", ["请求事项", "事实与理由", "法律依据"]),
    "defense": ("民事答辩状", ["答辩意见", "事实与理由", "法律依据"]),
    "arbitration_application": ("劳动仲裁申请书", ["仲裁请求", "事实与理由", "证据清单"]),
    "legal_opinion": ("法律意见书", ["基本情况", "分析与意见", "结论"]),
    "revision": ("合同修改稿", ["修改说明", "条款修改对照"]),
    "appeal": ("民事上诉状", ["上诉请求", "上诉理由"]),
}


def _h_draft(payload: dict[str, Any], messages: list[dict[str, str]]) -> str:
    doc_type = payload.get("doc_type") or "complaint"
    title, sections = DRAFT_TYPE_MAP.get(doc_type, ("法律文书", ["事实与理由", "法律依据"]))
    facts = payload.get("facts") or []
    issues = payload.get("issues") or []
    statutes = payload.get("statutes") or []
    cases = payload.get("cases") or []
    parties = payload.get("parties") or []

    lines = [f"# {title}", ""]
    if parties:
        lines.append("## 当事人")
        for p in parties:
            if isinstance(p, dict) and p.get("name"):
                lines.append(f"- {p.get('role') or '当事人'}：{p['name']}")
        lines.append("")

    if doc_type in ("complaint", "arbitration_application", "appeal"):
        lines.append("## 请求事项")
        for i, iss in enumerate(issues or [{"title": "依法判令对方承担相应责任"}], start=1):
            t = iss.get("title") if isinstance(iss, dict) else str(iss)
            lines.append(f"{i}. 依法判令：{t}。")
        lines.append("")

    lines.append("## 事实与理由")
    if facts:
        for i, f in enumerate(facts[:12], start=1):
            text = f.get("text") if isinstance(f, dict) else str(f)
            lines.append(f"{i}. {text}")
    else:
        lines.append("（此处需补充案件事实。事实部分仅能来自委托人提供的材料，不得虚构。）")
    lines.append("")

    if "法律依据" in sections or True:
        lines.append("## 法律依据")
        if statutes:
            for s in statutes[:8]:
                name = s.get("doc_name") or ""
                art = s.get("article_no") or ""
                status = s.get("status") or ""
                lines.append(f"- {name}{art}（{status}）")
        else:
            lines.append("（此处需补充法律依据）")
        lines.append("")

    if cases:
        lines.append("## 类案参照")
        for c in cases[:5]:
            ident = c.get("case_name") or c.get("case_no") or ""
            lines.append(f"- {ident}：{(c.get('gist') or '')[:120]}")
        lines.append("")

    lines.append("此致")
    lines.append("____人民法院")
    lines.append("")
    lines.append("具状人：____")
    lines.append("____年__月__日")
    return "\n".join(lines)


def _h_report(payload: dict[str, Any], messages: list[dict[str, str]]) -> str:
    disclaimer = payload.get("disclaimer") or ""
    statutes = payload.get("statutes") or []
    cases = payload.get("cases") or []
    knowledges = payload.get("knowledges") or []
    intent = payload.get("intent") or "consult"
    evidence = _parse_context(messages)

    lines = [f"# 法律辅助分析意见（{intent}）", "", "## 一、结论摘要"]
    summary: list[str] = []
    if statutes:
        summary.append(f"共检索到 {len(statutes)} 条相关法条，其中标注为现行有效的 "
                       f"{sum(1 for s in statutes if (s.get('status') or '') == '现行有效')} 条。")
    else:
        summary.append("本次法规检索未命中有效条文，结论范围相应缩小。")
    if cases:
        summary.append(f"共检索到 {len(cases)} 个类案，可作裁判倾向参照。")
    else:
        summary.append("本次类案检索未取得有效支撑，相关判断缺少类案依据。")
    if knowledges:
        summary.append(f"命中本所知识库 {len(knowledges)} 个片段，可对照审查清单/办案指引执行。")
    lines.extend([f"- {s}" for s in summary])
    lines.append("")

    lines.append("## 二、引用清单")
    if statutes:
        lines.append("### 法条")
        for s in statutes:
            # **不输出 source_path**。它是语料文件在服务器上的绝对路径，会出现在
            # 律师直接阅读（并可能外发）的文书正文里：既泄露服务端目录结构，
            # 类案文件名里还可能带当事人姓名。对读者而言它也毫无用处 ——
            # 想知道"依据哪部法"看 doc_name + article_no 就够了，
            # 不需要知道它存在哪个盘的哪个目录下。
            lines.append(f"- {s.get('doc_name') or ''}{s.get('article_no') or ''}"
                         f"（{s.get('status') or '未知'}）")
    if cases:
        lines.append("### 类案")
        for c in cases:
            ident = "｜".join(x for x in [c.get("case_no") or "", c.get("case_name") or "", c.get("court") or ""] if x)
            lines.append(f"- {ident}｜裁判要旨：{(c.get('gist') or '')[:150]}")
    if knowledges:
        lines.append("### 律所知识库")
        for k in knowledges:
            lines.append(f"- {k.get('title') or ''}（{k.get('doc_type') or ''}，版本 {k.get('version') or ''}）")
    if not (statutes or cases or knowledges):
        lines.append("本次未取得可引用的法条、类案或知识库来源。**不生成任何引用**。")
    lines.append("")

    if evidence:
        lines.append("## 三、检索证据片段")
        for tag, body in evidence[:8]:
            lines.append(f"- [{tag}] {body[:160]}")
        lines.append("")

    lines.append("---")
    if disclaimer:
        lines.append(f"> {disclaimer}")
    return "\n".join(lines)


def _h_general(payload: dict[str, Any], messages: list[dict[str, str]]) -> str:
    """通用问答兜底：只做证据摘录，不做自由生成。"""
    query = payload.get("query") or ""
    statutes = payload.get("statutes") or []
    cases = payload.get("cases") or []
    knowledges = payload.get("knowledges") or []

    lines = [f"针对问题「{query}」，检索结果如下（本回答仅摘录检索到的来源，不做推断性结论）：", ""]
    if statutes:
        lines.append("**相关法条**")
        for s in statutes[:6]:
            body = (s.get("text") or "")[:300]
            lines.append(f"- {s.get('doc_name') or ''}{s.get('article_no') or ''}（{s.get('status') or '未知'}）：{body}")
        lines.append("")
    if cases:
        lines.append("**类案裁判要旨**")
        for c in cases[:5]:
            ident = c.get("case_name") or c.get("case_no") or ""
            lines.append(f"- {ident}：{(c.get('gist') or '')[:200]}")
        lines.append("")
    if knowledges:
        lines.append("**本所指引**")
        for k in knowledges[:4]:
            lines.append(f"- {k.get('title')}（{k.get('version')}）：{(k.get('snippet') or '')[:200]}")
        lines.append("")
    if not (statutes or cases or knowledges):
        lines.append("未检索到相关来源。按引用纪律，本回答不给出法条或判例引用，"
                     "建议调整检索关键词，或直接提供相关材料后再分析。")
    disclaimer = payload.get("disclaimer") or ""
    if disclaimer:
        lines.append(f"> {disclaimer}")
    return "\n".join(lines)


def _last_question(messages: list[dict[str, str]]) -> str:
    """本轮问题：messages 里最后一条**不是证据/历史块**的 user 消息。

    ``build_rag_messages`` 把参考资料与历史对话都拼成了 user 角色
    （见 ``prompts.build_messages``），所以"最后一条 user"不一定就是问题 ——
    取错了会把整块证据当成问题显示出来。
    """
    for m in reversed(messages or []):
        if m.get("role") != "user":
            continue
        content = (m.get("content") or "").strip()
        if content and not content.startswith(("【参考资料】", "【历史对话】")):
            return content
    return ""


def _strip_leading_citation(text: str) -> str:
    """剥掉正文开头的「《法规名》第X条」行。

    库里每条法条的 ``text`` 都以法规名+条号开头（``《中华人民共和国民法典》
    第五百八十五条\\n【违约金】……``），而渲染时那一行已经由行首的
    ``[S1] 中华人民共和国民法典第五百八十五条`` 表达过一次了。重复显示之外还有个
    更隐蔽的后果：``extract_cites`` 的「漏标记回填」会把它当成一条漏标的引用，
    在行内再补一个标记 —— 正文于是变成 ``- [S1] …第五百八十五条[S2]``，
    同一段文字挂上两个编号。
    """
    head, sep, rest = text.partition("\n")
    if sep and head.lstrip().startswith("《") and rest.strip():
        return rest.lstrip()
    return text


def _h_rag_answer(payload: dict[str, Any], messages: list[dict[str, str]]) -> str:
    """纯 RAG 问答的兜底：只摘录检索到的来源，不做推断。

    两条**必须守住**的东西，第二条是这条链路踩过的真事：

    1. 正文要带 ``[Sn]`` / ``[Cn]`` 标记。``rag_answer.extract_cites`` 靠它们与证据池
       对齐、产出引用卡；不带标记的后果是正文写着法条、右侧「参考法规」却空着 ——
       界面把一个有依据的回答显示成没有依据。
    2. **不许把"证据没传进来"说成"没检索到"。** 调用方漏传 payload 时，证据其实还在
       ``messages`` 的【参考资料】块里（``_parse_context`` 就是解析它的），从那儿接着
       渲染即可。只有两边都空才是真的没检索到 —— 实测踩过：检索命中 8 法条 + 4 类案，
       回答却写「针对问题「」，未检索到相关来源」，问题与来源同时丢失，还谎报了检索结论。
    """
    query = (payload.get("query") or "").strip() or _last_question(messages)
    statutes = payload.get("statutes") or []
    cases = payload.get("cases") or []
    knowledges = payload.get("knowledges") or []

    lines = [f"针对问题「{query}」，检索结果如下"
             f"（当前无可用模型服务，本条为检索结果摘录，不做推断性结论）：", ""]

    if statutes or cases or knowledges:
        if statutes:
            lines.append("**相关法条**")
            for i, s in enumerate(statutes[:8], start=1):
                # 编号就是证据池下标（1-based），必须与 extract_cites 建卡时的顺序一致，
                # 错一位等于把 A 法条的依据挂到 B 法条名下。
                # ``strip("《》")`` 是为了不触发 extract_cites 的「漏标记回填」扫描：
                # STATUTE_CITE_RE 只认带书名号的写法，带上就会在行内再回填一个标记。
                name = str(s.get("doc_name") or "").strip().strip("《》")
                body = _strip_leading_citation(str(s.get("text") or ""))[:300]
                lines.append(f"- [S{i}] {name}{s.get('article_no') or ''}"
                             f"（{s.get('status') or '未知'}）：{body}")
            lines.append("")
        if cases:
            lines.append("**类案裁判要旨**")
            for i, c in enumerate(cases[:8], start=1):
                ident = c.get("case_no") or c.get("case_name") or ""
                lines.append(f"- [C{i}] {ident}｜{c.get('court') or ''}｜"
                             f"{(c.get('gist') or '')[:200]}")
            lines.append("")
        if knowledges:
            lines.append("**本所指引**")
            for i, k in enumerate(knowledges[:6], start=1):
                lines.append(f"- [K{i}] {k.get('title') or ''}"
                             f"（{k.get('doc_type') or ''}，版本 {k.get('version') or ''}）")
            lines.append("")
    else:
        # payload 里没有证据 —— 可能是这次真的没检索到，也可能是调用方漏传了。
        # 后者从【参考资料】块里捞得回来，两者必须分开表述。
        blocks = _parse_context(messages)
        if blocks:
            lines.append("**检索到的来源**")
            for tag, body in blocks[:8]:
                lines.append(f"- [{tag}] {body[:300]}")
            lines.append("")
        else:
            lines.append("未检索到相关来源。按引用纪律，本回答不给出法条或判例引用，"
                         "建议调整检索关键词，或直接提供相关材料后再分析。")

    disclaimer = payload.get("disclaimer") or ""
    if disclaimer:
        lines.append(f"> {disclaimer}")
    return "\n".join(lines)


def _h_win_rate(payload: dict[str, Any], messages: list[dict[str, str]]) -> str:
    """胜诉把握的「AI 法律分析」降级版。**返回与云端同构的 JSON**。

    只搬运 ``assess_evidence_sufficiency`` 已经算好的确定性结论（由调用方经
    ``payload["suggestions"]`` 传进来），**不新编法律判断** —— 降级状态下用户
    本来就分不清哪句是模型说的、哪句是代码编的，多写一句没有依据的结论只是添乱。

    这里必须显式登记进 ``HANDLERS``：不登记的话 ``run_task`` 会兜到 ``_h_general``
    返回 markdown，而调用方走的是 ``chat_json``，抠不出 JSON 就把整块分析变成空白，
    界面上看起来像功能坏了。
    """
    chain = payload.get("chain") or []
    metrics = payload.get("metrics") or {}
    gaps = [str(c.get("缺口") or "").strip() for c in chain if isinstance(c, dict)]
    gaps = [g for g in gaps if g]

    risks = []
    for c in chain:
        if not isinstance(c, dict):
            continue
        gap = str(c.get("缺口") or "").strip()
        if gap:
            risks.append(f"{c.get('主张') or '（未命名主张）'}：{gap}")

    reasons = []
    if metrics and not metrics.get("unsupported"):
        reasons.append("各条主张均已有证据支撑，证据链完整。")

    return json.dumps({
        "reasons": reasons,
        "risks": risks[:12],
        "next_moves": [str(s) for s in (payload.get("suggestions") or [])][:12],
        "decisions": [],
        "note": "模型不可用，以上为规则引擎根据证据链缺口生成的确定性结论。",
    }, ensure_ascii=False)


HANDLERS = {
    "intent": _h_intent,
    "extract": _h_extract,
    "issues": _h_issues,
    "contract_review": _h_contract_review,
    "case_analysis": _h_case_analysis,
    "draft": _h_draft,
    "report": _h_report,
    "general": _h_general,
    # 纯 RAG 问答（``rag_answer.answer`` 的 task 名）。**必须显式登记**：
    # 原来没有这一项，``HANDLERS.get(task, _h_general)`` 会把它静默兜到通用问答上 ——
    # 输出看着像模像样，实则拿不到这条链路的证据与编号规则。
    "rag_answer": _h_rag_answer,
    # 胜诉把握的 AI 法律分析（``case_service.WIN_TASK``）。同上，不登记就会静默
    # 兜到 _h_general，用户看到「法律分析」整块是空的。
    "win_rate": _h_win_rate,
}


def run_task(task: str, messages: list[dict[str, str]], payload: dict[str, Any]) -> str:
    handler = HANDLERS.get(task, _h_general)
    try:
        return handler(payload or {}, messages or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("规则引擎执行 %s 失败：%s", task, exc)
        return json.dumps({"error": str(exc)}, ensure_ascii=False) if task != "general" else "规则处理失败。"
