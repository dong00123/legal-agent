"""记忆脱敏器（Memory Sanitizer）。

方案 9.3 / 第十四章：**长期记忆与向量库仅存脱敏数据**，Memory Extractor 强制脱敏。
这是合规硬约束，不是"尽力而为"。设计原则：

1. **误伤可控，漏网不可接受，但不许破坏法条引用**。法律文本里"应当""甲方""劳动争议"
   这类词与姓氏首字高度重合（应/方/劳/同 都是姓氏字），粗暴的正则会把法条正文改烂，
   甚至把「上海市浦东新区人民法院」抹成「某地」导致引用失效。所以每条规则都带
   明确的**结构约束**（必须有门牌号、必须有角色标签等），宁少勿滥。
2. **全角半角两套标点都要覆盖**：``（）`` 与 ``()``、``：`` 与 ``:"`` 在中文法律文书里混用，
   只写半角会漏掉 ``原告：张三`` 这种最常见的写法 —— 而漏掉的是真名。
3. **残留检查**：正则脱敏永远有漏网，所以脱敏后再扫一遍"还像敏感信息的片段"并告警，
   把问题暴露出来而不是假装干净。

姓名识别的取舍（重要，改这里之前先读）：
中文姓名识别靠词表启发式，必然有误判。本模块采用**「角色标签 + 最短优先」**策略：
- 只处理紧跟角色标签（原告/被告/甲方/…）的姓名 —— 这是法律文书里姓名的绝大多数出现形式；
- 优先取 **2 字**候选（中文姓名以 2~3 字为主），命中黑名单则整体放弃而不是延长到 3 字。
  取 2 字会让「王小明」变成「王某明」，读起来略怪，但**身份已不可还原**，符合合规目标；
  延长到 3 字则会把「被告李四劳动争议」吃成「李某某某」，破坏正文。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("legal_agent.memory.sanitizer")


# --------------------------------------------------------------------------- 词表
SURNAME = (r"[赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
           r"戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐"
           r"费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄"
           r"和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁"
           r"杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍"
           r"虞万支柯昝管卢莫经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚"
           r"程嵇邢滑裴陆荣翁荀羊於惠甄曲家封芮羿储靳汲邴糜松井段富巫乌焦巴弓"
           r"牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙"
           r"叶幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴胥能苍双]")

ROLE_LABELS = ("原告", "被告", "第三人", "申请人", "被申请人", "上诉人", "被上诉人",
               "再审申请人", "甲方", "乙方", "丙方", "用人单位", "劳动者",
               "委托人", "代理人", "当事人", "被害人", "犯罪嫌疑人", "承租人", "出租人")

# 角色标签与姓名之间的分隔符（全角/半角标点、空格、括号）
NAME_SEP = r"[，,。；;、\s：:（）()]*"

# 姓氏字开头的常见法律词汇 —— 命中即判定"这不是姓名"
# 这些词如果被当姓名脱敏，会把法条正文改烂（如「甲方应当在…」→「甲方应某某在…」）
NON_NAME_WORDS = frozenset({
    # 应当/可以/有无/未/不/已 系
    "应当", "应以", "应予", "应负", "应纳", "应在", "应从", "应按", "应依", "应对",
    "可以", "可有", "有权", "有无", "无权", "无据", "无效", "无理", "无法",
    "未能", "未予", "未经", "未免", "不予", "不宜", "不得", "不需", "不满",
    "已经", "已将", "已由", "已为", "将要", "将予", "需知", "须知", "须经", "需求",
    # 时间/数量/程度
    "如下", "如何", "由此", "由于", "按时", "按期", "依照", "依据", "依法", "依约",
    "在案", "在场", "在编", "为了", "为其", "与其", "与否", "或者", "其他", "其中",
    "该案", "上述", "下列", "以下", "期间", "时效", "万元", "亿元", "余额", "金额",
    "单项", "单独", "单位", "经济", "经过", "程序", "阶段", "部分", "全部", "从而",
    "能力", "能够", "效力", "实效", "事实", "费用", "适用", "全部", "安全", "常见",
    "于是", "是否", "解决", "解除", "明确", "确认", "项目", "事项", "强制", "控制",
    "支持", "持有", "管理", "处理", "房屋", "出租", "包括", "概括", "共同", "同案",
    "合同", "书证", "证据", "案款", "款项", "劳动", "争议", "协议", "合并", "要求",
    "支付", "责任", "期限", "限额", "定金", "规定", "条理", "章节", "民事", "人民",
    "法院", "人员", "内容", "之一", "之后", "了解", "涉及", "起诉", "答辩", "判决",
    "裁定", "调解", "仲裁", "诉讼", "管辖", "违约", "赔偿", "损失", "利息", "本金",
    "租金", "押金", "标的", "履行", "签署", "签订", "变更", "转让", "解除权",
})

# 单字停用（出现在姓名第 2 位即判定不是姓名）——只收"绝不出现在名字里"的字
NON_NAME_CHARS = frozenset("当以有无未不已将需须知由按依在为与或的其该述列用")

RULES_NOTE = "完整规则清单见 Sanitizer.report()"


@dataclass
class Rule:
    name: str
    pattern: re.Pattern[str]
    repl: Callable[[re.Match[str]], str] | str
    note: str
    order: int = 100


# --------------------------------------------------------------------------- 替换函数
def _mask_id(m: re.Match[str]) -> str:
    s = m.group(0)
    return s[:4] + "*" * max(4, len(s) - 8) + s[-4:]


def _mask_phone(m: re.Match[str]) -> str:
    s = m.group(0)
    return s[:3] + "****" + s[-4:]


def _mask_bank(m: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    return digits[:4] + "*" * max(4, len(digits) - 8) + digits[-4:]


def _mask_email(m: re.Match[str]) -> str:
    local, _, domain = m.group(0).partition("@")
    return f"{local[0] if local else '*'}***@{domain}"


ORG_SUFFIX = ("有限公司", "股份有限公司", "有限责任公司", "合伙企业",
              "个体工商户", "律师事务所", "分公司")

_LABEL_PREFIX_RE = re.compile(
    "^(?:" + "|".join(sorted(ROLE_LABELS, key=len, reverse=True)) + ")+"
)


def _mask_company(m: re.Match[str]) -> str:
    """企业名称 → 「[角色标签]某某+组织形式」。

    为什么要剥掉开头的角色标签：``甲方北京华夏建筑工程有限公司`` 这个整体会被正则一次
    吃掉，如果把 ``甲方`` 也当成公司名的一部分，替换结果就成了 ``某某有限公司``，
    ``甲方/乙方`` 的对应关系（合同审查的核心信息）就丢了。
    """
    text = m.group(0)
    prefix = ""
    lm = _LABEL_PREFIX_RE.match(text)
    if lm:
        prefix = lm.group(0)
        text = text[lm.end():]
    suffix = next((s for s in ORG_SUFFIX if text.endswith(s)), "公司")
    return f"{prefix}某某{suffix}"


def _mask_address(m: re.Match[str]) -> str:
    return "某地"


def _mask_person(m: re.Match[str]) -> str:
    """角色标签后的姓名 → 保留姓氏 + 某。

    最短优先：先试 2 字，命中黑名单就整体放弃（而不是延长到 3 字，
    否则「被告李四劳动争议」会被吃成「李某某某」）。超过 2 字的余下部分原样保留。
    """
    label = m.group("label")
    sep = m.group("sep") or ""
    candidate = m.group("name")

    c2 = candidate[:2]
    if len(c2) < 2 or not _is_person_name(c2):
        return m.group(0)   # 不是姓名 → 原样返回，绝不误伤正文
    return f"{label}{sep}{c2[0]}{'某' * (len(c2) - 1)}{candidate[2:]}"


def _is_person_name(candidate: str) -> bool:
    """判定 2 字候选是否为姓名。

    只做"排除法"：命中黑名单词、或第 2 位是明确的非名字，就判定不是姓名。
    不做"包含法"（如要求必须在姓氏表内）—— 姓氏表已经通过正则约束了，
    这里再加正向条件只会引入新的漏网。
    """
    if len(candidate) < 2:
        return False
    if candidate in NON_NAME_WORDS:
        return False
    # 第 2 位是明确的非名字（当/以/有/无/未/不/已/将/需/须/如/由/按/依/在/为/与/或/的…）
    return candidate[1] not in NON_NAME_CHARS

def _mask_paren_person(m: re.Match[str]) -> str:
    """括号内姓名：``（张三，男，1980 年生）`` → ``（张某，男，1980 年生）``。"""
    inner = m.group("inner")
    c2 = inner[:2]
    if len(c2) < 2 or not _is_person_name(c2):
        return m.group(0)
    return (f"{m.group('open')}{c2[0]}{'某' * (len(c2) - 1)}"
            f"{inner[2:]}{m.group('rest')}{m.group('close')}")


# --------------------------------------------------------------------------- 规则表
RULES: list[Rule] = [
    Rule("身份证号", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), _mask_id,
         "18 位身份证号，保留前 4 后 4", 10),
    Rule("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), _mask_phone,
         "11 位手机号，保留前 3 后 4", 20),
    Rule("银行账号", re.compile(r"(?<!\d)\d{16,19}(?!\d)"), _mask_bank,
         "16~19 位数字串（银行卡/对公账号），保留前 4 后 4", 30),
    # 企业名称必须在姓名之前处理，否则「用人单位上海某某科技有限公司」会被姓名规则先吃掉
    Rule("企业名称",
         re.compile(r"[\u4e00-\u9fff（）()]{2,24}?(?:" + "|".join(ORG_SUFFIX) + r")"),
         _mask_company, "企业主体名称 → 「[标签]某某+组织形式」", 40),
    Rule("详细地址",
         # 结构约束：必须以门牌/房号类关键词收尾。
         # 这样「上海市浦东新区人民法院」不会命中（没有门牌号），避免抹掉法院名称导致引用失效。
         re.compile(r"[\u4e00-\u9fff]{2,15}?"
                    r"(?:省|市|区|县|镇|乡|街道|路|街|巷|道)"
                    r"[\u4e00-\u9fff]{0,15}?\d{1,5}\s*(?:号|弄|栋|单元|室|楼)"),
         _mask_address, "带门牌号的详细地址 → 「某地」", 50),
    Rule("角色标签姓名",
         re.compile(
             r"(?P<label>" + "|".join(sorted(ROLE_LABELS, key=len, reverse=True)) + r")"
             r"(?P<sep>" + NAME_SEP + r")"
             r"(?P<name>" + SURNAME + r"[\u4e00-\u9fff]{1,3})"
         ),
         _mask_person, "「原告：张三」类角色标签后的姓名（覆盖全角/半角标点与括号）", 60),
    Rule("括号内姓名",
         re.compile(
             r"(?P<open>[（(])(?P<inner>" + SURNAME + r"[\u4e00-\u9fff]{1,3})"
             r"(?P<rest>[^）)]{0,20})(?P<close>[）)])"
         ),
         _mask_paren_person, "「（张三，男，1980 年生）」括号内姓名，保留括号结构", 70),
    Rule("邮箱", re.compile(r"[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{2,63})+"),
         _mask_email, "邮箱保留首字符与域名", 80),
]


# --------------------------------------------------------------------------- 主入口
def sanitize(text: str, *, label: str = "") -> str:
    """强制脱敏（幂等：重复调用不再改动已脱敏内容）。"""
    if not text:
        return ""

    out = text
    for rule in sorted(RULES, key=lambda r: r.order):
        try:
            out = rule.pattern.sub(rule.repl, out)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001
            # 单条规则异常不能让整段文本以原文入库，但也不能中断其余规则
            logger.error("脱敏规则 %s 执行失败：%s；已跳过该规则", rule.name, exc)

    residue = find_residue(out)
    if residue:
        logger.warning("脱敏后仍发现疑似敏感片段（label=%s）：%s；请补充规则",
                       label or "-", "，".join(residue[:5]))
    return out


# 残留检查：脱敏后仍像敏感信息的片段。用「某」做排除，
# 否则已脱敏的「张某」会被反复告警，把真正的漏网淹掉。
RESIDUE_CHECKS: list[tuple[str, re.Pattern[str]]] = [
    ("疑似身份证", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("疑似手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("疑似长数字串", re.compile(r"(?<!\d)\d{15,}(?!\d)")),
    ("疑似未脱敏姓名", re.compile(
        r"(?:" + "|".join(ROLE_LABELS) + r")" + NAME_SEP +
        r"(" + SURNAME + r"[\u4e00-\u9fff]{1,2})(?![\u4e00-\u9fff]*某)"
    )),
]


def find_residue(text: str) -> list[str]:
    """检查脱敏后是否仍残留疑似敏感信息（用于质量把关，不抛异常）。

    **姓名的判定必须复用 ``_is_person_name``，不能自己再判一次。**
    曾经这里只看正则是否命中，于是「当事人陈述」「劳动者陈述其」被报成
    "疑似未脱敏姓名" —— 而脱敏规则那边（用 ``_is_person_name``）正确地
    把它们放了过去。两处判据不一致的表现是：**正文没被改坏，但告警一直响**。
    告警响多了就没人看了，而真正漏掉的真名会淹在里面。
    """
    hits: list[str] = []
    for name, pat in RESIDUE_CHECKS:
        for m in pat.finditer(text or ""):
            frag = m.group(0)[:30]
            if "某" in frag:
                continue
            if name == "疑似未脱敏姓名":
                # 与脱敏规则用同一把尺子：不是姓名就不算残留
                capture = m.group(1) if m.groups() else ""
                if len(capture) >= 2 and not _is_person_name(capture[:2]):
                    continue
            hits.append(f"{name}:{frag}")
    return hits


def sanitize_case_payload(payload: dict) -> dict:
    """对案件档案类 dict 逐字段脱敏（递归处理 str / list / dict）。"""
    def walk(v: Any) -> Any:
        if isinstance(v, str):
            return sanitize(v)
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v

    return {k: walk(v) for k, v in (payload or {}).items()}


class Sanitizer:
    """面向对象封装（便于注入自定义规则做测试）。"""

    def __init__(self, rules: list[Rule] | None = None) -> None:
        self.rules = rules if rules is not None else RULES

    def __call__(self, text: str, *, label: str = "") -> str:
        return sanitize(text, label=label)

    def payload(self, data: dict) -> dict:
        return sanitize_case_payload(data)

    def report(self) -> list[dict[str, Any]]:
        """规则清单（合规审查用，逐条可对照）。"""
        return [{"name": r.name, "pattern": r.pattern.pattern, "note": r.note, "order": r.order}
                for r in sorted(self.rules, key=lambda r: r.order)]

    def check(self, text: str) -> list[str]:
        return find_residue(text)


sanitizer = Sanitizer()
