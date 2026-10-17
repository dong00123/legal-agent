"""法律领域（domain）判定与检索过滤。

对应「先识别法律领域，再按领域检索与回答」的改造：
1. :func:`infer_domain` —— 规则优先的领域判定，与 ``router.IntentRouter``
   的"规则优先、LLM 兜底"同一策略；
2. :func:`filter_statutes_by_domain` / :func:`filter_knowledge_by_domain` ——
   在检索层按领域滤除跨领域错配（如劳动问题召回环保税法/知识产权条例）。

为什么单独放 ``rag`` 包而不是 ``agents`` 包：
- 检索工具（``tools/statute_tools.py`` / ``tools/knowledge_tools.py``）与
  纯 RAG 链路（``services/rag_answer.py`` → ``rag/pipeline.py``）都在用；
- agents 层的 ``router.py`` 也引用 :func:`infer_domain`（意图结果带 domain 字段）。
  放在 ``agents`` 会让 rag 层依赖 agents 层（方向反了）；放在这里两边都能引用。

domain 枚举与 ``agents.state.LegalDomain`` 一致，覆盖 ``data/raw`` 下全部 18 个
领域目录（见 :data:`RAW_DIR_DOMAIN`）：labor / tax / env / civil / criminal /
admin / constitution / ip / transport / military / agriculture / trade / safety /
culture / ethnic / social_security / legislation / judicature / economy / unknown。
其中 civil 为民事/商事兜底（无专属 raw 目录，民法、合同法、婚姻继承等散落各处）。
"""

from __future__ import annotations

import re
from typing import Any

# 领域枚举。**与 agents/state.py 的 LegalDomain 保持一致**，此处仅以字符串使用，
# 避免检索层与状态层互相 import。
DOMAINS = (
    "labor", "tax", "env", "civil",
    "criminal", "admin", "constitution", "ip", "transport",
    "military", "agriculture", "trade", "safety", "culture",
    "ethnic", "social_security", "legislation", "judicature", "economy",
    "unknown",
)

# --------------------------------------------------------------------------- 目录兜底
# ``data/raw`` 下 18 个领域目录 → domain 的映射。检索过滤的最后一道兜底：
# 词表没覆盖到的法条，只要 ``source_path`` 落在某个 raw 目录下，就按目录归属
# 该领域，避免手工词表遗漏造成"该领域法条被全部滤除"。
RAW_DIR_DOMAIN: dict[str, str] = {
    "交通运输": "transport",
    "军事法": "military",
    "农业法": "agriculture",
    "刑事": "criminal",
    "劳动法": "labor",
    "司法制度": "judicature",
    "国际贸易": "trade",
    "安全生产": "safety",
    "宪法": "constitution",
    "文化教育": "culture",
    "民族事务": "ethnic",
    "环境资源": "env",
    "知识产权法": "ip",
    "社会保障": "social_security",
    "立法程序": "legislation",
    "经济法": "economy",
    "行政法": "admin",
    "财税": "tax",
}

_RAW_DIR_RE = re.compile(r"[\\/]data[\\/]raw[\\/]([^\\/]+?)[\\/]")


def raw_dir_of(path: str) -> str:
    """从 source_path 里取 ``data\\raw\\<目录>`` 的目录名；取不到返回空串。"""
    m = _RAW_DIR_RE.search(path or "")
    return m.group(1) if m else ""


def raw_dir_domain(path: str) -> str:
    """source_path 目录兜底映射 → domain；未知目录返回空串。"""
    return RAW_DIR_DOMAIN.get(raw_dir_of(path), "")


