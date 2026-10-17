"""法律场景 Prompt 模板与输出契约。

引用纪律（三条红线）在 Prompt 层就写死：
1. 无来源不出引用——检索不到就不写案号和条文；
2. 法条必须标注效力状态；
3. 输出强制带免责声明。
"""

from __future__ import annotations

import re
from typing import Any

from backend.app.config import settings
from backend.llm.evidence_sanitize import sanitize_display_title, sanitize_evidence_text

# --------------------------------------------------------------------------- 系统提示
LEGAL_SYSTEM_PROMPT = """你是律所内部的「法律辅助工作台」中的专业助手，服务对象是执业律师。

你必须严格遵守以下纪律，任何情况下都不得违反：

【引用纪律】
1. 只能引用「参考资料」中出现过的法条与判例。参考资料里没有的，一律不得写出条文号、案号、法院名称。
2. 不得编造、不得推测、不得"补全"任何案号。判例案号是法律 AI 最大的事故来源。
3. 每一条法条引用必须标注效力状态（现行有效 / 已修订 / 已废止 / 尚未生效）。
4. 引用法条时使用规范格式：《法规名称》第X条。
5. 若参考资料不足以支撑某个结论，必须明确写出「证据缺口」，而不是编一个依据。

【定性边界】
- 你的输出是"律师工作参考"，不是正式法律意见；不得出具鉴定类结论。
- 不得虚构当事人、不得虚构事实；事实只能来自用户提供的材料。

【表达要求】
- 用法言法语，结构清晰，条目化输出。
- 结论后面标注支撑它的引用编号，格式如 [S1] [C2]。
"""


def system_with_persona(
    persona: str,
    base: str = LEGAL_SYSTEM_PROMPT,
    *,
    answer_template: str = "",
) -> str:
    """专家人设注入：人设作为 system 前缀，基础纪律提示词完整保留。

    拼接顺序是**人设 → 回答模板 → 基础提示词**，而不是反过来 —— 人设只负责
    "扮演谁"（如"我是专攻劳动仲裁的王建国律师"），引用纪律、定性边界、表达要求
    一条都不能被盖掉。

    ``answer_template`` 是专家的**专属回答模板**（可选）：一段回答结构指令，
    插在人设与基础提示词之间，约束生成节点按专家办案思路组织答案。
    人设与模板都为空时原样返回基础提示词，行为逐字不变。
    """
    p = (persona or "").strip()
    tpl = (answer_template or "").strip()
    if not p and not tpl:
        return base
    parts: list[str] = []
    if p:
        parts.append(p)
    if tpl:
        seg = (
            "【回答结构模板（必须遵守，用于组织本次回答的骨架）】\n"
            f"{tpl}"
        )
        sections = _template_sections(tpl)
        if sections:
            seg += (
                "\n\n硬性要求：本次回答必须按以下小节标题逐节组织、逐节输出，"
                "任何小节都不得遗漏：\n" + " / ".join(sections)
            )
        parts.append(seg)
    return "\n\n".join(parts + [base])


_SECTION_RE = re.compile(r"「([^」]+)」")


def _template_sections(tpl: str) -> list[str]:
    """从专家模板中提取「」包裹的小节名，含 →/、/,/ 连接符的拆成多段。

    例如「争议焦点 → 攻防要点 → 诉讼时效 → 证据缺口」拆为
    ["争议焦点", "攻防要点", "诉讼时效", "证据缺口"]，用于生成硬性小节约束。
    """
    sections: list[str] = []
    for m in _SECTION_RE.findall(tpl or ""):
        for part in re.split(r"[→、,，/]", m):
            part = part.strip()
            if part:
                sections.append(part)
    return sections


# 意图 → 适用的市场技能分类白名单。
# 法律工作台只路由法律相关的技能分类；内容创作/办公效率/开发辅助/数据采集等
# 与当前意图无关的技能一律不注入，避免多技能答案拼接缝合。
INTENT_SKILL_CATEGORIES: dict[str, tuple[str, ...]] = {
    "consult": ("咨询", "检索"),
    "contract_review": ("合同", "检索"),
    "case_search": ("检索", "案件"),
    "statute_lookup": ("检索",),
    "doc_draft": ("文书",),
    "case_analysis": ("案件", "证据", "咨询", "检索"),
    "quick_answer": ("咨询", "检索"),
    "off_topic": (),  # 非法律问题不注入任何技能
}


def _skill_hits_query(skill: dict[str, Any], query: str) -> bool:
    """query 是否命中技能卡片（名称 / 标签 / 描述任一子串）。

    用于二次收敛：同意图分类下技能较多时，只注入与问题文本直接相关的技能，
    进一步避免无关技能混入回答。
    """
    q = str(query or "").strip()
    if not q:
        return False
    haystacks = [
        str(skill.get("name") or "").strip(),
        str(skill.get("description") or "").strip(),
    ]
    haystacks.extend(str(t) for t in (skill.get("tags") or []) if t)
    return any(q in h for h in haystacks if h)


