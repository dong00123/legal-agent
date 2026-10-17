"""多模态识别的 Prompt 模板。

边界（方案 7.3）：VLM 用于**文档理解与要素提取**，不对外出具「印章真伪鉴定」类结论——
鉴定事项有法定资质要求，AI 只做信息提取与结构化。
"""

from __future__ import annotations

COMMON_HEAD = """你在协助律师处理法律文档中的图像/扫描件。请只做「信息提取与结构化」。
严禁给出任何鉴定性结论（例如判断印章真假、判断签名是否本人所签）。
看不清就写"无法辨认"，不要猜测。
"""

PROMPTS: dict[str, str] = {
    "business_license": COMMON_HEAD + """
这是一张营业执照或主体资格证明。请提取：
公司名称、统一社会信用代码、类型（有限责任公司/股份有限公司/个体工商户）、
法定代表人、注册资本、成立日期、住所、经营范围（摘要）。

输出 JSON：{"doc_kind": "营业执照", "company_name": "", "credit_code": "",
"company_type": "", "legal_rep": "", "registered_capital": "", "established": "",
"address": "", "business_scope": "", "confidence": 0.0}
""",
    "seal_or_signature": COMMON_HEAD + """
这是一处印章或签字区域。请只描述客观可见信息：
有无印章、印章上的文字内容（如"XX有限公司"）、印章位置、是否为骑缝章、
有无手写签名及其姓名（如可辨认）。

输出 JSON：{"doc_kind": "印章/签字页", "has_seal": true, "seal_text": "",
"seal_position": "", "is_cross_page": false, "has_signature": false,
"signature_name": "", "note": "", "confidence": 0.0}
""",
    "handwriting": COMMON_HEAD + """
这是正文中的手写批注。请识别手写内容，并说明它与哪一段正文相关。

输出 JSON：{"doc_kind": "手写批注", "handwriting": "", "related_clause": "",
"meaning": "", "confidence": 0.0}
""",
    "structure_diagram": COMMON_HEAD + """
这是一个结构图（股权结构 / 交易结构 / 组织架构）。请提取主体与关系。

输出 JSON：{"doc_kind": "结构图", "nodes": [{"name": "", "role": ""}],
"relations": [{"from": "", "to": "", "type": ""}], "summary": "", "confidence": 0.0}
""",
    "screenshot": COMMON_HEAD + """
这是证据材料中的截图。请说明它呈现的是什么系统/界面、显示了哪些关键信息
（时间、金额、账号、状态等，注意脱敏处理账号中的中间位）。

输出 JSON：{"doc_kind": "截图", "platform": "", "key_info": [], "summary": "", "confidence": 0.0}
""",
    "generic": COMMON_HEAD + """
请说明这张图是什么（照片/扫描件/示意图/表格截图/图表），以及其中包含的关键信息。

输出 JSON：{"doc_kind": "", "summary": "", "key_info": [], "confidence": 0.0}
""",
    # 整页合同/文书扫描件：要的是**全文转写**，不是概括 —— 合同审查要靠逐条正文定位风险。
    "document_scan": COMMON_HEAD + """
这是一份合同 / 法律文书的扫描件或照片。请**逐字转写**页面上的全部正文：
- 保留条款编号（第X条 / 1. / (一) …）、段落顺序与换行；
- 表格按行用「 | 」拼接单元格；
- 印章、签字、手写处只客观标注（如「[盖章]」「[手写：xxx]」）；
- 看不清的字用「□」占位，**不要猜测、不要总结、不要评论、不要输出解释**。

输出 JSON：{"doc_kind": "扫描件", "raw_text": "", "confidence": 0.0}
""",
}

# 按文件名/上下文关键词选择 prompt
KEYWORD_ROUTING: list[tuple[tuple[str, ...], str]] = [
    (("营业执照", "执照", "主体资格"), "business_license"),
    (("公章", "印章", "骑缝", "签字", "盖章", "签名"), "seal_or_signature"),
    (("批注", "手写", "手签", "备注"), "handwriting"),
    (("结构", "股权", "架构", "关系图", "流程图"), "structure_diagram"),
    (("截图", "聊天", "转账", "记录", "页面"), "screenshot"),
    (("合同", "协议", "扫描", "文书", "条款", "合同材料"), "document_scan"),
]


def pick_prompt(hint: str = "") -> tuple[str, str]:
    """根据提示词/文件名选择对应 prompt，返回 (类别, prompt)。"""
    h = hint or ""
    for keys, kind in KEYWORD_ROUTING:
        if any(k in h for k in keys):
            return kind, PROMPTS[kind]
    return "generic", PROMPTS["generic"]