# --------------------------------------------------------------------------- 领域信号
# 每项：(domain, 权重, 正则)。命中即累加权重。
#
# 权重的口径与 router.INTENT_SIGNALS 相同：权重越高越有决定性。
# 法规名强信号给 2.0（用户直接点名了领域），特征词给 0.8~1.0，
# 弱线索（如"发票""环境"这类可能跨域的通用词）给 0.5。
DOMAIN_SIGNALS: list[tuple[str, float, re.Pattern[str]]] = [
    # ---- 劳动 / 社保（强，raw 目录：劳动法）
    ("labor", 2.0, re.compile(r"(劳动合同法|劳动法|劳动争议调解仲裁法|工伤保险条例|社会保险法|"
                              r"劳动保障监察条例|劳务派遣暂行规定|职工带薪年休假条例|最低工资规定|"
                              r"劳动争议仲裁)")),
    ("labor", 1.0, re.compile(r"(劳动|劳动合同|辞退|裁员|解雇|离职|辞职|被开除|加班|加班费|欠薪|"
                              r"工资|薪资|社保|五险一金|公积金|工伤|职业病|试用期|年假|年休假|"
                              r"竞业限制|劳务派遣|用人单位|劳动者|经济补偿|赔偿金|双倍工资|"
                              r"产假|哺乳|女职工|带薪|劳动仲裁|劳动争议)")),
    ("labor", 0.8, re.compile(r"(劳动局|人社局|劳动监察|仲裁时效|违法解除|调岗|降薪|末位淘汰|"
                              r"解除劳动合同)")),

    # ---- 税务 / 财税（强，raw 目录：财税）
    ("tax", 2.0, re.compile(r"(个人所得税法|企业所得税法|增值税法|税收征收管理法|契税法|"
                            r"房产税|印花税法|消费税法|环境保护税法|注册会计师法)")),
    ("tax", 1.0, re.compile(r"(个税|税务|纳税|补税|退税|申报|逃税|偷税|漏税|骗税|虚开|"
                            r"企业所得税|增值税|发票|税务稽查|税收|税)")),
    ("tax", 0.5, re.compile(r"(税率|税额|税种|纳税人)")),

    # ---- 生态环境 / 环境资源（强，raw 目录：环境资源）
    ("env", 2.0, re.compile(r"(生态环境法典|环境保护法|环境影响评价法|大气污染防治法|"
                            r"水污染防治法|固体废物污染环境防治法|噪声污染防治法|"
                            r"土壤污染防治法|排污许可管理条例|原子能法|国家公园法)")),
    ("env", 1.0, re.compile(r"(环保|污染|排污|排放|废水|废气|固废|危废|噪声|噪音|环评|"
                            r"生态环境|环保税|绿色税|生态补偿|原子能|核能|放射性|国家公园|自然保护)")),
    ("env", 0.5, re.compile(r"(环境|大气|污水|扬尘|垃圾处理|环保部门)")),

    # ---- 民事 / 商事 / 诉讼（兜底，无专属 raw 目录）
    ("civil", 2.0, re.compile(r"(民法典|民事诉讼法)")),
    ("civil", 1.0, re.compile(r"(合同|协议|租赁|买卖|借款|借贷|欠款|债务|担保|抵押|定金|"
                              r"继承|遗嘱|离婚|抚养|赡养|彩礼|侵权|交通事故|房屋|房产|商铺|"
                              r"装修|物业|违约|货款|中介|合伙|股权|分红|欠条|借条|"
                              r"民事诉讼|起诉|应诉|败诉|胜诉|财产保全|执行)")),
    ("civil", 0.8, re.compile(r"(纠纷|赔偿损失|返还|解除合同|履行合同|货款|商业秘密)")),
    ("civil", 0.5, re.compile(r"(公司|股东|法定代表人|消费者|退货|保修|质量)")),

    # ---- 刑事（raw 目录：刑事）
    ("criminal", 2.0, re.compile(r"(刑法|刑事诉讼法|反有组织犯罪法|社区矫正法|"
                                 r"全国人民代表大会常务委员会关于.*刑事诉讼法.*的解释)")),
    ("criminal", 1.0, re.compile(r"(犯罪|刑事|刑罚|判刑|有期徒刑|拘役|管制|罚金|没收财产|"
                                 r"逮捕|刑事拘留|侦查|公诉|嫌疑人|被告人|罪犯|故意杀人|盗窃|"
                                 r"抢劫|诈骗罪|贪污|受贿|贩毒|毒品|走私)")),
    ("criminal", 0.8, re.compile(r"(立案侦查|刑事立案|提起公诉|认罪认罚|缓刑|减刑|假释)")),

    # ---- 行政（raw 目录：行政法）
    ("admin", 2.0, re.compile(r"(治安管理处罚法|行政处罚法|行政许可法|行政强制法|行政复议法|"
                              r"行政诉讼法|公务员法|监察法|村民委员会组织法|"
                              r"城市居民委员会组织法|信访条例)")),
    ("admin", 1.0, re.compile(r"(行政|行政处罚|行政拘留|治安管理|治安拘留|公安机关|派出所|行政强制|"
                              r"行政许可|行政复议|行政诉讼|政府信息公开|信访|居委会|村委会|"
                              r"村民自治|居务公开|政务)")),
    ("admin", 0.8, re.compile(r"(罚款|责令|吊销许可证|行政相对人|行政行为|执法人员|城管)")),

    # ---- 宪法（raw 目录：宪法）
    ("constitution", 2.0, re.compile(r"(宪法|宪法修正案)")),
    ("constitution", 1.0, re.compile(r"(根本法|国体|政体|宪法宣誓|违宪|全国人民代表大会|"
                                     r"人大常委会|公民基本权利|国家机构)")),
    ("constitution", 0.5, re.compile(r"(基本权利|人民代表大会|选举权)")),

    # ---- 知识产权（raw 目录：知识产权法）
    ("ip", 2.0, re.compile(r"(专利法|商标法|著作权法|知识产权保护条例|知识产权海关保护条例|"
                           r"专利法实施细则|商标法实施条例|著作权法实施条例|"
                           r"集成电路布图设计|地理标志)")),
    ("ip", 1.0, re.compile(r"(专利|商标|著作权|版权|知识产权|发明|实用新型|外观设计|驰名商标|"
                           r"盗版|抄袭|版权登记|专利申请|商标注册|商业秘密)")),
    ("ip", 0.5, re.compile(r"(原创|山寨|创意|侵权赔偿)")),

    # ---- 交通运输（raw 目录：交通运输）
    ("transport", 2.0, re.compile(r"(民用航空法|海商法|铁路法|公路法|港口法|道路交通安全法|"
                                  r"海上交通安全法|航道法|邮政法)")),
    ("transport", 1.0, re.compile(r"(交通运输|交通|运输|承运|海运|航运|航空|民航|船舶|船|"
                                  r"码头|港口|铁路|火车|列车|公路|道路|机动车|车辆|驾驶|"
                                  r"驾驶证|交通事故|货运|客运|物流|海商)")),
    ("transport", 0.5, re.compile(r"(出行|航班|车票|船票)")),

    # ---- 军事（raw 目录：军事法）
    ("military", 2.0, re.compile(r"(国防动员法|国防法|军事设施保护法|兵役法|军事)")),
    ("military", 1.0, re.compile(r"(军队|军人|士兵|军官|国防|动员|预备役|军事|演习|武装|"
                                 r"征兵|服役|军衔|军事法庭)")),
    ("military", 0.5, re.compile(r"(战争|退役|军人权益)")),

    # ---- 农业（raw 目录：农业法）
    ("agriculture", 2.0, re.compile(r"(渔业法|农业法|种子法|农产品质量安全法|畜牧法|"
                                    r"动物防疫法|森林法|草原法|农村土地承包法)")),
    ("agriculture", 1.0, re.compile(r"(农业|农民|农村|耕地|农田|农作物|种植|养殖|畜牧|渔业|"
                                    r"渔|捕捞|水产|农产品|农机|农药|化肥|种子|粮食|"
                                    r"土地承包|宅基地)")),
    ("agriculture", 0.5, re.compile(r"(乡村|合作社|农用地)")),

    # ---- 国际贸易（raw 目录：国际贸易）
    ("trade", 2.0, re.compile(r"(对外贸易法|外商投资法|海关法|进出口商品检验法|关税法)")),
    ("trade", 1.0, re.compile(r"(对外贸易|进出口|贸易|关税|海关|外资|外商投资|跨境|外汇|"
                              r"配额|出口|进口|国际贸易|反倾销|反补贴|贸易壁垒|WTO)")),
    ("trade", 0.5, re.compile(r"(商品贸易|服务贸易|贸易协定)")),

    # ---- 安全生产（raw 目录：安全生产）
    ("safety", 2.0, re.compile(r"(食品安全法|危险化学品安全法|安全生产法|消防法|"
                               r"突发事件应对法|特种设备安全法|矿山安全法)")),
    ("safety", 1.0, re.compile(r"(安全|危险|化学品|食品|食品安全|生产安全|安全生产|事故|消防|"
                               r"火灾|爆炸|中毒|辐射|应急预案|安全监管|隐患排查)")),
    ("safety", 0.5, re.compile(r"(质量|合格|标签|保质期|召回)")),

    # ---- 文化教育（raw 目录：文化教育）
    ("culture", 2.0, re.compile(r"(国家通用语言文字法|法治宣传教育法|教育法|义务教育法|"
                                r"高等教育法|教师法|文物保护法|出版管理条例)")),
    ("culture", 1.0, re.compile(r"(教育|学校|教师|学生|学位|语言文字|普通话|汉字|文化|文物|"
                                r"出版|媒体|新闻|广播|电影|电视|培训|教科书)")),
    ("culture", 0.5, re.compile(r"(书籍|图书馆|博物馆|文化遗产)")),

    # ---- 民族事务（raw 目录：民族事务）
    ("ethnic", 2.0, re.compile(r"(民族团结进步促进法|民族区域自治法)")),
    ("ethnic", 1.0, re.compile(r"(民族|少数民族|民族团结|自治州|自治县|民族自治|"
                               r"宗教信仰自由|风俗习惯)")),
    ("ethnic", 0.5, re.compile(r"(清真|民族政策)")),

    # ---- 社会保障（raw 目录：社会保障）
    ("social_security", 2.0, re.compile(r"(社会救助法|突发公共卫生事件应对法|慈善法|"
                                        r"红十字会法|退役军人保障法|妇女权益保障法|"
                                        r"老年人权益保障法|未成年人保护法|残疾人保障法)")),
    ("social_security", 1.0, re.compile(r"(社会救助|救助|低保|特困|慈善|捐赠|突发公共卫生|"
                                        r"疫情|防控|疫苗|退役军人|妇女|未成年人|儿童|老年人|"
                                        r"养老|残疾人|福利|保障)")),
    ("social_security", 0.5, re.compile(r"(困难群众|弱势群体|救济)")),

    # ---- 立法程序（raw 目录：立法程序）
    ("legislation", 2.0, re.compile(r"(立法法|立法程序|法律清理)")),
    ("legislation", 1.0, re.compile(r"(立法|法律制定|法律修改|法律清理|立法规划|法律草案|"
                                    r"法律解释|法律决定|法制工作)")),
    ("legislation", 0.5, re.compile(r"(条例制定|授权立法|法律效力位阶)")),

    # ---- 司法制度（raw 目录：司法制度）
    ("judicature", 2.0, re.compile(r"(仲裁法|律师法|法官法|检察官法|公证法|"
                                   r"人民法院组织法|人民检察院组织法|人民调解法)")),
    ("judicature", 1.0, re.compile(r"(仲裁|律师|法官|检察官|法院|检察院|法庭|司法|审判|"
                                   r"裁判|公证|调解|诉讼程序|管辖|证据规则|司法制度|"
                                   r"法律援助|司法鉴定)")),
    ("judicature", 0.8, re.compile(r"(商事仲裁|仲裁协议|仲裁条款|仲裁庭|仲裁裁决)")),

    # ---- 经济法（raw 目录：经济法）
    ("economy", 2.0, re.compile(r"(反不正当竞争法|网络安全法|国家发展规划法|公司法|"
                                r"合伙企业法|证券法|商业银行法|保险法|票据法|企业破产法|"
                                r"反垄断法|电子商务法|消费者权益保护法|产品质量法|"
                                r"招标投标法|政府采购法|价格法)")),
    ("economy", 1.0, re.compile(r"(公司|企业|证券|股票|债券|期货|银行|信贷|保险|票据|破产|"
                                r"清算|并购|重组|投资|融资|反垄断|反不正当竞争|垄断|竞争|"
                                r"网络安全|数据|个人信息|经营者|消费者|市场份额|招标|投标|"
                                r"价格|发展规划)")),
    ("economy", 0.5, re.compile(r"(市场|监管|经济|商务|电商)")),
]