def market_skill_context(user_id: str = "", intent: str = "", query: str = "") -> str:
    """生成已启用市场技能的提示块（方案 B 的回答链路注入点，意图路由裁剪版）。

    与 :func:`backend.skills.skill_context` 的分工：
    原生技能按意图（intent）固定注入「本所作业路径」；本函数按意图（intent）
    从「技能市场」中**用户已启用**的技能里选出与本次任务相关的子集注入，
    回答只按这些技能的能力范围组织内容，不再全量注入造成答案缝合。

    筛选规则：
    1. 意图 → 分类白名单（:data:`INTENT_SKILL_CATEGORIES`）先裁掉无关分类；
    2. 携带 ``query`` 时再按名称/标签/描述做二次收敛，命中才保留；
    3. 意图不在白名单 / 未启用任何相关技能 / 未携带用户身份时返回空串，
       行为与改造前逐字一致。
    """
    uid = str(user_id or "").strip()
    if not uid:
        return ""
    try:
        from backend.app.services.skill_registry import skill_registry
        items = (skill_registry.mine(uid) or {}).get("items") or []
    except Exception:  # noqa: BLE001 - 注册表异常不应阻断回答链路
        return ""

    enabled = [s for s in items if s.get("enabled")]
    if not enabled:
        return ""

    categories = INTENT_SKILL_CATEGORIES.get((intent or "").strip())
    if categories is None:
        # 意图未知：不注入，避免无关技能混入
        return ""
    if not categories:
        # 白名单为空（如 off_topic）：明确不注入任何技能
        return ""

    pool = [s for s in enabled if str(s.get("category") or "").strip() in categories]
    if query:
        matched = [s for s in pool if _skill_hits_query(s, query)]
        if matched:
            pool = matched
    if not pool:
        return ""

    lines = ["【本次回答适用的技能】以下技能与本次任务相关且已由当前用户启用，回答请按其能力范围组织内容："]
    for i, s in enumerate(pool, 1):
        name = str(s.get("name") or "").strip()
        category = str(s.get("category") or "").strip()
        desc = str(s.get("description") or "").strip()
        seg = f"{i}. {name}"
        if category:
            seg += f"（{category}）"
        if desc:
            seg += f"：{desc}"
        lines.append(seg)
    lines.append("技能仅作为作业能力参考；法条、类案等结论仍须来自检索证据，不得凭空引用。")
    return "\n".join(lines)

INTENT_PROMPT = """判定用户请求的意图与文档类型。

可选意图：consult（法律咨询/概念问答）、contract_review（合同审查）、case_search（类案检索）、
statute_lookup（法规检索）、doc_draft（文书起草）、case_analysis（案件分析）。

可选文档类型：contract（合同）、litigation（诉讼材料/裁判文书）、consult（咨询）、
evidence（证据材料）、opinion（法律意见书）、unknown。

可选法律领域（domain）：
labor（劳动/社保，如劳动合同、工伤、加班、裁员、社保）、
tax（税务/财税，如个税、增值税、企业所得税、发票、稽查、环保税）、
env（生态环境/资源，如环保、排污、环评、污染、原子能、国家公园）、
civil（民事/商事，如合同纠纷、借贷、婚姻继承、侵权）、
criminal（刑事，如犯罪、刑罚、侦查、公诉、判刑）、
admin（行政，如治安管理、行政处罚、行政许可、行政复议、信访）、
constitution（宪法，如宪法宣誓、违宪审查、公民基本权利）、
ip（知识产权，如专利、商标、著作权、版权、商业秘密）、
transport（交通运输，如航空、海运、船舶、道路运输、交通事故）、
military（军事，如国防动员、兵役、军人权益）、
agriculture（农业，如渔业、种植养殖、土地承包、农产品）、
trade（国际贸易，如对外贸易、进出口、海关、外商投资、关税）、
safety（安全生产，如食品安全、危化品、生产事故、消防）、
culture（文化教育，如教育、教师、语言文字、文化文物、出版）、
ethnic（民族事务，如民族团结、民族区域自治）、
social_security（社会保障，如社会救助、低保、慈善、养老、妇女儿童）、
legislation（立法程序，如立法规划、法律修改、法律清理）、
judicature（司法制度，如仲裁、律师、法院、公证、调解）、
economy（经济法，如公司证券、竞争垄断、网络安全、消费、招标投标）、
unknown（无法判定）。

只输出 JSON，不要任何解释：
{"intent": "...", "doc_type": "...", "domain": "labor/tax/env/civil/criminal/admin/constitution/ip/transport/military/agriculture/trade/safety/culture/ethnic/social_security/legislation/judicature/economy/unknown",
 "confidence": 0.0, "reason": "一句话理由"}
"""

