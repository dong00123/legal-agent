"""法律文书结构化：条款拆分、法条拆分、主体与事实抽取。

这一层决定了 RAG 的粒度——法律场景要能"定位到第X条"，所以切分必须以
条文/条款为最小单位，而不是固定字数窗口。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from backend.parser.doc_type import STATUTE_NAME_HINT, guess_cause_of_action

# --------------------------------------------------------------------------- 中文数字
CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6,
             "七": 7, "八": 8, "九": 9, "十": 10, "百": 100, "千": 1000, "万": 10000}


def cn_to_int(text: str) -> int:
    """中文数字转阿拉伯数字（支持 一百二十三、一百零一、二十）。"""
    if not text:
        return 0
    if text.isdigit():
        return int(text)
    total, section, number = 0, 0, 0
    for ch in text:
        if ch not in CN_DIGITS:
            continue
        val = CN_DIGITS[ch]
        if val == 10:
            number = max(number, 1) * 10
            section += number
            number = 0
        elif val >= 100:
            number = max(number, 1) * val
            section += number
            number = 0
        else:
            number = val
    return total + section + number


_CN_DIGIT_CHARS = "零一二三四五六七八九"
_CN_UNITS = ("", "十", "百", "千")


def int_to_cn(n: int) -> str:
    """阿拉伯数字转中文数字（:func:`cn_to_int` 的逆运算）。

    **为什么必须成对存在**：法条正文里条文号一律是中文数字（「第五百八十五条」），
    而程序内部的比较键是阿拉伯形（``585``）。写检索式时如果把阿拉伯形直接丢进
    BM25，索引里根本不存在这个 token —— 检索会返回 **0 条**（不是"排得靠后"，
    是彻底空手而归），精确条文定位于是静默落空。

    规则按法律文本习惯：``10`` → 十（不是一十）、``11`` → 十一、``101`` → 一百零一、
    ``110`` → 一百一十、``1201`` → 一千二百零一。
    """
    if n <= 0:
        return ""
    if n < 10:
        return _CN_DIGIT_CHARS[n]

    text = ""
    zero_pending = False          # 上一个非零位之后出现过 0，需要补一个「零」
    s = str(n)
    length = len(s)
    for i, ch in enumerate(s):
        digit = int(ch)
        pos = length - i - 1      # 从右往左的位次
        if digit == 0:
            zero_pending = True
            continue
        if zero_pending and text:
            text += "零"
        zero_pending = False
        text += _CN_DIGIT_CHARS[digit] + _CN_UNITS[pos]
    # 「一十X」在法律文本里写作「十X」
    if text.startswith("一十"):
        text = text[1:]
    return text


def article_no_cn(article_no: str) -> str:
    """把任意写法的条文号还原成法规正文里的中文数字写法；无法还原返回空串。

    ``第585条`` / ``585`` / ``第五百八十五条`` → ``第五百八十五条``
    """
    key = article_key(article_no)
    if not key:
        return ""
    head, _, sub = key.partition("-")
    if not head.isdigit():
        return ""
    text = f"第{int_to_cn(int(head))}条"
    if sub:
        sub_num = sub.lstrip("0") or "0"
        text += f"之{int_to_cn(int(sub_num)) if sub_num.isdigit() else sub}"
    return text


# 条文号：第X条 / 第X条之一 / 第X条之二
ARTICLE_NO_RE = re.compile(r"第\s*([一二三四五六七八九十百零两\d]+)\s*条(?:\s*之\s*([一二三四五六七八九十]+))?")
# 裸数字形式：Validator 的正则捕获组拿到的就是「八十七」这种（已剥掉「第」「条」），
# 所以这里必须能单独处理裸数字，否则同一法条在两条调用路径上会算出不同的键。
BARE_NUM_RE = re.compile(r"^[一二三四五六七八九十百零两\d]+$")


def article_key(article_no: str) -> str:
    """条文号的**比较键**：中文数字转阿拉伯，统一成 ``87`` / ``87-2`` 形式。

    为什么必须做这个转换：法条原文写「第八十七条」，而模型输出常写「第87条」。
    此前两处规范化（``tools.statute_tools.normalize_article`` 与
    ``harness.validator._norm_article``）都只去空格不改数字，于是
    **一个正确的引用会因为数字写法不同被终审拦下来** —— 属于假阳性拦截，
    而假阳性比漏拦更糟：律师会开始不信任终审结论。

    **同时接受裸数字**（``八十七`` / ``87``）：Validator 的引用正则是
    ``第\\s*([一二三四五六七八九十百零两\\d]+)\\s*条``，捕获组给过来的就是裸数字。
    若只认带「第…条」的写法，同一法条在"证据池构建"与"正文引用"两条路径上
    会得到不同的键，终审会把所有引用都判成不可溯源。

    比较一律走本函数，展示才用 :func:`normalize_article_no`。
    """
    cleaned = (article_no or "").replace(" ", "").replace("　", "")
    m = ARTICLE_NO_RE.search(cleaned)
    if m:
        base = cn_to_int(m.group(1))
        if m.group(2):
            return f"{base}-{cn_to_int(m.group(2))}"
        return str(base)
    if BARE_NUM_RE.match(cleaned) and cleaned:
        return str(cn_to_int(cleaned))
    return ""


def normalize_article_no(article_no: str) -> str:
    """条文号的**展示规范形**：统一为阿拉伯数字的「第X条」。

    实测语料里的条文号有「第八十七条」「第 87 条」「第八十七条之一」「八十七」
    等多种写法，入库、检索、终审都统一到这里，避免同一法条出现多个比较键。
    """
    key = article_key(article_no)
    if not key:
        return (article_no or "").strip()
    if "-" in key:
        base, sub = key.split("-", 1)
        try:
            sub_cn = "一二三四五六七八九十"[int(sub) - 1]
        except (ValueError, IndexError):
            sub_cn = sub
        return f"第{base}条之{sub_cn}"
    return f"第{key}条"


# --------------------------------------------------------------------------- 模式
# 法条：第X条 / 第X条之一 / 第X条之二
ARTICLE_RE = re.compile(
    r"(?m)^[ \t　]*(第\s*[一二三四五六七八九十百零两\d]+\s*条(?:\s*之[一二三四五六七八九十]+)?)"
    r"([ \t　]*[（(][^）)\n]{0,40}[）)])?"
)
# 合同条款：第X条 / 一、 / （一） / 1.1
CLAUSE_PATTERNS = [
    re.compile(r"(?m)^[ \t　]*(第\s*[一二三四五六七八九十百零两\d]+\s*条[^\n]{0,30})$"),
    re.compile(r"(?m)^[ \t　]*([一二三四五六七八九十]+)[、.．][ \t　]*([^\n]{0,40})$"),
    re.compile(r"(?m)^[ \t　]*([（(][一二三四五六七八九十]+[）)])[ \t　]*([^\n]{0,40})$"),
    re.compile(r"(?m)^[ \t　]*(\d+(?:\.\d+){1,2})[ \t　]+([^\n]{0,40})$"),
]
# 裁判文书分段
LITIGATION_SECTIONS = [
    ("header", re.compile(r"^(?P<t>[\u4e00-\u9fa5]{2,12}(?:人民法院|仲裁委员会)\s*$|.*(?:民事判决书|民事裁定书|民事调解书|判决书|裁定书|调解书)\s*$)")),
    ("parties", re.compile(r"^(?P<t>(?:原告|被告|上诉人|被上诉人|申请人|被申请人|第三人|委托诉讼代理人)[：:])")),
    ("claim", re.compile(r"^(?P<t>.{0,20}(?:诉讼请求|上诉请求|请求事项)[：:]?)")),
    ("facts", re.compile(r"^(?P<t>.{0,20}(?:经审理查明|本院查明|一审法院查明)[：:]?)")),
    ("reasoning", re.compile(r"^(?P<t>.{0,20}(?:本院认为|本院经审理认为)[：:]?)")),
    ("judgment", re.compile(r"^(?P<t>.{0,20}(?:判决如下|裁定如下|裁判结果)[：:]?)")),
]

# 主体：兼容全角/半角冒号与括号
PARTY_RE = re.compile(
    r"(?:^|[\n，,；;。])[ \t　]*"
    r"(?P<role>甲方|乙方|丙方|丁方|原告|被告|上诉人|被上诉人|申请人|被申请人|第三人|"
    r"出租人|承租人|买受人|出卖人|用人单位|劳动者|委托人|受托人|借款人|出借人|保证人)"
    r"[ \t　]*(?:（(?P<nick>[^）)]{0,20})[）)])?[ \t　]*[：:][ \t　]*"
    r"(?P<name>[^\n，,；;。]{2,60})"
)

ORG_SUFFIX = ("有限公司", "有限责任公司", "股份有限公司", "公司", "合伙企业", "事务所", "中心", "研究院", "学校", "医院", "银行", "集团")

# 日期
DATE_RE = re.compile(r"(?P<y>(?:19|20)\d{2})\s*[年\-/.]\s*(?P<m>\d{1,2})\s*[月\-/.]\s*(?P<d>\d{1,2})\s*日?")
LAW_HEAD_RE = re.compile(r"《(?P<name>[^》]{2,60})》")
DOC_NO_RE = re.compile(r"(?P<no>(?:第\s*[一二三四五六七八九十百\d]+\s*号)|(?:[（(]\s*\d{4}\s*[）)]\s*第\s*\d+\s*号))")
# 发布机关：按优先级排序（越靠前越权威/越具体）
PUBLISHERS: list[tuple[str, int]] = [
    ("全国人民代表大会常务委员会", 9),
    ("国务院", 8),
    ("最高人民法院", 7),
    ("最高人民检察院", 7),
    ("人力资源和社会保障部", 6),
    ("国家知识产权局", 6),
    ("国家市场监督管理总局", 6),
    ("商务部", 6),
    ("住房和城乡建设部", 6),
    ("财政部", 6),
    ("教育部", 6),
    ("公安部", 6),
    ("司法部", 6),
    ("省人民代表大会常务委员会", 5),
    ("市人民代表大会常务委员会", 5),
    ("人民代表大会常务委员会", 5),
    ("人民政府", 2),
    ("人民代表大会", 2),
]
# 法规名结尾特征
LAW_NAME_TAIL = r"(?:法|条例|规定|办法|细则|解释|决定|规则|准则|规程)"
LAW_NAME_RE = re.compile(r"中华人民共和国[\u4e00-\u9fa5]{2,30}" + LAW_NAME_TAIL)


# --------------------------------------------------------------------------- 数据结构
@dataclass
class Clause:
    clause_no: str = ""
    clause_title: str = ""
    clause_text: str = ""
    index: int = -1
    page: int | None = None
    start: int = 0
    end: int = 0


@dataclass
class Article:
    article_no: str = ""
    heading: str = ""
    text: str = ""
    index: int = -1


@dataclass
class Party:
    name: str = ""
    role: str = ""
    kind: str = ""


@dataclass
class StatuteMeta:
    doc_name: str = ""
    doc_no: str = ""
    publisher: str = ""
    publish_date: date | None = None
    effective_date: date | None = None
    repealed_date: date | None = None
    status: str = "现行有效"
    level: str = ""
    category: str = ""
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- 法条拆分
def split_articles(text: str) -> list[Article]:
    """把法规正文按「第X条」拆成条文列表。"""
    if not text:
        return []
    matches = list(ARTICLE_RE.finditer(text))
    if not matches:
        return []
    articles: list[Article] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        article_no = re.sub(r"\s+", "", m.group(1))
        heading = (m.group(2) or "").strip("（）() \t　")
        # 去掉开头的「第X条」本身，剩下的作为条文正文
        body_wo_no = body[len(m.group(1)):].strip()
        if heading and body_wo_no.startswith(m.group(2) or "\0"):
            body_wo_no = body_wo_no[len(m.group(2)):].strip()
        articles.append(Article(article_no=article_no, heading=heading, text=body_wo_no, index=i))
    return articles


CHAPTER_LINE_RE = re.compile(r"^[ \t　]*第\s*[一二三四五六七八九十百零两\d]+\s*[章节][ \t　]*[^\n]{0,40}$")


def strip_toc(text: str) -> str:
    """剔除法规正文前的「目录」区块。

    目录里的「第一章 总则」「第一条 ……」会被下游误当成正文条文入库，
    导致检索命中目录、条款定位错位，所以入库前先剥掉。
    """
    if not text:
        return text
    idx = text.find("目录")
    if idx == -1 or idx > 3000:
        return text
    tail = text[idx:]
    m = re.search(r"(?m)^[ \t　]*第\s*[一二三四五六七八九十百零两\d]+\s*条", tail)
    if not m:
        return text
    cut = idx + m.start()
    # 保留「第一条」之前最近的章节标题行
    keep_from = cut
    for mm in CHAPTER_LINE_RE.finditer(text, idx, cut):
        keep_from = mm.start()
    return text[:idx] + text[keep_from:]


def _pick_law_name(text: str, filename: str) -> str:
    """法规名称判定顺序（踩过坑，顺序很重要）：

    1. 文件名里带「中华人民共和国XXX」→ 直接用（最可靠）；
    2. 文件名本身像法规名（含 法/条例/规定/办法/解释…）→ 用文件名；
       **必须在"正文首个《中华人民共和国…》"之前判定**，否则司法解释类文档
       会因为正文引用了《中华人民共和国劳动法》而被改名（实测踩过）；
    3. 正文**开头 120 字**内的「中华人民共和国XXX」→ 那是标题区；
    4. 兜底：正文第一个《》。
    """
    stem = re.sub(r"[_]?\d{6,8}(?:_\d+)?$", "", (filename or "").rsplit(".", 1)[0]).strip()
    head = (text or "")

    m = LAW_NAME_RE.search(stem)
    if m:
        return m.group(0)

    # 文件名是简写（如「劳动合同法.pdf」），正文开头有全称时补全
    if stem and f"中华人民共和国{stem}" in head[:400]:
        return f"中华人民共和国{stem}"

    if stem and len(stem) <= 40 and any(k in stem for k in STATUTE_NAME_HINT):
        return stem

    m = LAW_NAME_RE.search(head[:120])
    if m:
        return m.group(0)

    m = LAW_HEAD_RE.search(head[:400])
    if m:
        return m.group("name")
    return stem


_LOCAL_PUBLISHER_RE = re.compile(
    r"[\u4e00-\u9fa5]{2,10}(?:省|市|自治区|自治州|县|区)人民代表大会常务委员会"
)
_CENTRAL_PUBLISHERS: list[tuple[str, int]] = [
    ("全国人民代表大会常务委员会", 9),
    ("国务院", 8),
    ("最高人民法院", 7),
    ("最高人民检察院", 7),
    ("人力资源和社会保障部", 6),
    ("国家知识产权局", 6),
    ("国家市场监督管理总局", 6),
    ("商务部", 6),
    ("住房和城乡建设部", 6),
    ("财政部", 6),
    ("教育部", 6),
    ("公安部", 6),
    ("司法部", 6),
]


def _pick_publisher(head: str) -> str:
    """发布机关：只在文档前 800 字（真正的发布信息区）查找，避免命中正文引用。"""
    window = (head or "")[:800]
    m = _LOCAL_PUBLISHER_RE.search(window)
    if m:
        return m.group(0)
    hits: list[tuple[int, str]] = []
    for name, priority in _CENTRAL_PUBLISHERS:
        pos = window.find(name)
        if pos != -1:
            hits.append((pos - priority * 100, name))
    if hits:
        hits.sort()
        return hits[0][1]
    if "人民代表大会常务委员会" in window:
        return "人民代表大会常务委员会"
    return ""


def parse_statute_meta(text: str, filename: str = "") -> StatuteMeta:
    """从法规正文头部与文件名解析元数据（名称、文号、机关、日期）。"""
    meta = StatuteMeta()
    head = (text or "")[:3000]

    name = _pick_law_name(text, filename)
    meta.doc_name = name if name.startswith("《") else f"《{name}》"

    m = DOC_NO_RE.search(head)
    if m:
        meta.doc_no = m.group("no").replace(" ", "")

    meta.publisher = _pick_publisher(head)

    # 日期：文件名后缀日期最可靠，其次取正文头部前两个日期
    dates = list(DATE_RE.finditer(head))
    if len(dates) >= 1:
        meta.publish_date = _to_date(dates[0])
        meta.effective_date = _to_date(dates[1]) if len(dates) > 1 else meta.publish_date

    fm = re.search(r"(?:^|_)((?:19|20)\d{6})(?:_\d+)?$", (filename or "").rsplit(".", 1)[0])
    if fm:
        s = fm.group(1)
        try:
            d = date(int(s[:4]), int(s[4:6]), int(s[6:8]))
            meta.publish_date = d
            meta.effective_date = meta.effective_date or d
        except ValueError:
            pass

    plain = meta.doc_name.strip("《》")
    if "司法解释" in plain or "解释" in plain or "最高人民法院" in meta.publisher:
        meta.level = "司法解释"
    elif "条例" in plain or "规定" in plain or "办法" in plain or "细则" in plain:
        meta.level = "行政法规" if "国务院" in meta.publisher else "地方性法规"
    elif "法" in plain:
        meta.level = "法律"
    else:
        meta.level = "规范性文件"

    # 发布机关兜底：国家级法律/司法解释可按级别推定，避免元数据空缺
    if not meta.publisher:
        if meta.level == "法律":
            meta.publisher = "全国人民代表大会常务委员会"
        elif meta.level == "司法解释":
            meta.publisher = "最高人民法院"

    if any(k in plain for k in ("劳动", "工伤", "社保", "工资", "职工", "就业")):
        meta.category = "劳动法"
    elif any(k in plain for k in ("知识产权", "商标", "专利", "著作权")):
        meta.category = "知识产权法"
    elif "民法典" in plain:
        meta.category = "民法典"
    elif any(k in plain for k in ("民事诉讼法", "仲裁")):
        meta.category = "诉讼与仲裁"
    else:
        meta.category = "其他"
    return meta


def _to_date(m: re.Match) -> date | None:
    try:
        return date(int(m.group("y")), int(m.group("m")), int(m.group("d")))
    except (ValueError, TypeError):
        return None


def check_statute_status(meta: StatuteMeta, as_of: date | None = None) -> str:
    """依据生效/废止日期判定效力状态。"""
    as_of = as_of or date.today()
    if meta.repealed_date and meta.repealed_date <= as_of:
        return "已废止"
    if meta.effective_date and meta.effective_date > as_of:
        return "尚未生效"
    if meta.extra.get("amended"):
        return "已修订"
    return "现行有效"


# --------------------------------------------------------------------------- 合同条款
# 条款正文的「尾巴」：这些东西不属于本条，是切条时被顺带圈进来的。
# 每一类都是真机上撞到过的：
#   ① 页标 `<!-- 第 3 页 -->` 与孤立的页脚页码 `3`（text_extractor 每页都插）
#   ② 下一节的标题 `八、劳动争议处理` / `第三章 …`
#   ③ 落款签名区 `甲方：…` `盖章：` `2024 年 2 月 28 日`
# ②③ 的后果不只是"看着脏"：`revise_service.build_rewritten` 是**按条款区间替换**的，
# 末条（第二十五条）的区间一直拉到文件末尾，于是改写合同时**把整个落款签名区一起换掉了**
# —— 交付出去的合同没有签署页，是废纸。① 会让交付物正文中间冒出裸页码。
# ①：页标与页脚页码。**整行锚定**：`1. 乙方被查实在应聘时…` 这种分项行不能当页码，
# 区别就在"这一行除了数字没有别的东西"。
_TAIL_PAGE_RE = re.compile(
    r"(?m)^[ \t　]*(?:<!--\s*第\s*\d+\s*页\s*-->|\d{1,3})[ \t　]*$"
)
# ①b：行内嵌的页标（``…中断，<!-- 第 38 页 -->从中断…``）。整行锚定的
# ``_TAIL_PAGE_RE`` 匹配不到这种（前后都有正文），必须在截断前先做一次行内 sub。
# 同时是 statute 路径之外（contract/unknown/litigation）的统一收敛点。
_TAIL_PAGE_INLINE_RE = re.compile(r"<!--\s*第\s*\d+\s*页\s*-->")
# ③：落款签名区。**不锚行尾** —— 「甲方：上海具识智能科技有限公司 甲方代表人：陈志远」
# 冒号后面还有一长串，锚了行尾就只能匹配到光秃秃的「盖章：」那一行，
# 上面几行照样留在条款里（第一次就是这么错的）。
_TAIL_SIGN_RE = re.compile(
    r"(?m)^[ \t　]*(?:"
    r"(?:甲方|乙方|用人单位|劳动者|盖章|签字|法定代表人|委托代理人)\s*[：:]"
    r"|\d{4}\s*年[^\n]{0,12}月[^\n]{0,12}日[ \t　]*$"
    r")"
)
# ②：下一节的标题。**限长 30 字**：条款内部的「一、」分项如果是一整句话，会超长
# 而不会被误当成章节标题（「一、依照法律法规约定乙方应承担的赔偿责任；」不会命中）。
# 短分项（「一、工资」）与章节标题天然无法区分 —— 合同的分项惯例是 1./（一），
# 顶层章节才是 一、，这里按惯例判。
_TAIL_HEAD_RE = re.compile(
    r"(?m)^[ \t　]*(?:[一二三四五六七八九十]{1,3}|第\s*[0-9一二三四五六七八九十百零两]{1,4})"
    r"\s*[、章节][^\n]{0,30}$"
)


def _trim_clause_tail(body: str) -> str:
    """把条款正文收紧到"这一条"为止（见 :data:`_TAIL_CUT_RE`）。

    ⚠ 命中位置**必须在 body 内部**才算数（``m.start() > 0``）。``split_articles``
    传进来的 ``a.text`` 已经剥掉了本条自己的「第X条」，但正文第一行仍可能以
    「甲方：」这类字样开头（比如"甲方：指…"的定义条），锚在 0 处的命中一旦当作
    截断点，整条就被清空了 —— ``revise_service._tighten`` 里是同一个写法。

    只在后面还有内容时截断；截不到就原样返回，**绝不猜**。
    """
    body = body or ""
    # 先做行内页标剥离（嵌在句中的页标，_TAIL_PAGE_RE 整行锚定清不到）
    body = _TAIL_PAGE_INLINE_RE.sub("", body)
    cuts = [m.start() for m in (_TAIL_PAGE_RE.search(body), _TAIL_HEAD_RE.search(body),
                               _TAIL_SIGN_RE.search(body)) if m]
    cut = min(cuts) if cuts else 0
    return body[:cut].rstrip() if cut > 0 else body


def split_clauses(text: str) -> list[Clause]:
    """拆分条款。

    优先级：``第X条``（法规与合同通用，且是法律场景最小定位单位）→
    ``一、`` / ``（一）`` / ``1.1`` → 自然段兜底。

    注意：PDF 抽取出的文本里「第X条」往往与正文同处一行，所以匹配不能要求行尾结束，
    否则会漏掉绝大多数条文（实测劳动合同法只切出 18 条的元凶就在这里）。
    """
    if not text:
        return []
    # 行内页标统一剥离：覆盖「其他条款标记」「自然段」等不经过
    # ``_trim_clause_tail`` 的分支（页标只影响正文，不影响条款锚点定位）。
    text = _TAIL_PAGE_INLINE_RE.sub("", text)

    # 1) 条文优先
    # ⚠ ``split_articles`` 的末条区间是"到文件末尾"（法规这么切没问题 —— 末条后面
    # 没有别的东西）。合同不一样：末条后面是落款签名区，中间各条后面还可能跟着
    # 下一节的标题与页标。所以**在合同这条路上**再收一次口（法规那条线直接用
    # ``split_articles``，见 pipeline.py 的 statute 分支，不受这里影响）。
    articles = split_articles(text)
    if len(articles) >= 2:
        return [
            Clause(
                clause_no=a.article_no,
                clause_title=a.heading,
                clause_text=_trim_clause_tail(a.text)[:4000],
                index=a.index,
            )
            for a in articles
        ]

    # 2) 其他条款标记
    marks: list[tuple[int, str, str]] = []
    seen_pos: set[int] = set()
    for pat in CLAUSE_PATTERNS:
        for m in pat.finditer(text):
            if m.start() in seen_pos:
                continue
            groups = [g for g in m.groups() if g]
            if not groups:
                continue
            seen_pos.add(m.start())
            label = re.sub(r"\s+", "", groups[0])
            title = (groups[1] if len(groups) > 1 else "").strip()
            marks.append((m.start(), label, title))

    if not marks:
        # 没有可识别条款标记：按自然段切
        paras = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
        return [
            Clause(clause_no=f"段落{i + 1}", clause_text=p[:2000], index=i, start=0, end=0)
            for i, p in enumerate(paras[:200])
        ]

    marks.sort(key=lambda x: x[0])
    clauses: list[Clause] = []
    for i, (pos, label, title) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        body = text[pos:end].strip()
        clauses.append(
            Clause(
                clause_no=label,
                clause_title=title,
                clause_text=body[:4000],
                index=i,
                start=pos,
                end=end,
            )
        )
    return clauses


def split_litigation_sections(text: str) -> dict[str, str]:
    """裁判文书按「首部 / 当事人 / 诉请 / 事实 / 理由 / 主文」分段。"""
    sections: dict[str, list[str]] = {"header": [], "parties": [], "claim": [], "facts": [], "reasoning": [], "judgment": []}
    current = "header"
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for name, pat in LITIGATION_SECTIONS:
            if pat.match(stripped):
                current = name
                break
        sections[current].append(stripped)
    return {k: "\n".join(v) for k, v in sections.items()}


# --------------------------------------------------------------------------- 主体与事实
def extract_parties(text: str) -> list[Party]:
    """抽取主体信息。正则同时覆盖全角（）与半角()、全角：与半角:。"""
    out: list[Party] = []
    seen: set[tuple[str, str]] = set()
    for m in PARTY_RE.finditer(text or ""):
        role = m.group("role")
        name = (m.group("name") or "").strip(" 　:：")
        if not name:
            continue
        name = re.split(r"[，,；;]", name)[0].strip()
        kind = _guess_kind(name, role)
        key = (role, name)
        if key in seen:
            continue
        seen.add(key)
        out.append(Party(name=name, role=role, kind=kind))
    return out


def _guess_kind(name: str, role: str) -> str:
    if any(s in name for s in ORG_SUFFIX):
        return "法人"
    if role in ("用人单位", "出租人", "委托人") and len(name) > 6:
        return "法人"
    if re.search(r"[元,，、]", name) and any(s in name for s in ORG_SUFFIX):
        return "法人"
    if 2 <= len(name) <= 4 and re.fullmatch(r"[\u4e00-\u9fa5·]{2,4}", name):
        return "自然人"
    return ""


FACT_HINTS = ("经审理查明", "本院查明", "事实如下", "约定", "签订", "支付", "拖欠", "解除",
              "离职", "入职", "违约", "交付", "验收", "通知", "协商")


def extract_facts(text: str, limit: int = 40) -> list[dict]:
    """抽取事实句（规则版）。用于降级路径；正常路径由 LLM 抽取。"""
    facts: list[dict] = []
    for para in re.split(r"\n{1,}", text or ""):
        para = para.strip()
        if len(para) < 12:
            continue
        if not any(h in para for h in FACT_HINTS):
            continue
        for sent in re.split(r"(?<=[。；;])", para):
            sent = sent.strip()
            if len(sent) < 12 or not any(h in sent for h in FACT_HINTS):
                continue
            facts.append({"text": sent[:500], "source": "", "page": None})
            if len(facts) >= limit:
                return facts
    return facts


def extract_elements(text: str, filename: str = "") -> dict:
    """结构化要素抽取总入口（供 Intake Agent 的降级路径使用）。"""
    from backend.parser.doc_type import judge_doc_type

    dt = judge_doc_type(text, filename)
    clauses = split_clauses(text) if dt.doc_type in ("contract", "unknown") else []
    parties = extract_parties(text)
    facts = extract_facts(text)
    result: dict = {
        "doc_type": dt.doc_type,
        "doc_type_confidence": dt.confidence,
        "cause_of_action": guess_cause_of_action(text),
        "parties": [p.__dict__ for p in parties],
        "facts": facts,
        "clauses": [
            {
                "clause_no": c.clause_no,
                "clause_title": c.clause_title,
                "clause_text": c.clause_text,
                "index": c.index,
            }
            for c in clauses
        ],
        "litigation_sections": split_litigation_sections(text) if dt.doc_type == "litigation" else {},
    }
    return result