# 材料形态对领域的强提示（与 router 的 material_doc_type 同一口径）
_MATERIAL_DOC_TYPE_SIGNAL: dict[str, tuple[str, float]] = {
    "contract": ("civil", 1.0),
    "litigation": ("civil", 0.6),   # 诉讼材料可能是劳动仲裁/民诉/刑诉，权重压低，规则词优先
    "opinion": ("civil", 0.5),
}

_DOMAIN_NAMES = {
    "labor": "劳动/社保",
    "tax": "税务/财税",
    "env": "生态环境/资源",
    "civil": "民事/商事",
    "criminal": "刑事",
    "admin": "行政",
    "constitution": "宪法",
    "ip": "知识产权",
    "transport": "交通运输",
    "military": "军事",
    "agriculture": "农业",
    "trade": "国际贸易",
    "safety": "安全生产",
    "culture": "文化教育",
    "ethnic": "民族事务",
    "social_security": "社会保障",
    "legislation": "立法程序",
    "judicature": "司法制度",
    "economy": "经济法",
    "unknown": "无法判定",
}


def domain_label(domain: str) -> str:
    return _DOMAIN_NAMES.get(domain or "", "无法判定")


def infer_domain(query: str, *, material_text: str = "",
                 material_doc_type: str = "") -> str:
    """规则优先的领域判定。返回 domain 枚举之一（见 :data:`DOMAINS`）。

    口径与 ``IntentRouter.classify`` 一致：命中信号累加权重，取最高分；
    最高分 < 1.0 时判 ``unknown``（弱信号不硬猜，交给检索层全量召回）。

    注意：``material_doc_type == "litigation"`` 只加 0.6 —— 劳动仲裁材料与
    民事诉讼材料都可能是"诉讼材料"，规则词命中时以规则词为准。
    """
    blob = f"{query or ''}\n{material_text or ''}"
    scores: dict[str, float] = {}
    for domain, weight, pat in DOMAIN_SIGNALS:
        if pat.search(blob):
            scores[domain] = scores.get(domain, 0.0) + weight
    if material_doc_type and material_doc_type != "unknown":
        dom, w = _MATERIAL_DOC_TYPE_SIGNAL.get(material_doc_type, ("", 0.0))
        if dom:
            scores[dom] = scores.get(dom, 0.0) + w
    if not scores:
        return "unknown"
    top, top_score = max(scores.items(), key=lambda kv: kv[1])
    return top if top_score >= 1.0 else "unknown"