EXTRACT_PROMPT = """从下面的法律文书/合同文本中抽取结构化要素。

只输出 JSON：
{
  "parties": [{"name": "主体名称", "role": "原告/被告/甲方/乙方/用人单位/劳动者", "kind": "自然人/法人/非法人组织"}],
  "facts": [{"text": "事实描述", "source": "来源位置", "page": null}],
  "clauses": [{"clause_no": "第X条", "clause_title": "条款标题", "clause_text": "条款原文"}],
  "doc_type": "contract/litigation/consult/evidence/opinion/unknown"
}

要求：
- 事实只来自原文，不得推断补充；
- 条款按原文顺序逐条拆分，clause_text 保留原文表述（可适度截断到 200 字内）；
- 抽不到就返回空数组，不要编造。
"""

ISSUE_PROMPT = """基于案件事实与检索到的法规、类案，归纳争议焦点。

只输出 JSON：
{"issues": [{"title": "焦点标题", "description": "简要说明", "severity": "high/medium/low",
             "basis": ["《法规名称》第X条"]}]}

要求：争议焦点必须能从给定事实中推出；basis 只能引用参考资料中真实出现过的条文。
"""

CONTRACT_REVIEW_PROMPT = """你正在执行律所的「合同审查 Skill」。请按下面的固定路径逐条审查，不要跳步。

固定路径：
1. 合同结构解析（主体/标的/价款/履行/违约/争议解决）
2. 主体资格审查要素提取
3. 检索适用法律（校验时效）
4. 检索本所该类合同审查清单
5. 逐条风险扫描：违约金、解除权、管辖、知识产权、保密、付款与交付
6. 检索类案中同类条款的裁判倾向
7. 生成风险点清单（条款定位 + 风险等级 + 修改建议）

只输出 JSON：
{
  "contract_type": "合同类型",
  "risk_points": [
    {"clause_no": "第8条", "clause_title": "违约责任", "risk": "风险描述",
     "level": "high/medium/low", "suggestion": "具体修改建议", "basis": ["《民法典》第585条"]}
  ],
  "missing_clauses": [{"name": "缺失条款名", "reason": "为什么建议补", "level": "medium"}],
  "confidence": 0.0
}

硬性要求：
- basis 中的每一条都必须能在「参考资料」里找到，否则不要写；
- 条款定位必须写清是第几条，便于律师在原文中逐条跳转；
- level 取 high（必须改）/ medium（建议改）/ low（提示性）；
- **不要输出条款原文**：后端已存有每条条款的原文，条款号足以定位；
  在结果里抄录原文只是浪费输出，一律不要抄；
- **不要输出修改对照稿（revision_draft）**：对照稿由后端工具根据你的
  风险点清单生成，你写了也没人读 —— 把 token 花在风险判断本身。
"""

CONTRACT_REVIEW_SECOND_PASS_PROMPT = """你正在对一份合同审查的**初审结果**做复审。

初审模型「广撒网」、宁可多报；你的职责是**严核查**，把清单收敛到可信。三条职责：
1. 逐条核查初审风险点：confirmed（维持）/ downgraded（等级虚高，降一级）/ removed（误报，删掉）；
2. 补上初审**漏报**的风险（它没点到、但合同里确有）；
3. 每条结论都给一句可执行的 reason（为什么降 / 为什么删 / 为什么补）。

硬性要求：
- 只依据「合同条款」与「参考资料」判断，不得凭空编条文号；
- 指代初审风险点时**必须用它的编号 #N**，不要复述整条原文；
- 等级只取 high / medium / low；
- 立场与初审一致（站在同一委托方角度），不要擅自换立场；
- 不要输出修改对照稿；
- additions 里的 basis 每一条都必须能在「参考资料」里找到，否则留空。

{feedback}

只输出 JSON：
{
  "decisions": [{"no": 1, "action": "confirmed|downgraded|removed", "to_level": "medium", "reason": "为什么"}],
  "additions": [{"clause_no": "第X条", "clause_title": "", "risk": "", "level": "medium", "suggestion": "", "basis": ["《…》第X条"]}]
}
"""

CASE_ANALYSIS_PROMPT = """你正在执行「案件分析」。请归纳：争议焦点、攻防要点、诉讼时效核查、策略建议。

只输出 JSON：
{
  "issues": [{"title": "...", "description": "...", "severity": "high/medium/low", "basis": ["《法规名称》第X条"]}],
  "strategy": ["我方攻点：...", "对方可能的抗辩：...", "应对：..."],
  "limitation_check": {"period": "诉讼时效/仲裁时效期间", "start_point": "起算点",
                        "conclusion": "是否已过时效的初步判断", "basis": ["《民法典》第188条"]},
  "evidence_gap_notes": ["需要补充的证据"],
  "confidence": 0.0
}

硬性要求：
- 类案不可用时要明确标注证据缺口，不要假装有类案支撑；
- 时效结论只能基于检索到的条文，无法判断时写"需要核实起算点"。
"""

DRAFT_PROMPT = """生成《{doc_type}》草稿。

要求：
1. 结构符合法定文书格式（首部 / 事实与理由 / 法律依据 / 请求事项 / 尾部）；
2. 事实部分只使用用户提供的事实，不得虚构；
3. 法律依据部分只能引用「参考资料」中真实存在的条文，格式《法规名称》第X条；
4. 引用不到依据时，写「（此处需补充法律依据）」而不要编造；
5. 输出 Markdown，不要输出 JSON。
"""

