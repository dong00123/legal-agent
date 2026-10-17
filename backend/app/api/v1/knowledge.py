"""知识库 / 法规库 / 更新包管理接口（管理后台，**全部限管理员**）。

方案 12.3（B）律所管理后台要求四件事：知识库管理、法规库版本发布与回滚、
系统配置、审计中心。本文件覆盖前两件（配置与审计在 ``admin.py``）。

为什么要单独一个文件而不是塞进 ``admin.py``：知识库与法规库是**内容资产**，
它们的变更直接改变 Agent 的输出依据；账号与配置是**运行参数**。
两类操作的审计粒度、回滚方式、风险等级都不同，混在一起会让"谁动了知识库"
这类问题在审计里查不出来 —— 所以标签也分开（``知识库`` / ``管理后台``）。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from backend.app.api.deps import Meta, Page, RequireAdmin, page_of
from backend.app.services import case_service, knowledge_service

logger = logging.getLogger("legal_agent.api.knowledge")

router = APIRouter(tags=["知识库与法规库"])


class DocUpsert(BaseModel):
    title: str = Field(..., min_length=1, max_length=256)
    doc_id: int | None = Field(None, description="传了则更新该行；不传则按「标题+版本」匹配")
    doc_type: str = Field("guideline", description="guideline / checklist / template / other")
    category: str = ""
    version: str = "v1.0"
    effective_date: str = Field("", description="YYYY-MM-DD，知识库引用会标注版本与生效日期")
    status: str = Field("draft", description="active / draft / archived")
    content: str = Field("", description="正文；变更后会自动标记为待入库")
    stored_path: str = ""


class StatuteStatusUpdate(BaseModel):
    status: str = Field(..., description="现行有效 / 已修订 / 已废止 / 尚未生效 / 未知")
    repealed_date: str = ""
    version: str = ""


class PackCreate(BaseModel):
    pack_id: str = Field(..., min_length=1, max_length=64)
    filename: str = ""
    stored_path: str = ""
    doc_count: int = 0
    status: str = "imported"
    detail: str = ""


def _audit(admin: Any, meta: Meta, action: str, *, resource: str, resource_id: str,
           detail: str, result: str = "ok") -> None:
    from backend.agents.harness.audit import audit_writer

    audit_writer.record(action, user_id=admin.id, username=admin.username,
                        team_id=admin.team_id, client=meta.client, ip=meta.ip,
                        resource=resource, resource_id=resource_id,
                        detail=detail, result=result)


def _guard(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


# --------------------------------------------------------------------------- 知识库
@router.get("/knowledge/docs", summary="知识库文档列表")
def list_docs(
    admin: RequireAdmin,
    page: Page,
    keyword: Annotated[str, Query()] = "",
    doc_type: Annotated[str, Query()] = "",
    category: Annotated[str, Query()] = "",
    doc_status: Annotated[str, Query(alias="status")] = "",
) -> dict[str, Any]:
    rows = knowledge_service.list_docs(keyword=keyword, doc_type=doc_type,
                                      category=category, status=doc_status,
                                      limit=page.limit, offset=page.offset)
    return page_of(rows, page)


@router.get("/knowledge/docs/{doc_id}", summary="知识库文档详情（含正文）")
def get_doc(doc_id: int, admin: RequireAdmin) -> dict[str, Any]:
    row = knowledge_service.get_doc(doc_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"文档不存在：{doc_id}")
    return row


@router.post("/knowledge/docs", summary="新建/更新知识库文档")
def upsert_doc(payload: DocUpsert, admin: RequireAdmin, meta: Meta) -> dict[str, Any]:
    """写入知识库。``status="active"`` 会把同标题的其它生效版本置为 archived。

    返回体里的 ``superseded_versions`` 要如实透出：管理员需要知道
    "这次发布顶掉了哪几版"，否则一次误操作会让某条指引悄悄失去生效版本，
    而检索引用只能标出"版本 v1.0"、看不出它其实已经不该被引用了。
    """
    try:
        out = knowledge_service.upsert_doc(
            title=payload.title, doc_id=payload.doc_id, doc_type=payload.doc_type,
            category=payload.category, version=payload.version,
            effective_date=payload.effective_date, status=payload.status,
            content=payload.content, stored_path=payload.stored_path, uploaded_by=admin.id,
        )
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc
    _audit(admin, meta, "knowledge.upsert", resource="knowledge_doc",
           resource_id=str(out["id"]),
           detail=f"{out['title']} {out['version']} → {out['status']}"
                  f"{'，替代版本 ' + '、'.join(out['superseded_versions']) if out['superseded_versions'] else ''}")
    return out


@router.delete("/knowledge/docs/{doc_id}", summary="删除知识库文档")
def delete_doc(doc_id: int, admin: RequireAdmin, meta: Meta) -> dict[str, Any]:
    out = knowledge_service.delete_doc(doc_id)
    if out.get("ok"):
        _audit(admin, meta, "knowledge.delete", resource="knowledge_doc",
               resource_id=str(doc_id), detail=f"删除《{out.get('title', '')}》")
    return out


@router.get("/knowledge/categories", summary="知识库分类")
def categories(admin: RequireAdmin) -> dict[str, Any]:
    return {"items": knowledge_service.categories()}


# --------------------------------------------------------------------------- 法规库
@router.get("/statutes/lib", summary="法规库清单")
def statute_lib(
    admin: RequireAdmin,
    page: Page,
    keyword: Annotated[str, Query()] = "",
    lib_status: Annotated[str, Query(alias="status")] = "",
    category: Annotated[str, Query()] = "",
    level: Annotated[str, Query()] = "",
) -> dict[str, Any]:
    rows = knowledge_service.list_statutes(keyword=keyword, status=lib_status,
                                          category=category, level=level,
                                          limit=page.limit, offset=page.offset)
    return page_of(rows, page)


@router.get("/statutes/lib/stats", summary="法规库统计")
def statute_lib_stats(admin: RequireAdmin) -> dict[str, Any]:
    return knowledge_service.statute_stats()


@router.get("/statutes/lib/{doc_name}", summary="单部法规详情")
def statute_detail(doc_name: str, admin: RequireAdmin) -> dict[str, Any]:
    row = knowledge_service.get_statute(doc_name)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"法规库中无此法规：{doc_name}")
    return row


@router.put("/statutes/lib/{doc_name}/status", summary="变更法规效力状态")
def set_statute_status(doc_name: str, payload: StatuteStatusUpdate,
                       admin: RequireAdmin, meta: Meta) -> dict[str, Any]:
    """改效力状态。**法条效力写错比找不到法条更危险** ——
    标成"现行有效"的已废止条文会被引用进正式文书，所以这里强制走四个白名单取值，
    并逐条留审计。"""
    try:
        out = knowledge_service.set_statute_status(
            doc_name, payload.status, repealed_date=payload.repealed_date,
            version=payload.version,
        )
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc
    if not out.get("ok"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=out.get("reason", "变更失败"))
    _audit(admin, meta, "statute.status", resource="statute_doc", resource_id=doc_name,
           detail=f"{doc_name} → {payload.status}"
                  + (f"（废止日期 {payload.repealed_date}）" if payload.repealed_date else ""))
    return out


# --------------------------------------------------------------------------- 更新包
@router.get("/update-packs", summary="法规更新包列表")
def list_packs(
    admin: RequireAdmin,
    pack_status: Annotated[str, Query(alias="status")] = "",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    return {"items": knowledge_service.list_packs(status=pack_status, limit=limit)}


@router.post("/update-packs", summary="登记法规更新包",
             status_code=status.HTTP_201_CREATED)
def record_pack(payload: PackCreate, admin: RequireAdmin, meta: Meta) -> dict[str, Any]:
    try:
        out = knowledge_service.record_pack(
            payload.pack_id, filename=payload.filename, stored_path=payload.stored_path,
            doc_count=payload.doc_count, status=payload.status, detail=payload.detail,
            uploaded_by=admin.id,
        )
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc
    _audit(admin, meta, "updatepack.record", resource="update_pack",
           resource_id=payload.pack_id,
           detail=f"登记更新包 {payload.filename or payload.pack_id}，含 {payload.doc_count} 部法规")
    return out


@router.post("/update-packs/{pack_id}/rollback", summary="回滚更新包")
def rollback_pack(pack_id: str, admin: RequireAdmin, meta: Meta) -> dict[str, Any]:
    """标记回滚。**法规数据不会自动恢复**（见 ``knowledge_service.rollback_pack``），
    返回体里带 ``detail`` 说明边界 —— 不能让管理员以为点了按钮就回到上一版了。"""
    out = knowledge_service.rollback_pack(pack_id)
    if out.get("ok"):
        _audit(admin, meta, "updatepack.rollback", resource="update_pack",
               resource_id=pack_id, result="ok",
               detail="已标记回滚；法规数据未自动恢复，需按清单人工核对或重新导入上一版")
    return out


# --------------------------------------------------------------------------- 入库
@router.post("/knowledge/reingest", summary="重建语料索引（法规 / 类案 / 知识库）")
async def reingest(
    admin: RequireAdmin,
    meta: Meta,
    force: Annotated[bool, Query(description="强制重建（忽略已有索引）")] = False,
    statutes: Annotated[bool, Query()] = True,
    cases: Annotated[bool, Query()] = True,
    knowledge: Annotated[bool, Query()] = True,
) -> dict[str, Any]:
    """重建语料索引。

    **耗时可能很长**（几千条法条要重新向量化），所以这里同步执行并如实返回耗时。
    生产环境应改成走任务队列（``backend/worker``），接口只返回 task_id ——
    同步等待会被网关超时掐断，而掐断时索引处于半建状态。
    """
    import time

    from backend.corpus.ingest import ingest_all

    started = time.monotonic()
    try:
        report = await ingest_all(force=force, statutes=statutes, cases=cases,
                                  knowledge=knowledge)
    except Exception as exc:  # noqa: BLE001
        logger.exception("语料重建失败")
        _audit(admin, meta, "knowledge.reingest", resource="corpus", result="error",
               resource_id="", detail=f"重建失败：{type(exc).__name__}: {exc}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f"重建失败：{type(exc).__name__}: {exc}") from exc

    elapsed = int((time.monotonic() - started) * 1000)
    body = report.to_dict() if hasattr(report, "to_dict") else vars(report)
    _audit(admin, meta, "knowledge.reingest", resource="corpus",
           resource_id="all", detail=f"重建索引耗时 {elapsed}ms")
    return {"ok": True, "elapsed_ms": elapsed, "report": body}