# --------------------------------------------------------------------------- 检索过滤
# 领域 → 法规名关键词（子串匹配 doc_name / source_path，口径与
# ``statute_tools.statute_search`` 的 ``category`` 过滤一致）。
#
# 词表口径：**只收该领域的专属标识词**，不收跨域通用词 ——
# 例如 civil 不收"纠纷"（劳动纠纷也带纠纷）、labor 不收"社保"以外的宽泛词。
# 宁可少召回（领域库确实没有时如实报告），也不要拉进错配法条。
#
# 除关键词外，:func:`filter_statutes_by_domain` 还按 ``source_path`` 的
# ``data\\raw\\<目录>`` 做目录兜底（见 :data:`RAW_DIR_DOMAIN`），
# 保证每个 raw 目录的法条都能归入正确领域，不依赖词表逐条收录。
_STATUTE_DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "labor": (
        "劳动", "劳动合同", "劳动争议", "工伤", "社会保险", "工伤保险", "工资",
        "劳务派遣", "女职工", "年休假", "最低工资", "劳动保障监察",
    ),
    "tax": ("税", "税收", "税务", "财政", "注册会计师"),
    "env": ("环境", "生态", "污染", "排污", "原子能", "国家公园", "自然保护区"),
    "civil": ("民法典", "民事", "民事诉讼"),
    "criminal": ("刑法", "刑事", "刑事诉讼法", "犯罪", "刑罚"),
    "admin": ("治安管理", "行政处罚", "行政许可", "行政强制", "行政复议", "行政诉讼",
              "监察法", "村民委员会", "居民委员会", "信访"),
    "constitution": ("宪法",),
    "ip": ("知识产权", "专利", "商标", "著作权", "版权", "集成电路", "地理标志"),
    "transport": ("民用航空", "海商", "铁路", "公路", "港口", "航道", "邮政", "交通",
                  "船舶", "道路", "机动车"),
    "military": ("国防", "军事", "兵役", "军人"),
    "agriculture": ("渔业", "农业", "畜牧", "动物防疫", "种子", "农产品", "农村",
                    "土地承包", "森林", "草原"),
    "trade": ("对外贸易", "进出口", "海关", "外商投资", "关税", "贸易"),
    "safety": ("食品安全", "危险化学品", "安全生产", "消防", "突发事件", "特种设备",
               "矿山"),
    "culture": ("教育", "语言文字", "宣传", "文化", "文物", "出版", "教师", "学校"),
    "ethnic": ("民族", "民族团结", "宗教"),
    "social_security": ("社会救助", "公共卫生", "慈善", "红十字", "退役军人", "妇女",
                        "未成年人", "老年人", "残疾人"),
    "legislation": ("立法", "法律清理", "法制工作", "决定"),
    "judicature": ("仲裁", "律师", "法官", "检察官", "公证", "人民调解", "司法",
                   "法院", "检察院"),
    "economy": ("不正当竞争", "网络安全", "发展规划", "公司", "证券", "银行", "保险",
                "票据", "破产", "反垄断", "电子商务", "消费者", "招标", "价格",
                "产品质量"),
}