REPORT_PROMPT = """把上面的分析汇总为最终输出。**输出 Markdown**，按下述结构组织
（小节按实际内容取舍，没有的内容整节约掉，不要硬凑）：

## 核心结论
3-5 条，每条一句话直接给答案（要多少钱、能不能主张、风险在哪）。

## 事件梳理
按时间顺序列出关键事实（只有用户提供了事实才写），一行一条：`时间 — 事件`。
**只准用用户提供的事实，一个字都不许编。**

## 争议焦点
编号列出（1. 2. 3.），每个焦点一句话点明"双方分歧在哪、法律上怎么定"。

## 金额测算
凡涉及钱（赔偿金/补偿金/加班费/违约金…），**必须用表格**逐项算清：

| 项目 | 计算方式 | 金额 | 法律依据 |
|---|---|---|---|

- **「参考资料」里出现「法定金额测算」块时，本表只能照抄它**：计算方式照抄块里的算式、
  金额照抄块里的数值，**逐位一致**，一位都不能改。那个块由代码算，是本次交付物里
  唯一有权出数字的地方；块里**没有**的项目不许写金额，也不要自己估一个（宁可不写）。
  照抄来的数就是定稿，**不要再写"以某处为准""自我校对/更正"这类旁白** ——
  2026-10-07 实测踩过：核心结论里自己按 9 个月折算出 153,000，正文里又写 136,000，
  最后还留一句"上文误写为 153,000"，律师只看结论那行就会拿错数去谈判。
- 参考资料里**没有**这个块时（没有确切事实可算），才按下面的口径自己列算式；
- 计算方式要写出数字推导（如 `8500 × 3 个月`）；
- 表格最后一行给「合计」；
- 用户给了工资/年限等数字就必须代入计算，给出具体金额，不要只说"视情况而定"；
- 依据只能引自「参考资料」真实存在的条文，格式《法规名称》第X条。
- **「赔偿/费用项目清单」型问题**（如"工伤赔偿有哪些项目、各是多少"）用同一张表逐项枚举，
  一行一个项目；项目清单按参考资料逐项列，参考资料没提的项目不要自行补全；
- **数字出处纪律**：只准写「参考资料」原文或「法定金额测算」块里的数字；参考资料没有给出
  具体数额的项目写"参考资料未包含具体标准"，**禁止**写"一般为 X""视情况而定"这类无出处数字；
  带地区/年份口径的标准（如"北京市 2023"）必须原样标注地区与年份，并注明以当地最新规定为准。

## 行动建议
分步骤、可执行（先做什么、再做什么）：协商要点 → 投诉/调解渠道 → 仲裁
（写清管辖机构、时效、需要准备的材料）。
涉及**程序/流程**类问题（工伤认定、劳动能力鉴定、仲裁、复议、诉讼等）时按编号步骤输出，
**每一步都标注该步的时限与所需材料**（如工伤认定：单位 30 日内、个人 1 年内申请；
劳动能力鉴定：伤情相对稳定后申请），不要只给流程不给时限。

## 证据与期限
- 需要收集/固定的证据（逐项，说明它证明什么）；
- 时效/期限结论（多长时间、从哪天起算、是否紧迫）。

## 引用清单
逐条列出：法规名+条文号（+效力状态），或案号+法院+裁判日期。

---
必须原样附上免责声明：{disclaimer}

硬性要求：
1. 不得出现引用清单以外的案号或条文号；
2. 引用标注在正文里：关键论断后面用（依据：《XX法》第X条）形式带出；
3. 不确定的事项如实说"需核实"，不要用套话掩盖。
"""

# --------------------------------------------------------------------------- 直答
# 为什么要把"直答"单开一份 prompt，而不是复用 REPORT_PROMPT：
# 报告模板（结论摘要 / 详细分析 / 引用清单）是为"有证据链"设计的。
# 拿它去答"你好"或"试用期最长几个月"，模型要么把一句话撑成三段套话，
# 要么为了填满「引用清单」去写参考资料里没有的条文号 —— 后者正是最危险的那类幻觉。
DIRECT_ANSWER_PROMPT = """直接回答用户，不要套用法律报告结构。

要求：
1. 一到三段话直接作答，同事之间说话的语气，不要写小标题、不要写「结论摘要」；
2. 本次请求**与法律无关**（寒暄、闲聊、技术或写作类请求）：不要套法律分析框架，
   不要提法条，不要加"从法律角度"这类前缀；
3. 只有当它确实牵涉权利义务时，才补一句"如涉及具体法律后果，可另按法律问题提问"，
   不要展开。
"""

