"""案件与材料接口（方案第九章长期记忆 + 第十二章权限隔离 + 第十四章数据隔离）。

**案件隔离的落点就在这个文件。** 每个读写案件/材料的接口都必须把登录用户传给
服务层（``case_service`` 的 ``viewer`` 参数），由 ``security.can_access_case``
统一判定。服务层对"没传主体"是**拒绝**而不是放行 —— 所以这里漏传会得到 403，
而不是静默地全量暴露（见 ``case_service.assert_access`` 的说明）。

上传接口的两个约束：

1. **先落盘再登记，且大小受限。** 直接 ``await file.read()`` 会把整个文件读进内存；
   几百 MB 的卷宗足以打满内存。这里是分块写盘的。
2. **文件名要净化后再用。** 上传文件名可能带路径穿越（``../../etc/passwd``）、
   重名、超长。存储名由 ``case_service.safe_filename`` + 内容哈希构成，
   原文件名只作为展示名入库。
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field, field_validator

from backend.app.api.deps import (
    Meta, Page, RequireUser, apply_page_params, effective_page, page_of,
)
from backend.app.config import settings
from backend.app.services import case_service

logger = logging.getLogger("legal_agent.api.cases")

router = APIRouter(tags=["案件与材料"])

CHUNK = 1 << 20  # 1 MiB


def _guard(exc: Exception) -> HTTPException:
    """把服务层异常转成 HTTP。三个映射一一对应，不做兜底 ——
    ``CaseError`` 之外的异常应该冒到全局处理器并留下堆栈，而不是被吞成 400。"""
    if isinstance(exc, case_service.CaseNotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, case_service.CaseForbidden):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


# --------------------------------------------------------------------------- 模型
class CaseCreate(BaseModel):
    case_id: str = Field(..., min_length=1, max_length=48, description="案件编号")
    title: str = Field("", max_length=256)
    case_type: str = Field("litigation", description="litigation / contract / consult")
    cause_of_action: str = Field("", max_length=128, description="案由")
    client_name: str = Field("", max_length=128, description="委托人（内部会脱敏）")
    opponent_name: str = Field("", max_length=128)
    stage: str = Field("intake")
    summary: str = ""
    tags: list[str] = Field(default_factory=list)


class CaseUpdate(BaseModel):
    title: str | None = None
    case_type: str | None = None
    cause_of_action: str | None = None
    client_name: str | None = None
    opponent_name: str | None = None
    stage: str | None = None
    status: str | None = None
    summary: str | None = None
    outcome: str | None = None
    dispute_issues: list[dict[str, Any]] | None = None
    tags: list[str] | None = None


class ProgressCreate(BaseModel):
    stage: str = Field(..., description="阶段：" + " / ".join(case_service.STAGES))
    note: str = ""
    operator: str = ""
    task_id: str = ""
    result: str = ""

    @field_validator("stage")
    @classmethod
    def _known_stage(cls, v: str) -> str:
        """stage 必须是 ``case_service.STAGES`` 里的值。

        以前这里是自由字符串，而 ``add_progress`` 只在 ``stage in STAGES`` 时才动
        ``case.stage`` —— 于是传一个非法值会**静默地只追加流水、阶段纹丝不动**：
        界面提示"已记录"、用户以为推进成功了，其实没有。这正是脏数据的入口
        （库里那两行中文 stage「分析完成」就是这么来的）。宁可 422 报错。
        """
        v = (v or "").strip()
        if v not in case_service.STAGES:
            raise ValueError(
                "未知阶段「%s」；合法值：%s" % (v, " / ".join(case_service.STAGES))
            )
        return v


# --------------------------------------------------------------------------- 案件
@router.get("/cases", summary="案件列表")
def list_cases(
    user: RequireUser,
    page: Page,
    page_number: Annotated[int | None, Query(alias="page", ge=1, description="页码（从 1 起）")] = None,
    page_size: Annotated[int | None, Query(ge=1, le=200, description="每页条数")] = None,
    keyword: Annotated[str, Query(description="按名称/编号/案由/委托人模糊匹配")] = "",
    case_type: Annotated[str, Query()] = "",
    case_status: Annotated[str, Query(alias="status")] = "",
    stage: Annotated[str, Query()] = "",
) -> dict[str, Any]:
    """案件列表。非管理员**在 SQL 上就加了归属条件**，不是取回来再过滤。"""
    limit, offset = apply_page_params(page, page_number, page_size)
    out = case_service.list_cases(user, keyword=keyword, case_type=case_type,
                                  status=case_status, stage=stage,
                                  limit=limit, offset=offset, count_total=True)
    return page_of(out["items"], effective_page(page, limit, offset), total=out["total"])


@router.post("/cases", summary="新建案件", status_code=status.HTTP_201_CREATED)
def create_case(payload: CaseCreate, user: RequireUser, meta: Meta) -> dict[str, Any]:
    """新建案件。归属默认是**创建者本人 + 本人团队**，不允许通过入参指定别人 ——
    否则任何人都能把案件挂到别人名下（或挂到别的团队，从而让自己看得到）。"""
    try:
        return case_service.create_case(
            payload.case_id, title=payload.title, case_type=payload.case_type,
            cause_of_action=payload.cause_of_action, client_name=payload.client_name,
            opponent_name=payload.opponent_name, stage=payload.stage,
            owner_id=user.id, team_id=user.team_id, summary=payload.summary,
            tags=payload.tags,
        )
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc


@router.get("/cases/{case_id}", summary="案件详情")
def get_case(
    case_id: str,
    user: RequireUser,
    with_materials: Annotated[bool, Query()] = True,
) -> dict[str, Any]:
    try:
        return case_service.get_case(case_id, user, with_materials=with_materials)
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc


@router.put("/cases/{case_id}", summary="更新案件")
def update_case(case_id: str, payload: CaseUpdate, user: RequireUser) -> dict[str, Any]:
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="没有需要更新的字段")
    try:
        return case_service.update_case(case_id, user, **fields)
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc


@router.delete("/cases/{case_id}", summary="删除案件（保留磁盘原文）")
def delete_case(case_id: str, user: RequireUser) -> dict[str, Any]:
    """删除案件档案与材料记录。**磁盘上的卷宗原文不删** —— 误删无法恢复，
    而档案记录删错了可以重建。"""
    try:
        return case_service.delete_case(case_id, user)
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc


@router.get("/cases/{case_id}/progress", summary="办理进度流水")
def get_progress(case_id: str, user: RequireUser) -> dict[str, Any]:
    case = case_service.get_case(case_id, user, with_materials=False)
    return {"case_id": case_id, "stage": case["stage"], "items": case["progress"]}


@router.post("/cases/{case_id}/progress", summary="追加办理进度")
def add_progress(case_id: str, payload: ProgressCreate, user: RequireUser) -> dict[str, Any]:
    try:
        case_service.get_case(case_id, user, with_materials=False)   # 先过权限
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc
    return case_service.add_progress(
        case_id, stage=payload.stage, note=payload.note,
        operator=payload.operator or user.username, task_id=payload.task_id,
        result=payload.result,
    )


# --------------------------------------------------------------------------- 证据链
@router.get("/cases/{case_id}/evidence-chain", summary="读取证据链")
def get_evidence_chain(case_id: str, user: RequireUser) -> dict[str, Any]:
    try:
        return case_service.get_evidence_chain(case_id, user)
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc


@router.post("/cases/{case_id}/evidence-chain", summary="生成证据链",
             status_code=status.HTTP_200_OK)
async def generate_evidence_chain(case_id: str, user: RequireUser) -> dict[str, Any]:
    """聚合案件摘要 + 材料，调用 LLM 产出 claim→evidence→gap 证据链并落库。

    模型不可用时退回确定性骨架（仍以材料为节点、标注待补），不会 500。
    """
    try:
        return await case_service.generate_evidence_chain(case_id, user)
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc


@router.get("/cases/{case_id}/evidence-assessment", summary="证据充分度评估")
def evidence_assessment(case_id: str, user: RequireUser) -> dict[str, Any]:
    """读已生成的证据链，给出三档结论（充分 / 基本充分但有风险 / 证据不足）+

    致命缺口清单 + 可补强缺口 + 补证建议。纯确定性计算，不调模型；
    缺口文案复用证据链里已生成的内容。界面需标注"AI 辅助判断，非法律意见"。
    """
    try:
        return case_service.assess_evidence_sufficiency(case_id, user)
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc


@router.get("/cases/{case_id}/win-assessment", summary="胜诉把握评估（读）")
def get_win_assessment(case_id: str, user: RequireUser) -> dict[str, Any]:
    """案件详情页「胜诉成功率」区读取用。

    **数字是现算的**（``case_service.score_win_rate``，纯确定性、不调模型），
    AI 法律分析则是读回上次 POST 存下来的。两半分开的理由见
    ``case_service`` 里「胜诉把握」那一段的说明。

    与证据链的既定口径一致：**只读不拦非诉案件**，非诉返回 ``supported=false``。
    """
    try:
        return case_service.get_win_assessment(case_id, user)
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc


@router.post("/cases/{case_id}/win-assessment", summary="胜诉把握评估（生成）",
             status_code=status.HTTP_200_OK)
async def generate_win_assessment(case_id: str, user: RequireUser) -> dict[str, Any]:
    """重算证据覆盖率并生成 AI 法律分析，**只把模型那半落库**。

    只对诉讼案件开放（与生成证据链同一道闸门）。模型不可用时法律分析退化为
    确定性建议，数字照常给出，不会 500。
    """
    try:
        return await case_service.generate_win_assessment(case_id, user)
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc


@router.get("/cases/by-task/{task_id}", summary="按任务号反查归属案件")
def case_by_task(task_id: str, user: RequireUser) -> dict[str, Any]:
    """分析报告里的「生成证据链」按钮用：从任务号找到它自动归档到的案件。

    找不到（该任务未归档成案件）返回 404，前端据此提示先归档。
    """
    c = case_service.find_case_by_task(task_id, user)
    if c is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"未找到任务 {task_id} 归属的案件（可能尚未归档）")
    return c


# --------------------------------------------------------------------------- 材料
@router.get("/cases/{case_id}/materials", summary="材料列表")
def list_materials(case_id: str, user: RequireUser) -> dict[str, Any]:
    try:
        items = case_service.list_materials(case_id, user)
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc
    return {"case_id": case_id, "count": len(items), "items": items}


@router.post("/cases/{case_id}/materials", summary="上传材料",
             status_code=status.HTTP_201_CREATED)
async def upload_material(
    case_id: str,
    user: RequireUser,
    meta: Meta,
    file: Annotated[UploadFile, File(description="卷宗文件（Word / PDF / 图片）")],
    doc_type: Annotated[str, Form(description="材料类型")] = "unknown",
) -> dict[str, Any]:
    """上传一份材料。

    **先权限校验，再写磁盘。** 反过来会先把文件落进 UPLOAD_DIR/别人案件目录、
    再报 403，磁盘上就留下了不该存在的文件。

    证据材料是证据链的输入，所以**同样只对诉讼案件开放**
    （见 ``case_service.assert_evidence_chain_allowed``）；这一道也放在写盘之前。
    """
    try:
        case = case_service.get_case(case_id, user, with_materials=False)
        case_service.assert_evidence_chain_allowed(case.get("case_type"))
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc

    if not file.filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="缺少文件名")

    limit_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    tmp_path: Path | None = None
    size = 0
    try:
        # 分块写到临时文件，边写边算大小 —— 超限就立刻中止，
        # 不把几百 MB 读进内存（``await file.read()`` 的经典坑）
        with tempfile.NamedTemporaryFile(delete=False, suffix=Path(file.filename).suffix) as tmp:
            tmp_path = Path(tmp.name)
            while True:
                block = await file.read(CHUNK)
                if not block:
                    break
                size += len(block)
                if size > limit_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"文件超过上限 {settings.MAX_UPLOAD_MB} MB",
                    )
                tmp.write(block)

        if size == 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="空文件")

        info = case_service.register_material(
            case_id, tmp_path, filename=file.filename, doc_type=doc_type,
            uploaded_by=user.id, move=True,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        await file.close()

    from backend.agents.harness.audit import audit_writer

    audit_writer.material_access(
        case_id=case_id, material=info["filename"], action="material.upload",
        user_id=user.id, username=user.username, team_id=user.team_id,
        client=meta.client, ip=meta.ip, resource_id=str(info["id"]),
        detail=f"上传 {size} 字节，doc_type={doc_type}",
    )
    # **必须显式剥离 ``stored_path``。**
    # ``register_material`` 返回的是 ``material_to_dict(..., with_path=True)``
    # —— 它对内是对的（解析管线要知道去哪儿读这份材料），但它不是接口响应体。
    # 服务端绝对路径会把内网目录结构暴露出去，这里不能整包回传。
    payload = {k: v for k, v in info.items() if k != "stored_path"}
    return {**payload, "uploaded": True}


@router.get("/materials/{material_id}", summary="材料详情")
def get_material(material_id: int, user: RequireUser) -> dict[str, Any]:
    try:
        return case_service.get_material(material_id, user)
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc


@router.get("/materials/{material_id}/content", summary="材料正文（界面查看用）")
def material_content(
    material_id: int,
    user: RequireUser,
    max_chars: Annotated[int, Query(
        ge=1000, le=500_000,
        description="正文上限。界面按需调大，但有个数 —— 防止一份几十万字的判决书把响应体顶穿",
    )] = case_service.MATERIAL_VIEW_MAX_CHARS,
) -> dict[str, Any]:
    """读材料正文。**读取即留痕**（与 ``list_materials`` 同一条：材料访问审计）。

    与 ``POST /materials/{id}/parse`` 的区别：那条**刻意不回正文**（解析产物可能是
    几十万字，接口只回结构化摘要）。界面「查看材料」需要的是正文本身，所以单开这一条，
    并且明确回报 ``source``（解析产物 / 原文）与 ``truncated``（有没有被截断）——
    律师拿着这段文字核对时，得知道看的是哪一份、后面还有没有。
    """
    try:
        return case_service.read_material_text(material_id, user, max_chars=max_chars)
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc


@router.delete("/materials/{material_id}", summary="删除材料记录（保留磁盘原文）")
def delete_material(material_id: int, user: RequireUser) -> dict[str, Any]:
    try:
        return case_service.delete_material(material_id, user)
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc


@router.post("/materials/{material_id}/parse", summary="解析材料（抽取文本 / 条款 / 表格）")
async def parse_material(
    material_id: int,
    user: RequireUser,
    meta: Meta,
    do_vision: Annotated[bool, Query(description="扫描件是否走多模态识别")] = True,
) -> dict[str, Any]:
    """解析材料并回写解析状态。

    解析结果**不回传全文**（可能几十万字），只回传结构化摘要：
    类型、页数、字符数、条款数、表格数、占位符数。全文由后续的
    ``POST /search/material`` 或直接发起任务来消费。
    """
    try:
        # 两次调用都传 ``user``，各自独立过一遍权限。
        # 看起来重复，但**不能用 ``INTERNAL`` 省掉第二次**：
        # 一旦这里习惯用内部哨兵取路径，"猜 id 读别人合同"就成了一条
        # 不需要权限的通道，而且它只在某次重构后才暴露。
        info = case_service.get_material(material_id, user)
        path = case_service.material_path(material_id, user)
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc

    if not Path(path).is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="材料文件不存在（可能已被移动或清理）")

    from backend.parser.pipeline import parse_document

    try:
        doc = await parse_document(path, do_vision=do_vision)
    except Exception as exc:  # noqa: BLE001
        logger.exception("材料解析失败：%s", material_id)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f"解析失败：{type(exc).__name__}: {exc}") from exc

    text = getattr(doc, "text", "") or ""
    parsed_path = getattr(doc, "parsed_path", "") or ""
    case_service.mark_parsed(material_id, parsed_path=parsed_path,
                            doc_type=getattr(doc, "doc_type", "") or info["doc_type"])

    from backend.agents.harness.audit import audit_writer

    audit_writer.material_access(
        case_id=info["case_id"], material=info["filename"], action="material.parse",
        user_id=user.id, username=user.username, team_id=user.team_id,
        client=meta.client, ip=meta.ip, resource_id=str(material_id),
        detail=f"解析 {len(text)} 字符，vision={do_vision}",
    )
    return {
        "material_id": material_id,
        "case_id": info["case_id"],
        "filename": info["filename"],
        "doc_type": getattr(doc, "doc_type", "") or info["doc_type"],
        "chars": len(text),
        "pages": len(getattr(doc, "pages", None) or []),
        "clauses": len(getattr(doc, "clauses", None) or []),
        "tables": len(getattr(doc, "tables", None) or []),
        "placeholders": len(getattr(doc, "placeholders", None) or []),
        "vision_used": bool(getattr(doc, "vision_used", False)),
        "notes": list(getattr(doc, "notes", None) or []),
        # ⚠️ ``parsed_path`` **不回传**（2026-10-12 移除）：它是服务端绝对路径，
        # 而这条响应直接进浏览器。前端只用 ``parsed`` 这个布尔量与 ``preview``。
        # 想看正文请用 ``GET /materials/{id}/content``（那条也做了同样的剥离）。
        "preview": text[:300],
        "note": "全文不回传，请用任务或材料检索接口消费",
    }


@router.get("/cases/stats/summary", summary="案件统计")
def case_stats(user: RequireUser) -> dict[str, Any]:
    """案件统计。管理员看全所，律师看本人可见范围 —— **字段集两边一致**。

    见 ``case_service.stats`` 的注释：律师分支少给 materials，
    工作台的「卷宗材料」就会永远显示「—」。
    """
    if user.is_admin:
        return case_service.stats()
    rows = case_service.list_cases(user, limit=200)
    out = case_service.stats(case_ids=[r["case_id"] for r in rows])
    out["scope"] = "本人可见范围"
    return out