# 领域 → 知识库标题关键词（子串匹配 title）。
# knowledge 语料较小（办案指引/审查清单/文书模板），词表按 title 特征收敛。
_KNOWLEDGE_DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "labor": ("劳动", "劳动争议", "劳动仲裁", "工伤", "社保", "社会保险"),
    "tax": ("税",),
    "env": ("环境", "环保"),
    "civil": (
        "合同", "租赁", "买卖", "服务", "民事", "诉讼", "文书", "类案",
        "裁判", "答辩", "起诉", "委托", "担保", "侵权", "借贷", "债权",
        "继承", "婚姻", "证据", "执行", "审判",
    ),
    "criminal": ("刑事", "刑法", "犯罪", "辩护", "看守所"),
    "admin": ("行政", "治安", "处罚", "许可", "复议", "诉讼"),
    "constitution": ("宪法",),
    "ip": ("知识产权", "专利", "商标", "著作权"),
    "transport": ("交通", "运输", "船舶", "航空", "海事"),
    "military": ("军事", "国防"),
    "agriculture": ("农业", "渔业", "农村", "土地"),
    "trade": ("贸易", "进出口", "海关", "外贸"),
    "safety": ("安全", "食品", "生产", "消防"),
    "culture": ("教育", "文化", "学校", "出版"),
    "ethnic": ("民族",),
    "social_security": ("救助", "保障", "福利", "慈善", "养老"),
    "legislation": ("立法",),
    "judicature": ("仲裁", "律师", "调解", "司法", "法院"),
    "economy": ("公司", "证券", "银行", "合同", "破产", "竞争", "消费者"),
}