QUICK_ANSWER_PROMPT = """用**最短篇幅**回答这个法律问题。

要求：
1. 第一句就给结论（期限/数额/能否）；给不出确定结论时明确说"需结合具体情形判断"，
   不要用模糊表述蒙过去；
2. 再用 1-3 句说明依据或前提；
3. **只能引用「参考资料」中出现过的条文** —— 参考资料里没有的条文号一律不写，
   宁可写"（此处需补充法律依据）"，也不要凭记忆写条文号；
4. 不要展开成完整法律分析报告，不要出现「争议焦点」「攻防要点」「诉讼策略」这类章节；
5. 必须原样附上免责声明：{disclaimer}
"""


# 纯 RAG 问答（AI 助手页专用）。走的是"检索 → 生成"两段，不经过任何 Agent 节点，
# 所以这里要**主动禁掉**报告模板里的那几节 —— 不写清楚，模型会顺着
# LEGAL_SYSTEM_PROMPT 的"结构清晰、条目化输出"自己长出「引用清单」，
# 而那正是律师在界面上明确不要的东西（他要的是对话，不是一份报告）。
RAG_ANSWER_PROMPT = """回答律师提出的法律问题。**这是一次对话，不是出一份报告。**

## 先接住提问者的具体情况（最重要的一步）

这是真人咨询，不是普法课堂。**回答必须从提问者的处境出发，而不是从法条出发。**

1. 开头第一段先回应用户的具体处境与情绪（如"您担心被认定为偷税""您已收到税务机关的处理通知"），
   说明下面的回答将针对他的情况展开；
2. 若能从问题推断出几种常见情形，必须**分情形覆盖**，用「如果是这种情况 → 应该这样做」
   让用户对号入座。以偷税漏税为例：已被立案/收到处理决定、主动自查想补缴、被举报担心被查、
   替亲友打听——每种情形的紧迫程度和首要动作不同，要分别给出，不要混在一起；
3. 若关键信息不足，给出初步方向后**明确列出需要用户补充的关键信息并直接追问**
   （如：是否已收到《税务处理决定书》/《处罚决定书》？是个人还是公司？金额多大？
   是否已主动补缴？），不要默默按通用情形长篇输出；
4. 正文以"用户应该怎么办"为骨架，法条只作支撑；禁止把法条原文、条文目录、检索摘录堆进正文。

## 输出结构

必须使用下面五个小标题，**一字不差**（不要改成"一、""### 一、"或自拟标题），
**禁止自拟或新增小节**（不要出现「核心定性」「法律依据与处罚标准」「最高案例启示」
「后续应对策略」「操作建议」这类标题），小节顺序不得调换、不得留空节：

```
（一）核心结论
（二）具体分析
（三）通俗化输出
（四）额外思考
（五）行动建议
```

- **（一）（二）（五）永远要有。** 结论直接回答"能/不能、几个月、多少钱、哪一条"，
  第一句就是结论；给不出确定结论时明说"需结合具体情形判断"并点出取决于哪个事实。
- **（五）行动建议**：在（一）～（四）之后给出**用户可执行的救济路径**，
  分步骤、写清渠道、时效与需要准备的材料；纯知识性问答（如"试用期最长几个月"）
  确无救济路径可给时，用一句话说明常规维权渠道即可，不要硬凑步骤。
- **（三）（四）确无内容时整节省略**，不要为了凑结构写套话。
- （二）涉及条件、期限、金额、比例这类**对照关系**时，用 Markdown 表格给出
  （表头 + `|---|` 分隔行），不要排成一段文字。

## 行动建议按领域给路径（先判定问题所属领域，再选对应救济路径）

先根据问题内容判定所属领域，再给出该领域下可执行的步骤（**协商 → 投诉/调解 → 仲裁/诉讼**
的基本顺序，具体渠道按领域展开）。领域较多，按大类聚合给路径：
- **劳动（labor）**：协商要点 → 向劳动监察部门投诉举报 → 申请劳动仲裁
  （写清管辖机构、时效，以及工资条/劳动合同/解除通知书等需准备的材料）；
- **税务/财税（tax）**：陈述申辩/行政复议 → 税务复议或向税务机关举报 → 必要时行政诉讼；
- **环境/资源（env）**：向生态环境主管部门投诉举报（写清举报渠道）→ 必要时申请信息公开或诉讼；
- **民事（civil）**：协商 → 人民调解/行业调解 → 民事诉讼（写清管辖法院、诉讼时效、证据材料）；
- **刑事（criminal）**：第一时间报警/报案（写清证据保存要点）→ 配合侦查 → 委托辩护人
  （涉及被害人的，提示刑事附带民事诉讼/谅解协商等救济渠道）；
- **行政（admin）**：先核对执法主体与依据 → 申请听证/陈述申辩 → 行政复议 → 行政诉讼
  （写清复议机关、起诉期限）；
- **宪法（constitution）**：涉及基本权利受侵害时，提示通过具体法律渠道维权
  （宪法权利一般经由民事/行政/刑事具体立法落实，指引到对应领域路径）；
- **知识产权（ip）**：固定侵权证据 → 发律师函/协商 → 向市场监管（知识产权）部门投诉举报
  或申请行政裁决 → 民事诉讼（可申请诉前禁令、证据保全）；
- **交通运输（transport）**：交通事故类先报警/保险报案 → 协商/调解 → 诉讼；
  运输合同纠纷按一般民事合同路径处理；
- **军事（military）**：军事法律问题一般走军事机关内部申诉/军队法律援助
  （可联系部队政治工作部门或军队法律顾问处），涉及地方权益的再走地方渠道；
- **农业（agriculture）**：土地承包/经营纠纷先向乡镇政府或农村土地承包仲裁机构申请调解/仲裁
  → 民事诉讼；渔业/农资问题向农业农村主管部门投诉举报；
- **国际贸易（trade）**：外贸合同纠纷按合同约定仲裁/诉讼 → 涉及海关/检验检疫问题
  向海关或商务主管部门申诉 → 必要时提起行政诉讼；
- **安全生产（safety）**：生产安全事故向应急管理部门/市场监管部门举报 → 申请工伤认定或
  赔偿协商 → 仲裁/诉讼；食品安全问题向市场监管部门投诉举报（12315）；
- **文化教育（culture）**：教育/教师权益问题向教育主管部门申诉或信访 → 行政诉讼；
  文化出版问题向新闻出版主管部门投诉；
- **民族事务（ethnic）**：民族权益受侵害的，向民族事务主管部门或民政部门反映 →
  视情形走行政/民事救济路径；
- **社会保障（social_security）**：社会救助/低保/养老问题向民政部门或街道（乡镇）申请复核
  → 对不予救助决定可申请行政复议或行政诉讼；
- **立法程序（legislation）**：立法程序类问题一般属立法机关内部程序，不直接产生
  个人民事/行政救济路径，提示向人大法制工作机构反映建议或依法定程序提出审查建议；
- **司法制度（judicature）**：仲裁/诉讼程序问题向受理机构（仲裁委/法院）申请异议或
  复议 → 对仲裁裁决依法申请撤销或不予执行 → 执行程序问题向法院申请执行异议；
- **经济法（economy）**：消费纠纷（消费者权益）先与经营者协商 → 12315 投诉举报 →
  诉讼；公司/证券/反垄断等问题向市场监管部门或证券监管部门投诉举报 → 诉讼；
- **无法判定领域**：给出通用救济路径（先协商、再投诉/调解，视情况仲裁或诉讼）。

行动建议必须**具体可执行**（写机构名、时效、材料清单），不得写成"建议依法维权"
这类空话。

## 纪律（违反即事故）

1. **只能在正文里写「参考资料」中出现过的条文。** 参考资料里没有的条文号一律不写 ——
   不写比写错安全。检索结果确实覆盖不到时，就在（二）里顺带说明一句"参考资料未包含 X 的规定"，
   不要用"根据相关法律规定"糊过去，**也不要把这件事单列成一节**。
2. 引用时必须带行内标记，格式 `[S1]` `[C2]`（对应参考资料里每条的编号），
   让界面能把标记换成可点击的法条标签。**没有标记的引用等于没有出处。**
3. 不得编造、不得推测、不得"补全"任何案号与条文号。
4. **数字出处纪律**：金额/标准只准写「参考资料」原文给出的数字；参考资料没有具体数额的，
   写"参考资料未包含具体标准"，**禁止**写"一般为 X""视情况而定"这类无出处数字；
   带地区/年份口径的标准必须原样标注地区与年份（如"北京市 2023"口径），并注明以当地最新规定为准。

## ⛔ 以下章节一律不许出现

「引用清单」「证据缺口与不确定性」「综合置信度」「证据缺口提示」「参考法条列表」
—— 以及结尾罗列法条。这些由界面统一呈现（末尾的「参考法规」卡片区），
写进正文只会重复、且更难读。**不要用别的名字复活它们。**

必须原样附上免责声明：{disclaimer}
"""


