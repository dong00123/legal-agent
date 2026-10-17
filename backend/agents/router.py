"""Router / 意图 Agent（方案 4.1）。

职责：判定 **法律咨询 / 合同审查 / 类案检索 / 文书起草 / 法规查询 / 案件分析**，
并给出 ``doc_type``。简单任务直接单 Agent 完成（方案 4.1 的关键约束）。

判定策略是**规则优先、LLM 兜底**，而不是反过来：

- 意图判定是**整个流程的分叉点**。误判一次，后面所有检索式、节点序列、审查路径
  全错，而且错得很像"结果是合理的"，律师很难察觉。
- 规则判定是确定性的、可回归测试的、不依赖外部服务的。本机 Ollama 常常没有模型、
  内网 LLM 也可能抖动，如果 LLM 优先，演示环境和生产环境的行为会不一致。
- 规则给出的分数同时是**可解释的**：日志里能看到"命中：合同/条款/违约金"，
  便于运营同学直接调整词表。

LLM 只在规则分数落在模糊区间（既不像 A 也不像 B）时才介入做区分。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from backend.agents.harness.router import route_for_intent
from backend.agents.state import Intent, LegalState
from backend.rag.domain import DOMAINS, domain_label, infer_domain

logger = logging.getLogger("legal_agent.agents.router")

# --------------------------------------------------------------------------- 意图词表
# 每项：(意图, 权重, 正则)。命中即累加权重。
INTENT_SIGNALS: list[tuple[str, float, re.Pattern[str]]] = [
    # 合同审查
    ("contract_review", 1.0, re.compile(r"(审查|审阅|过一下|看看).{0,8}(合同|协议)")),
    ("contract_review", 0.9, re.compile(r"(合同|协议).{0,6}(风险|有没有问题|哪里有问题|有坑)")),
    ("contract_review", 0.7, re.compile(r"(条款|违约金|赔偿条款).{0,6}(改|修改|怎么改|风险)")),
    ("contract_review", 0.6, re.compile(r"(甲方|乙方|承租方|出租方).{0,20}(应当|不得|有权)")),
    ("contract_review", 0.5, re.compile(r"(补充协议|附件).{0,6}(冲突|不一致|矛盾)")),

    # 类案检索
    ("case_search", 1.0, re.compile(r"(类案|相似案例|类似案例|相关案例|典型案例)")),
    ("case_search", 0.8, re.compile(r"(法院|判决|判例|裁判).{0,6}(怎么判|如何认定|裁判观点)")),
    ("case_search", 0.7, re.compile(r"(有没有|找).{0,4}(案例|判例)")),

    # 法规查询
    ("statute_lookup", 1.0, re.compile(r"《[^》]{2,40}》\s*第\s*[一二三四五六七八九十百零两\d]+\s*条")),
    ("statute_lookup", 0.8, re.compile(r"(法条|条文).{0,6}(是|怎么规定|内容|原文)")),
    ("statute_lookup", 0.7, re.compile(r"(哪|什么)(条|一部法|法律).{0,6}(规定|依据)")),
    ("statute_lookup", 0.6, re.compile(r"(现行有效|废止|失效|修订).{0,8}(吗|了没|情况)")),

    # 文书起草
    ("doc_draft", 1.0, re.compile(r"(起草|写|拟|生成|帮我做).{0,8}(起诉状|答辩状|上诉状|申请书|意见书|律师函|代理词)")),
    ("doc_draft", 0.9, re.compile(r"(起诉状|答辩状|上诉状|申请书|律师函|代理词|法律意见书)")),
    ("doc_draft", 0.7, re.compile(r"(修改稿|对照稿|修订版)")),

    # 案件分析
    ("case_analysis", 1.0, re.compile(r"(分析|评估|研判).{0,6}(这个?案子|本案|案件|纠纷)")),
    ("case_analysis", 0.8, re.compile(r"(胜诉|败诉|赢|输).{0,6}(概率|可能|把握|机会)")),
    ("case_analysis", 0.7, re.compile(r"(诉讼策略|攻防|抗辩思路|怎么打)")),
    ("case_analysis", 0.7, re.compile(r"(争议焦点|争点).{0,8}(是|有哪|归纳)")),
    ("case_analysis", 0.6, re.compile(r"(诉讼时效|时效).{0,6}(过|未过|还有多久|怎么算)")),

    # 法律咨询（兜底性质，权重低）
    ("consult", 0.35, re.compile(r"(怎么办|能不能|是否可以|合法吗|有没有责任|如何维权)")),
    ("consult", 0.30, re.compile(r"(赔偿|补偿|责任|义务).{0,8}(多少|标准|怎么算)")),
]

# --------------------------------------------------------------------------- 非法律信号
# 与法律无关的请求（寒暄、闲聊、IT/写作/翻译/算数…）。
# ⛔ 判成非法律的代价远大于判成法律咨询：前者是"该审的没审"，
#    后者只是"多跑两个节点"。所以这里的词表刻意**保守**——
#    只要同时出现任何法律特征词（见 _LEGAL_MARK_RE），一律不判 off_topic。
OFF_TOPIC_SIGNALS: list[re.Pattern[str]] = [
    re.compile(r"^\s*(你好|您好|hi|hello|嗨|哈喽|在吗|在么|谢谢|感谢|拜拜|再见)\s*[!！。.?？~]*\s*$", re.I),
    re.compile(r"(你是谁|你叫什么|你能做什么|你能干什么|介绍(一下)?你自己|帮我写(一首)?(诗|歌|故事))"),
    re.compile(r"(天气|温度|几度|下雨|股票|基金|彩票|星座|运势)"),
    # 「写……代码」之间常夹着语言名（"写一段 Python 代码"），所以中间要留空隙，
    # 否则这类 IT 请求会因为差几个字而落到法律咨询路径上跑满 7 个节点。
    re.compile(r"(写|编|敲).{0,10}(代码|程序|脚本|sql|正则|接口|函数)", re.I),
    re.compile(r"(报错|bug|堆栈|编译不过|跑不起来|怎么安装|部署教程|环境配置|"
               r"excel|word 批量|数据库|服务器|linux|python|java|javascript)", re.I),
    re.compile(r"(翻译(一下|成)?(英文|中文|日语)|润色(一下)?这段(话|文字))"),
    re.compile(r"(讲(个)?(笑话|故事)|闲聊|聊聊|陪我)"),
]

# 只要命中这些词，就**绝不**判为非法律 —— 它们是"这确实是个法律问题"的强标记
_LEGAL_MARK_RE = re.compile(
    r"(合同|协议|条款|法条|法律|法规|民法典|劳动法|刑法|公司|劳动|租赁|赔偿|补偿|违约金|"
    r"起诉|诉讼|法院|判决|仲裁|律师|甲方|乙方|违法|侵权|解除|辞退|工伤|欠薪|"
    r"离婚|抚养|继承|债务|借条|担保|股权|知识产权|专利|商标|时效|证据|立案|执行|"
    r"处罚|行政|税务|社保|公积金|试用期|竞业限制|保密)"
)

# --------------------------------------------------------------------------- 复杂度降档
# 判为 consult / case_analysis 之后，再看**问题本身是否简单**，简单就降档为
# quick_answer（StatuteNode → ValidatorNode → ReportNode 三个节点）。
#
# 三条判据必须同时成立才降档（宁可多跑，不可漏跑）：
#   1. 短：去掉空白后 ≤ QUICK_MAX_LEN 字（长问句通常自带多事实/多诉求）；
#   2. 无材料：本次没上传任何材料（有材料就要解析，IntakeNode 不能省）；
#   3. 无"重作业"动词：没有审查/起草/分析/起诉/策略/逐条/帮我改这类诉求。
# 另外**只问一件事**：多个问号/分号说明是多问题，降档会答不全。
QUICK_MAX_LEN = 40
# 末段那组是**咨询类诉求**（怎么办/能不能/如何维权…）：它们句子通常很短，
# 只看"短 + 无审查动词"会把"公司辞退我怎么办"误判成简单问题 ——
# 而这类提问恰恰是真正需要完整证据链的（涉及事实认定与举证责任），
# 降档给出的"一句结论"反而会误导律师。所以它们一律视为重作业。
# 真机踩过："用人单位未签书面劳动合同，劳动者可以主张哪些权利？"
# 与上一组全部不沾边，被降成 quick_answer（只查法条、不跑类案），
# 报告里"类案 0 个"其实是路由裁掉的，不是库里没有。
# 故"主张权利/索赔/可以要求…"这类**要救济方案**的问法也一并算重作业。
_HEAVY_DEMAND_RE = re.compile(
    r"(审查|审阅|过一下|逐条|起草|拟|写(一份|个)?(起诉状|答辩状|意见书|律师函|合同)|"
    r"分析|评估|研判|策略|攻防|怎么打|胜诉|把握|归纳|整理(一份)?(报告|意见)|帮我改|"
    r"风险(点|清单)|对照稿|修改(稿|建议)|"
    r"怎么办|该如何|如何维权|能不能|是否可以|合法吗|违法吗|有没有责任|要不要紧|"
    r"主张|维权|索赔|可以要求|有哪些权利|什么权利)"
)


def looks_off_topic(query: str, *, has_material: bool = False) -> bool:
    """是否与法律无关。**保守判定**：有材料或命中法律特征词时一律返回 False。

    为什么"有材料就不算闲聊"：律师上传了合同再打字，字多半是"看看这个"，
    那句话本身确实像闲聊，但材料才是真正的输入 —— 判成闲聊等于把合同晾在一边。
    """
    text = (query or "").strip()
    if not text or has_material:
        return False
    if _LEGAL_MARK_RE.search(text):
        return False
    return any(p.search(text) for p in OFF_TOPIC_SIGNALS)


def looks_quick(query: str, *, has_material: bool = False) -> bool:
    """法律问题但是不是"一句话能答完"的简单问题。

    返回 True 只表示"可以降档"，**调用方仍要确认它确实是个法律问题**
    （非法律的走 off_topic，不该走这条）。
    """
    text = (query or "").strip()
    if not text or has_material:
        return False
    if len(re.sub(r"\s+", "", text)) > QUICK_MAX_LEN:
        return False
    if _HEAVY_DEMAND_RE.search(text):
        return False
    # 多个问号/分号/顿号分隔的并列问题 → 不是单一问题，不降档
    if len(re.findall(r"[？?；;]", text)) > 1:
        return False
    # 必须问的是"法律的事"：没有任何法律特征词时，它多半是闲聊，交给 off_topic 处理
    return bool(_LEGAL_MARK_RE.search(text))


# 文书类型 → 起草意图时的 doc_type 与 draft_type
DOC_TYPES: list[tuple[str, str, re.Pattern[str]]] = [
    ("complaint", "litigation", re.compile(r"(起诉状|诉状|起诉)")),
    ("defense", "litigation", re.compile(r"(答辩状|答辩)")),
    ("appeal", "litigation", re.compile(r"(上诉状|上诉)")),
    ("opinion", "opinion", re.compile(r"(法律意见书|意见书|代理词|律师函)")),
    ("application", "litigation", re.compile(r"(申请书|仲裁申请)")),
    ("contract_revision", "contract", re.compile(r"(修改稿|对照稿|修订)")),
]

AMBIGUOUS_LOW = 0.5
AMBIGUOUS_GAP = 0.25


@dataclass
class IntentResult:
    """意图判定结果（含可解释的命中详情）。

    ``domain`` 为法律领域（labor/tax/env/civil/unknown），与意图**并行判定**：
    由 :func:`backend.rag.domain.infer_domain` 规则判定，LLM 兜底（规则无法
    判定且模型可用时）；unknown 表示无法可靠判定，检索层不做领域过滤。
    """

    intent: Intent
    confidence: float
    hits: list[str] = field(default_factory=list)
    doc_type: str = "unknown"
    draft_type: str = ""
    decided_by: str = "rule"
    notes: list[str] = field(default_factory=list)
    domain: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "domain": self.domain,
            "confidence": round(self.confidence, 2),
            "hits": self.hits,
            "doc_type": self.doc_type,
            "draft_type": self.draft_type,
            "decided_by": self.decided_by,
            "notes": self.notes,
        }


class IntentRouter:
    """意图判定 + doc_type 推断。"""

    async def classify(self, query: str, *, material_text: str = "",
                       material_doc_type: str = "") -> IntentResult:
        scores: dict[str, float] = {}
        hits: list[str] = []
        blob = f"{query}\n{query}"  # 咨询语本身权重加倍（材料文本只在末尾少量参与）

        # 领域与意图并行判定：先按规则词表判一次（确定性、可回归测试），
        # 规则判不出（unknown）且走到 LLM 兜底分支时再由 LLM 补判。
        rule_domain = infer_domain(query, material_text=material_text,
                                   material_doc_type=material_doc_type)

        for intent, weight, pat in INTENT_SIGNALS:
            if pat.search(blob):
                scores[intent] = scores.get(intent, 0.0) + weight
                hits.append(f"{intent}:{pat.pattern[:24]}")

        # 有材料时，材料形态是强信号
        if material_text:
            if material_doc_type == "contract":
                scores["contract_review"] = scores.get("contract_review", 0.0) + 0.5
                hits.append("有合同材料")
            elif material_doc_type in ("litigation", "evidence", "opinion"):
                scores["case_analysis"] = scores.get("case_analysis", 0.0) + 0.3
                hits.append("有诉讼材料")

        # ---- 复杂度分档（在"定方向"之后、"定节点"之前）
        # 顺序是刻意的：**先看是不是法律问题，再看这个问题有多复杂**。
        # 反过来（先判简单）会把"你好"当成简单法律问题送进 StatuteNode 检索。
        # 无规则信号 = 连一个法律特征词都没命中，此时才轮到 off_topic / quick_answer。
        if not scores:
            if looks_off_topic(query, has_material=bool(material_text)):
                return IntentResult(
                    intent="off_topic", confidence=0.8, decided_by="rule",
                    hits=["非法律信号"], notes=["与法律无关，直接作答，不进入检索与终审"],
                    domain=rule_domain,
                )
            if looks_quick(query, has_material=bool(material_text)):
                return IntentResult(
                    intent="quick_answer", confidence=0.6, decided_by="rule",
                    hits=["简单法律问题"],
                    notes=["单一法律问题，按快速问答路径执行（仅检索法条后直答）"],
                    doc_type=_doc_type(material_doc_type, query, "quick_answer"),
                    domain=rule_domain,
                )

        if not scores:
            # 完全没有规则信号：先让 LLM 试着判定一次（仅模型可用时调，
            # 降级到规则引擎时 _llm_refine 直接返回 None，退回默认行为，不影响降级）
            refined = await self._llm_refine(query)
            if refined:
                domain = rule_domain
                if domain == "unknown":
                    domain = await self._llm_refine_domain(query) or "unknown"
                return IntentResult(
                    intent=refined, confidence=0.65, hits=["LLM 兜底判定"],
                    decided_by="llm",
                    doc_type=_doc_type(material_doc_type, query, refined),
                    draft_type=_draft_type(query, refined),
                    notes=["规则未命中任何意图特征词，改由 LLM 判定"],
                    domain=domain,
                )
            return IntentResult(
                intent="consult", confidence=0.35, decided_by="default",
                doc_type=_doc_type(material_doc_type, query, "consult"),
                notes=["未命中任何意图特征词，按最通用的「法律咨询」路径执行"],
                domain=rule_domain,
            )

        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        top_intent, top_score = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0.0

        # 弱信号 + 问题本身很简单 ⇒ 降档为快速问答。
        # 例：「竞业限制补偿金最低标准是多少」只命中 consult 的 0.3 分弱信号，
        # 但它要的只是一个数额，跑 7 个节点（含类案检索与争议焦点归纳）纯属浪费。
        # 判定的闸门是 :func:`looks_quick`（短、无材料、无重作业诉求），
        # 其中"重作业诉求"已把「怎么办/能不能」这类真咨询排除在外。
        if top_score < AMBIGUOUS_LOW and looks_quick(query, has_material=bool(material_text)):
            return IntentResult(
                intent="quick_answer", confidence=0.55, hits=hits, decided_by="rule",
                doc_type=_doc_type(material_doc_type, query, "quick_answer"),
                notes=[f"规则仅命中弱信号（{top_intent}={top_score:.2f}）且问题为单一结论型，"
                       f"已降档为快速问答（{len(route_for_intent('quick_answer')['nodes'])} 个节点）"],
                domain=rule_domain,
            )

        # 模糊区间：让 LLM 试着区分；LLM 不可用就沿用规则结果并声明
        if top_score < AMBIGUOUS_LOW or (second and top_score - second < AMBIGUOUS_GAP):
            refined = await self._llm_refine(query)
            if refined:
                # ⚠ ranked 可能只有**一个**元素：长段案情描述往往只命中 consult 一个
                # 意图，但分数低于模糊阈值也会进这个分支 —— 这里曾直接取 ranked[1]
                # 而 IndexError，IntentNode 整个失败、流程退回"自动判定"
                # （置信度 0.15、零引用，页面上只看到一句"证据缺口"）。
                runner_up = f"{ranked[1][0]}={second:.2f}" if len(ranked) > 1 else "无其他候选"
                domain = rule_domain
                if domain == "unknown":
                    domain = await self._llm_refine_domain(query) or "unknown"
                return IntentResult(
                    intent=refined, confidence=0.7, hits=hits, decided_by="llm",
                    doc_type=_doc_type(material_doc_type, query, refined),
                    draft_type=_draft_type(query, refined),
                    notes=[f"规则得分接近（{top_intent}={top_score:.2f} vs "
                           f"{runner_up}），改由 LLM 判定"],
                    domain=domain,
                )
            notes = [f"规则判定存在歧义（{top_intent}={top_score:.2f}），"
                     f"LLM 不可用，沿用规则结果"]
            return IntentResult(
                intent=top_intent, confidence=round(min(0.6, top_score), 2),  # type: ignore[arg-type]
                hits=hits, decided_by="rule-ambiguous",
                doc_type=_doc_type(material_doc_type, query, top_intent),
                draft_type=_draft_type(query, top_intent), notes=notes,
                domain=rule_domain,
            )

        conf = min(0.95, 0.5 + top_score / 2)
        return IntentResult(
            intent=top_intent, confidence=round(conf, 2), hits=hits,
            decided_by="rule",
            doc_type=_doc_type(material_doc_type, query, top_intent),
            draft_type=_draft_type(query, top_intent),
            domain=rule_domain,
        )

    async def _llm_refine(self, query: str) -> str | None:
        """仅用于歧义消解；任何异常都返回 None（不把歧义变成错误）。

        注意这里**必须先确认 provider 不是 rule**：``LLMClient`` 在探测不到模型时
        会降级成 ``RuleProvider``，如果直接调 ``chat_json``，规则引擎会返回一个
        "看起来像 LLM 判定"的结果，让 ``decided_by`` 谎报为 llm。
        """
        try:
            from backend.llm.client import llm_client

            provider = await llm_client.ensure_provider()
            if provider.name == "rule":
                logger.info("无可用的真实模型服务，意图歧义不交由 LLM 消解")
                return None

            data, _result = await llm_client.chat_json(
                [
                    {"role": "system",
                     "content": "你是法律任务意图分类器。只输出 JSON，不要任何解释。"},
                    {"role": "user",
                     "content": f"用户请求：{query}\n"
                                '请判定意图，输出 {"intent": "..."}，'
                                "可选值：consult（法律咨询）、contract_review（合同审查）、"
                                "case_search（类案检索）、statute_lookup（法规查询）、"
                                "doc_draft（文书起草）、case_analysis（案件分析）、"
                                "quick_answer（一句话能答完的简单法律问题，如期限/数额/概念解释）、"
                                "off_topic（与法律无关，如寒暄、闲聊、IT 问题）"},
                ],
                task="intent",
                default={"intent": ""},
            )
            intent = (data or {}).get("intent", "")
            if intent in ("consult", "contract_review", "case_search",
                          "statute_lookup", "doc_draft", "case_analysis",
                          "quick_answer", "off_topic"):
                return intent
            logger.info("LLM 返回了无法识别的意图：%r", intent)
        except Exception as exc:  # noqa: BLE001
            logger.info("LLM 意图判定不可用：%s", exc)
        return None

    async def _llm_refine_domain(self, query: str) -> str | None:
        """领域判定的 LLM 兜底：仅在规则判出 unknown 且模型可用时调用。

        与 :meth:`_llm_refine` 同策略 —— 任何异常 / 非法返回值都返回 None，
        由调用方回退到 ``"unknown"``（检索层不过滤），不把领域判定变成错误。
        """
        try:
            from backend.llm.client import llm_client

            provider = await llm_client.ensure_provider()
            if provider.name == "rule":
                logger.info("无可用的真实模型服务，领域判定不交由 LLM 兜底")
                return None

            data, _result = await llm_client.chat_json(
                [
                    {"role": "system",
                     "content": "你是法律问题领域分类器。只输出 JSON，不要任何解释。"},
                    {"role": "user",
                     "content": f"用户请求：{query}\n"
                                '请判定法律领域，输出 {"domain": "..."}，'
                                '可选值：labor（劳动/社保）、tax（税务/财税）、'
                                'env（生态环境/资源）、civil（民事/商事）、'
                                'criminal（刑事）、admin（行政）、constitution（宪法）、'
                                'ip（知识产权）、transport（交通运输）、military（军事）、'
                                'agriculture（农业）、trade（国际贸易）、safety（安全生产）、'
                                'culture（文化教育）、ethnic（民族事务）、'
                                'social_security（社会保障）、legislation（立法程序）、'
                                'judicature（司法制度）、economy（经济法）、'
                                'unknown（无法判定）'},
                ],
                task="intent_domain",
                default={"domain": ""},
            )
            domain = (data or {}).get("domain", "")
            if domain in DOMAINS:
                return domain
            logger.info("LLM 返回了无法识别的领域：%r", domain)
        except Exception as exc:  # noqa: BLE001
            logger.info("LLM 领域判定不可用：%s", exc)
        return None

    # ------------------------------------------------------------------ 入口
    async def route(self, state: LegalState, harness: Any = None) -> IntentResult:
        """节点式入口：写入 ``state.intent`` / ``doc_type``。"""
        result = await self.classify(
            state.get("query") or "",
            material_text=state.get("material_text") or "",
            material_doc_type=state.get("doc_type") or "",
        )
        state["intent"] = result.intent
        state["doc_type"] = result.doc_type  # type: ignore[typeddict-item]
        state["domain"] = result.domain  # type: ignore[typeddict-item]
        if result.draft_type:
            state["draft_type"] = result.draft_type

        if harness is not None:
            harness.tracer.info(
                "IntentNode", "classify",
                f"intent={result.intent} domain={result.domain} conf={result.confidence} "
                f"doc_type={result.doc_type} by={result.decided_by}",
            )
            for h in result.hits[:8]:
                harness.tracer.info("IntentNode", "signal", h)
        logger.info("意图判定：%s（置信度 %.2f，依据 %s）→ doc_type=%s domain=%s",
                    result.intent, result.confidence, result.decided_by,
                    result.doc_type, result.domain)
        return result


# --------------------------------------------------------------------------- 工具
def _doc_type(material_doc_type: str, query: str, intent: str) -> str:
    """推断材料形态。材料解析结果优先于关键词猜测。"""
    if material_doc_type and material_doc_type != "unknown":
        return material_doc_type
    if intent == "contract_review":
        return "contract"
    if intent in ("case_analysis", "doc_draft", "case_search"):
        return "litigation"
    if intent == "statute_lookup":
        return "statute"
    # 从问句里找线索
    if re.search(r"(合同|协议|甲方|乙方|出租|承租)", query or ""):
        return "contract"
    if re.search(r"(起诉|被告|原告|判决|案件)", query or ""):
        return "litigation"
    return "consult"


def _draft_type(query: str, intent: str) -> str:
    if intent != "doc_draft":
        # 其它意图也可能顺带要一份文书
        for draft_type, _dt, pat in DOC_TYPES:
            if pat.search(query or ""):
                return draft_type
        return ""
    for draft_type, _dt, pat in DOC_TYPES:
        if pat.search(query or ""):
            return draft_type
    return "complaint"


def explicit_result(intent: str, query: str, *,
                    material_doc_type: str = "") -> IntentResult | None:
    """按调用方**钉住**的意图构造判定结果；意图不在路由表里时返回 ``None``。

    用途：产品页面的每个 Agent 入口（类案检索 / 文书中心）各自对应一种意图，
    不能靠分类器"猜对"。原先没有这条路 —— 入口传的 ``intent`` 只写进任务表，
    而 IntentNode 无条件重跑分类并覆写，于是页面的语义与管线跑的东西
    可以默默地不一致。

    ``doc_type`` / ``draft_type`` 仍然要推断：它们决定检索式与文书落款立场，
    与"跑哪条路由"是两件事。推断复用分类路径上的同一对函数，
    **不另写一份** —— 两份推断早晚分叉，而分叉的表现是"同一份材料，
    从按钮进去和从输入框进去，检索式不一样"。

    ``confidence`` 给 1.0 是诚实的：这不是判定，是调用方的要求。
    ``decided_by="explicit"`` 让 Trace、报告与事后排障都能一眼区分
    "它判对了"与"入口钉住了、它没判"。
    """
    from backend.agents.harness.router import INTENT_ROUTES

    if intent not in INTENT_ROUTES:
        return None
    return IntentResult(
        intent=intent,  # type: ignore[arg-type]
        confidence=1.0,
        hits=[],
        decided_by="explicit",
        doc_type=_doc_type(material_doc_type, query, intent),
        draft_type=_draft_type(query, intent),
        notes=[f"意图由调用方显式指定（{INTENT_ROUTES[intent].get('label', intent)}），未走分类器"],
        domain=infer_domain(query, material_doc_type=material_doc_type),
    )


router = IntentRouter()
