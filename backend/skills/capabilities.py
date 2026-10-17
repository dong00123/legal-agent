"""Agent 可安装的原子 Skill 注册表。

这里的 Skill 与 ``backend.skills.base.SkillSpec``（办案 Playbook）刻意分开：
前者是一项可调用动作，严格一对一映射真实 Tool；后者是一组带完成判据的作业步骤。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.tools.registry import TOOL_REGISTRY


CATEGORY_NAMES = {
    "statute": "法规检索",
    "case": "类案研究",
    "material": "材料处理",
    "document": "法律文书",
    "knowledge": "知识库",
    "calc": "法律计算",
    "business": "业务协作",
}


@dataclass(frozen=True)
class CapabilitySkill:
    id: str
    name: str
    description: str
    category: str
    tool: str
    dependencies: tuple[str, ...] = ()
    featured: bool = False
    risk_level: str = "low"
    tags: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        spec = TOOL_REGISTRY[self.tool]
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "category_name": CATEGORY_NAMES.get(self.category, self.category),
            "tool": self.tool,
            "dependencies": list(self.dependencies),
            "featured": self.featured,
            "risk_level": self.risk_level,
            "read_only": bool(spec.idempotent),
            "requires_audit": bool(spec.requires_audit),
            "params": spec.param_list(),
            "tags": list(self.tags),
        }


CAPABILITY_SKILLS: dict[str, CapabilitySkill] = {
    "statute_search": CapabilitySkill(
        "statute_search", "法条检索", "按关键词、条文号和效力层级检索法规，并校验效力状态。",
        "statute", "statute_search", ("statute_validity_check",), True,
        tags=("法条原文", "效力校验"),
    ),
    "statute_version_compare": CapabilitySkill(
        "statute_version_compare", "法规版本比对", "比较法规新旧版本，定位新增、删除和修改条文。",
        "statute", "statute_version_compare", featured=True, tags=("新旧版本", "条文差异"),
    ),
    "case_search": CapabilitySkill(
        "case_search", "类案检索", "按案由、争议焦点和法院层级检索类案。",
        "case", "case_search", ("case_no_verify",), True, tags=("裁判要旨", "案号核验"),
    ),
    "case_summarize": CapabilitySkill(
        "case_summarize", "判例摘要", "从判例全文提取争议焦点、裁判理由、要旨和结果。",
        "case", "case_summarize", featured=True, tags=("要旨提取", "结构化"),
    ),
    "material_parse": CapabilitySkill(
        "material_parse", "材料解析", "解析合同和诉讼材料，提取条款、主体、事实与表格。",
        "material", "material_parse", featured=True, tags=("要素提取", "条款拆分"),
    ),
    "material_ocr": CapabilitySkill(
        "material_ocr", "卷宗 OCR", "识别扫描件与图片材料，并回插法律文书要素。",
        "material", "material_ocr", featured=True, tags=("扫描件", "OCR"),
    ),
    "material_cross_compare": CapabilitySkill(
        "material_cross_compare", "多文档比对", "比较合同、补充协议与函件中的主体、金额、日期和期限。",
        "material", "material_cross_compare", featured=True, tags=("一致性", "差异定位"),
    ),
    "knowledge_search": CapabilitySkill(
        "knowledge_search", "本所知识检索", "检索办案指引、审查清单、合同模板和文书规范。",
        "knowledge", "knowledge_search", featured=True, tags=("办案指引", "版本标注"),
    ),
    "damages_calc": CapabilitySkill(
        "damages_calc", "法定金额测算", "确定性计算经济补偿、违法解除赔偿、代通知金和加班费。",
        "calc", "damages_calc", featured=True, tags=("N/2N", "确定性计算"),
    ),
    "draft_generate": CapabilitySkill(
        "draft_generate", "法律文书生成", "基于已核验事实和引用生成起诉状、答辩状与法律意见草稿。",
        "document", "draft_generate", featured=True, tags=("起诉状", "答辩状"),
    ),
    "revision_draft_generate": CapabilitySkill(
        "revision_draft_generate", "合同修改对照稿", "根据风险点生成原文、修改后和修改理由三列对照稿。",
        "document", "revision_draft_generate", featured=True, tags=("合同修订", "三列对照"),
    ),
    "format_normalize": CapabilitySkill(
        "format_normalize", "文书格式规范化", "套用律所格式规范处理标题、正文、落款和日期。",
        "document", "format_normalize", tags=("律所模板", "格式统一"),
    ),
    "review_checklist_export": CapabilitySkill(
        "review_checklist_export", "审查清单导出", "将审查结果生成可交付的 Markdown 或 HTML 文件。",
        "business", "review_checklist_export", risk_level="medium", tags=("结果导出", "写入文件"),
    ),
}


CORE_AGENT_SKILLS = ("statute_search", "case_search", "knowledge_search")


def list_capability_skills() -> list[dict[str, Any]]:
    return [skill.to_dict() for skill in CAPABILITY_SKILLS.values()]


def resolve_tools(skill_ids: tuple[str, ...] | list[str] | set[str]) -> tuple[str, ...]:
    tools: list[str] = []
    for skill_id in skill_ids:
        skill = CAPABILITY_SKILLS.get(skill_id)
        if skill is None:
            continue
        for name in (skill.tool, *skill.dependencies):
            if name not in tools:
                tools.append(name)
    return tuple(tools)


def capability_health() -> dict[str, Any]:
    missing = {
        skill.id: [name for name in (skill.tool, *skill.dependencies) if name not in TOOL_REGISTRY]
        for skill in CAPABILITY_SKILLS.values()
    }
    missing = {key: value for key, value in missing.items() if value}
    return {"ok": not missing, "skills": len(CAPABILITY_SKILLS), "missing_tools": missing}


__all__ = [
    "CAPABILITY_SKILLS", "CORE_AGENT_SKILLS", "CapabilitySkill", "capability_health",
    "list_capability_skills", "resolve_tools",
]