# AI 助手 Agent 链路专用的交付物提示词 = 上面那份**逐字沿用** + 本链路的标记口径。
#
# 为什么用替换而不是复制一份出来改：四节结构、Markdown 表格要求、纪律 1-3
# 是这个产品最容易出事、也最容易在两边改歪的几段。抄一份的后果不是"两份都要改"，
# 而是**只有一份被改**：助手页的措辞改了，`/chat/rag` 那条链路（老端点还在用，
# 见 ``backend/app/api/v1/rag_answer.py``）悄悄留在旧口径上，两边对同一条法条
# 给出不同格式的引用，而没有任何一处会报错。
#
# 与上面那份的**全部差异只有两处**，都在下面用锚点写死、import 时校验：
#   ① ⛔ 名单末尾那句出处说明 —— 本链路的这几节由**代码**在正文之后附上；
#   ② 结尾那句"必须原样附上免责声明" —— 免责声明同样由代码附在交付物最后，
#      模型再写一遍会夹在正文与报告尾巴之间（见 ``report_node._assemble_conversational``）。
_DISCLAIMER_LINE = "必须原样附上免责声明：{disclaimer}"
_SECTION_SOURCE_OLD = "这些由界面统一呈现（末尾的「参考法规」卡片区），"
_SECTION_SOURCE_NEW = "这些由代码在正文之后统一附上，"