def statute_domain_keywords(domain: str) -> tuple[str, ...]:
    return _STATUTE_DOMAIN_KEYWORDS.get(domain or "", ())


def knowledge_domain_keywords(domain: str) -> tuple[str, ...]:
    return _KNOWLEDGE_DOMAIN_KEYWORDS.get(domain or "", ())


def _match_any(text: str, keywords: tuple[str, ...]) -> bool:
    t = text or ""
    return any(k in t for k in keywords)


def filter_statutes_by_domain(items: list[dict[str, Any]], domain: str) -> list[dict[str, Any]]:
    """按领域过滤法规命中；domain 为空 / unknown / 无词表时原样返回。

    过滤基准（命中任一即保留）：
    1. ``doc_name`` / ``source_path`` 命中该领域法规名关键词；
    2. ``source_path`` 的 ``data\\raw\\<目录>`` 目录映射等于该领域（词表兜底）。

    保证"劳动问题不召回环保税法 / 知识产权条例 / 海商法"这类跨领域错配，
    同时每个 ``data/raw`` 目录的法条都能归入正确领域。
    """
    kws = statute_domain_keywords(domain)
    if not kws:
        return list(items or [])
    return [
        i for i in (items or [])
        if _match_any(i.get("doc_name") or "", kws)
        or _match_any(i.get("source_path") or "", kws)
        or raw_dir_domain(i.get("source_path") or "") == domain
    ]


