"""文书类型判定。

法律文档不是普通文本：合同、裁判文书、证据材料、法律意见书的结构完全不同，
后续结构化和 Chunk 策略要按类型定制，所以先判类型。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 各类型的强特征词（命中权重不同）
SIGNATURES: dict[str, list[tuple[str, float]]] = {
    "contract": [
        ("甲方", 2.0), ("乙方", 2.0), ("丙方", 1.5), ("本合同", 2.0), ("协议", 1.0),
        ("出租人", 2.0), ("承租人", 2.0), ("买受人", 2.0), ("出卖人", 2.0),
        ("用人单位", 2.0), ("劳动者", 2.0), ("违约金", 1.0), ("签订地点", 1.5),
        ("合同编号", 1.5), ("履行期限", 1.0),
    ],
    "litigation": [
        ("民事判决书", 3.0), ("民事裁定书", 3.0), ("民事调解书", 3.0), ("起诉状", 2.5),
        ("答辩状", 2.5), ("上诉人", 2.0), ("被上诉人", 2.0), ("原告", 2.0), ("被告", 2.0),
        ("本院认为", 3.0), ("判决如下", 3.0), ("审判长", 2.5), ("书记员", 2.0),
        ("案号", 1.5), ("诉讼请求", 2.0), ("开庭审理", 1.5),
    ],
    "opinion": [
        ("法律意见书", 3.0), ("律师意见", 2.5), ("本所律师", 2.5), ("出具本法律意见", 3.0),
        ("尽调", 1.0), ("核查", 1.0),
    ],
    "evidence": [
        ("证据清单", 3.0), ("证据名称", 2.0), ("证明目的", 2.5), ("质证意见", 2.0),
        ("附件", 1.0), ("扫描件", 1.0),
    ],
    "consult": [
        ("咨询", 1.5), ("请问", 1.0), ("如何", 0.8), ("是否合法", 1.5), ("怎么办", 1.0),
    ],
    # 法规条文本身（法规库入库用，区别于当事人之间的合同）
    "statute": [
        ("本法", 2.5), ("本条例", 2.5), ("本规定", 2.0), ("本细则", 2.0), ("本办法", 2.0),
        ("中华人民共和国主席令", 3.0), ("国务院令", 3.0), ("附则", 2.0), ("总则", 1.2),
        ("自公布之日起施行", 2.5), ("自发布之日起施行", 2.5), ("施行", 1.0),
        ("目录", 1.0), ("第一章", 1.2), ("全国人民代表大会常务委员会", 2.0),
    ],
}

# 以「第X条」开头的条文行
_ARTICLE_LINE = re.compile(r"(?m)^[ \t　]*第\s*[一二三四五六七八九十百零两\d]+\s*条")
# 法规文件名特征
STATUTE_NAME_HINT = ("法", "条例", "规定", "办法", "细则", "解释", "意见", "决定", "规则")


@dataclass
class DocTypeResult:
    doc_type: str
    confidence: float
    scores: dict[str, float]
    reason: str = ""


def _normalize(text: str, limit: int = 8000) -> str:
    return (text or "")[:limit]


def judge_doc_type(text: str, filename: str = "") -> DocTypeResult:
    """基于特征词打分判定文档类型。"""
    head = _normalize(text)
    name = filename or ""
    scores: dict[str, float] = {k: 0.0 for k in SIGNATURES}

    for dtype, sigs in SIGNATURES.items():
        for word, weight in sigs:
            cnt = head.count(word)
            if cnt:
                # 出现次数带边际递减，避免长文档里"原告"刷分
                scores[dtype] += weight * (1 + min(cnt, 5) * 0.2)

    # 文件名权重
    for dtype, keys in (
        ("contract", ("合同", "协议")),
        ("litigation", ("起诉状", "答辩状", "判决", "裁定", "仲裁申请书")),
        ("opinion", ("意见书", "法律意见")),
        ("evidence", ("证据", "清单")),
        ("statute", ("法", "条例", "规定", "办法", "细则", "司法解释")),
    ):
        if any(k in name for k in keys):
            scores[dtype] += 3.0

    # 条文密度特征：大量「第X条」且没有合同主体 → 法规
    article_lines = len(_ARTICLE_LINE.findall(head))
    has_contract_party = ("甲方" in head) or ("乙方" in head)
    if article_lines >= 5 and not has_contract_party:
        scores["statute"] += min(article_lines, 40) * 0.5
    if article_lines >= 3 and ("中华人民共和国" in head or "人民代表大会常务委员会" in head):
        scores["statute"] += 3.0
    # 反向修正：有合同主体时不应判为法规
    if has_contract_party:
        scores["statute"] *= 0.15

    best = max(scores, key=lambda k: scores[k])
    total = sum(scores.values()) or 1.0
    confidence = round(scores[best] / total, 3)
    if scores[best] < 2.0:
        return DocTypeResult("unknown", 0.3, scores, "特征词命中不足，判定为未知类型")
    return DocTypeResult(best, confidence, scores, f"特征词得分最高：{best}={scores[best]:.1f}")


# --------------------------------------------------------------------------- 案由
CAUSE_PATTERNS: list[tuple[str, str]] = [
    (r"劳动合同|劳动关系|用人单位|劳动者|解除劳动|工伤|工资|社保|经济补偿", "劳动争议"),
    (r"房屋租赁|租赁合同|承租人|出租人|租金", "房屋租赁合同纠纷"),
    (r"买卖合同|货款|买受人|出卖人|供货", "买卖合同纠纷"),
    (r"借款|民间借贷|本金|利息|还款", "民间借贷纠纷"),
    (r"服务合同|技术服务|咨询服务", "服务合同纠纷"),
    (r"建设工程|施工合同|工程款", "建设工程施工合同纠纷"),
    (r"股权|股东|出资|公司章程", "与公司有关的纠纷"),
    (r"知识产权|商标|专利|著作权|侵权", "知识产权权属、侵权纠纷"),
    (r"离婚|抚养|夫妻共同财产", "婚姻家庭纠纷"),
    (r"继承|遗嘱|遗产", "继承纠纷"),
]


def guess_cause_of_action(text: str) -> str:
    """从文本推测案由（供类案检索构造检索式）。"""
    head = _normalize(text, 4000)
    best, hits = "", 0
    for pattern, cause in CAUSE_PATTERNS:
        cnt = len(re.findall(pattern, head))
        if cnt > hits:
            best, hits = cause, cnt
    return best or "其他民事纠纷"