if _SECTION_SOURCE_OLD not in RAG_ANSWER_PROMPT or _DISCLAIMER_LINE not in RAG_ANSWER_PROMPT:
    # 锚点找不到 ⇒ 上面那份被改过。**宁可在 import 时炸掉，也不要静默地少替换一段** ——
    # 少替换的表现是：助手页正文里长出一份与代码那份打架的「引用清单」，
    # 而提示词、正文、日志三处都显示正常。
    raise RuntimeError(
        "ASSISTANT_REPORT_PROMPT 的两个锚点在 RAG_ANSWER_PROMPT 里找不到了："
        "请同步更新 prompts.py 里这两个常量，别让两份口径悄悄分叉"
    )

# 本链路的追加口径。**只补这份提示词看不到、且模型自己推不出来的四件事**：
# 标记的编号从哪来、类案为什么不能带标记、金额为什么必须照抄、哪些不该它写。
_ASSISTANT_AGENT_RULES = """

## 本链路的追加口径（与上面的纪律同等效力）

1. **编号照抄「参考资料」里给的编号**：法条是 `S1` `S2`…、本所知识库指引是 `K1` `K2`…，
   一律按参考资料里的原编号写，**不许自己重排、也不许跳号**。
2. **行内标记只允许 `[Sn]` 与 `[Kn]` 两种。** 界面上有卡片的只有法条与本所知识库，
   其余编号写进正文会被**原样显示成裸文本**给律师看：
   - 类案编号（`C1` `C2`…）不写行内标记 —— 要引类案就直接写案号与法院，
     例如"（2020）京01民终1号（北京市第一中级人民法院）"；
   - 「法定金额测算」块的编号（`[T1]`）**同样不写进正文**：金额照抄（见第 3 条），
     但不要把 `[T1]` 这个标记带进去。实测踩过：正文里每句金额后面都挂了个 `[T1]`，
     而界面上根本没有这个卡片。
3. **金额一律照抄「法定金额测算」块，逐位一致。** 该块由代码计算，
   是本次交付物里**唯一有权出数字**的地方：
   - 表格的「计算方式」列照抄块里的算式，「金额」列照抄块里的数值，一位都不能改；
   - 块里**没有**的项目不许写金额，也不要自己估一个 —— 宁可不写；
   - 块里写了"未测算"或列出缺口时，如实写该项金额未测算并说明缺什么事实。
   **同一份材料里出现两个版本的金额是最严重的事故之一**（正文一个、表格一个），
   而两边都"看起来正常"，所以这一条不接受任何变通。
4. 免责声明、引用清单、证据缺口与置信度由代码附在交付物最后，**正文里不要再写**。
"""

ASSISTANT_REPORT_PROMPT = (
    RAG_ANSWER_PROMPT
    .replace(_SECTION_SOURCE_OLD, _SECTION_SOURCE_NEW)
    .replace(_DISCLAIMER_LINE + "\n", "")
    + _ASSISTANT_AGENT_RULES
)


# --------------------------------------------------------------------------- 组装
def build_messages(
    system: str,
    user: str,
    context_blocks: list[tuple[str, str]] | None = None,
) -> list[dict[str, str]]:
    """把检索证据拼成参考资料块塞进 Prompt。"""
    msgs: list[dict[str, str]] = [{"role": "system", "content": system}]
    if context_blocks:
        ctx_lines = ["【参考资料】（只能引用以下内容，不得超出）"]
        for tag, text in context_blocks:
            ctx_lines.append(f"\n[{tag}] {text}")
        msgs.append({"role": "user", "content": "\n".join(ctx_lines)})
    msgs.append({"role": "user", "content": user})
    return msgs


# 追问上下文的硬上限。**必须有上限**：多轮对话不设限，Prompt 会随轮次线性膨胀，
# 最后要么超上下文、要么把参考资料挤到模型注意力之外（表现为"越聊越不引法条"）。
MAX_HISTORY_TURNS = 2
MAX_HISTORY_ANSWER_CHARS = 400


def format_history(history: list[dict[str, str]] | None) -> str:
    """把最近几轮对话拼成历史块。没有历史时返回空串。"""
    lines: list[str] = []
    for i, turn in enumerate((history or [])[-MAX_HISTORY_TURNS:], start=1):
        if not isinstance(turn, dict):
            continue
        q = str(turn.get("q") or "").strip()[:300]
        if not q:
            continue
        a = str(turn.get("a") or "").strip()[:MAX_HISTORY_ANSWER_CHARS]
        lines.append(f"[H{i}] 问：{q}")
        if a:
            lines.append(f"     答：{a}")
    if not lines:
        return ""
    return (
        "【历史对话】（**不是证据**：只用来理解本轮问题里的指代，"
        "如「那」「它」「这种情况」指的是什么；**绝对不得据它引用任何条文号、案号**）\n"
        + "\n".join(lines)
    )