def filter_knowledge_by_domain(items: list[dict[str, Any]], domain: str) -> list[dict[str, Any]]:
    """按领域过滤知识库命中；domain 为空 / unknown / 无词表时原样返回。

    过滤基准是 ``title``（知识库条目的领域特征都在标题里：
    办案指引 / 审查清单 / 文书模板）。``doc_type`` 不参与 ——
    劳动仲裁申请书与民诉起诉状都是"文书模板"，拿 doc_type 过滤会互相污染。
    """
    kws = knowledge_domain_keywords(domain)
    if not kws:
        return list(items or [])
    return [i for i in (items or []) if _match_any(i.get("title") or "", kws)]


def filter_cases_by_domain(items: list[dict[str, Any]], domain: str) -> list[dict[str, Any]]:
    """按领域过滤类案命中；domain 为空 / unknown / 无词表时原样返回。

    类案元数据里没有独立的领域字段，可用的领域信号按优先级取：
    1. ``cause_of_action``（案由，如「劳动争议」「房屋租赁合同纠纷」）——
       结构化、最可靠：案由命中目标领域词表 → 保留；案由明确命中**其它**
       领域词表 → 滤除（就业歧视案的案由是「劳动争议」，属劳动领域，
       即使案名含「民族」也不是民族事务判例）；
    2. ``source_path`` 的 ``data\\raw\\<目录>`` 目录兜底；
    3. ``case_name`` 案名子串匹配仅作最后防线 —— 仅在案由为空时使用，
       避免「某公司民族身份就业歧视案」这类案名含领域词、案由却属他域的案例误留。

    注意：过滤后为空属正常 —— 该领域在类案库中确实没有判例时，
    如实报告「类案证据缺口」比召回跨领域错配判例更符合引用纪律。
    """
    kws = statute_domain_keywords(domain)
    if not kws:
        return list(items or [])
    out: list[dict[str, Any]] = []
    for i in (items or []):
        cause = str(i.get("cause_of_action") or "").strip()
        src = str(i.get("source_path") or "")
        if _match_any(cause, kws) or raw_dir_domain(src) == domain:
            out.append(i)
            continue
        if cause and _cause_in_other_domain(cause, domain):
            continue
        name = str(i.get("case_name") or "")
        if not cause and _match_any(name, kws):
            out.append(i)
    return out


def _cause_in_other_domain(cause: str, domain: str) -> bool:
    """案由是否明确命中目标领域**之外**的领域词表（如「劳动争议」命中劳动）。"""
    for d, kws in _STATUTE_DOMAIN_KEYWORDS.items():
        if not d or d == domain or d == "unknown":
            continue
        if _match_any(cause, kws):
            return True
    return False