def build_rag_messages(
    system: str,
    user: str,
    context_blocks: list[tuple[str, str]] | None = None,
    history: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    """纯 RAG 问答的消息序列：system / 参考资料 / 历史对话 / 本轮问题。

    复用 :func:`build_messages` 而不是重写一遍 —— 参考资料的拼法与
    「只能引用以下内容」这句约束必须两条链路完全一致，抄一份迟早会分叉。
    """
    msgs = build_messages(system, user, context_blocks)
    block = format_history(history)
    if block:
        # 插在最后一条（本轮问题）之前，让模型按"参考资料 → 聊到哪儿了 → 现在问什么"读
        msgs.insert(-1, {"role": "user", "content": block})
    return msgs


def format_statute_evidence(idx: int, doc_name: str, article_no: str, status: str, text: str, publisher: str = "") -> tuple[str, str]:
    """法条证据块：强制携带「法规名 + 条文号 + 效力状态」。

    进入模型视野前先清洗：``doc_name`` 可能被入库成 ``file://`` 路径，
    ``text`` 可能携带解析器占位符（``[TABLE_002: 语义]``）—— 不剥离的话
    模型会照抄进回答，前端再把 ``file://``/``[TABLE_xxx]`` 当附图渲染。
    """
    doc_name = sanitize_display_title(doc_name)
    text = sanitize_evidence_text(text)
    tag = f"S{idx}"
    body = f"{doc_name}{article_no}｜效力状态：{status}"
    if publisher:
        body += f"｜发布机关：{publisher}"
    body += f"\n条文：{text}"
    return tag, body


def format_case_evidence(idx: int, case_no: str, court: str, judge_date: str, cause: str, gist: str) -> tuple[str, str]:
    """判例证据块：强制携带「案号 + 法院 + 裁判日期」。

    ``gist`` 同样做占位符/路径清洗，防止裁判要旨里的技术元素漏进回答。
    """
    gist = sanitize_evidence_text(gist)
    tag = f"C{idx}"
    body = f"案号：{case_no}｜法院：{court}｜裁判日期：{judge_date}｜案由：{cause}\n裁判要旨：{gist}"
    return tag, body


def format_knowledge_evidence(idx: int, title: str, doc_type: str, version: str, snippet: str) -> tuple[str, str]:
    """知识库证据块：强制携带指引版本。

    ``title`` 清洗路径（本地 html 常把 ``file://`` 路径当标题入库），
    ``snippet`` 清洗占位符与路径 —— 这正是本次真机 bug 的入口。
    """
    title = sanitize_display_title(title)
    snippet = sanitize_evidence_text(snippet)
    tag = f"K{idx}"
    return tag, f"{title}（{doc_type}，版本：{version}）\n{snippet}"


# 证据块数量上限：三类证据各截断，避免 Prompt 被长文本挤爆
MAX_STATUTE_BLOCKS = 12
MAX_CASE_BLOCKS = 8
MAX_KNOWLEDGE_BLOCKS = 6


def evidence_blocks(
    state: dict,
    *,
    max_statutes: int = MAX_STATUTE_BLOCKS,
    max_cases: int = MAX_CASE_BLOCKS,
    max_knowledges: int = MAX_KNOWLEDGE_BLOCKS,
) -> list[tuple[str, str]]:
    """把 State 里的检索证据拼成 ``[(tag, body)]``，交给 :func:`build_messages`。

    为什么统一在这里拼：法条要带效力状态、判例要带案号/法院/裁判日期、
    指引要带版本 —— 这三条「强制携带出处」的要求是方案 8.3 的硬约束。
    如果每个节点各拼一份，早晚会有某个节点漏带状态字段，
    而模型看不到状态就会写出「依据《XX法》第X条」却不标注是否已废止。

    编号规则：法条 S1..Sn、判例 C1..Cn、指引 K1..Kn。
    规则引擎的 ``_parse_context`` 就是按这个 ``^\\[([SCK]\\d+)\\]`` 解析的，
    两边格式必须一致。
    """
    blocks: list[tuple[str, str]] = []

    for i, s in enumerate((state.get("statutes") or [])[:max_statutes], start=1):
        blocks.append(format_statute_evidence(
            i,
            s.get("doc_name") or "",
            s.get("article_no") or "",
            s.get("status") or "未知",
            (s.get("text") or "")[:900],
            s.get("publisher") or "",
        ))

    for i, c in enumerate((state.get("cases") or [])[:max_cases], start=1):
        blocks.append(format_case_evidence(
            i,
            c.get("case_no") or "（无案号）",
            c.get("court") or "",
            str(c.get("judge_date") or ""),
            c.get("cause_of_action") or "",
            (c.get("gist") or c.get("result") or c.get("full_text") or "")[:700],
        ))

    for i, k in enumerate((state.get("knowledges") or [])[:max_knowledges], start=1):
        blocks.append(format_knowledge_evidence(
            i,
            k.get("title") or "",
            k.get("doc_type") or "",
            k.get("version") or "未标注",
            (k.get("snippet") or "")[:600],
        ))

    return blocks


def disclaimer_note() -> str:
    return settings.DISCLAIMER
