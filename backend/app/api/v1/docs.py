"""文书草稿接口（方案 12.3「草稿同步：Web ↔ EXE 同一账号无缝续编」）。

这是双端协同的关键一环：EXE 端生成文书草稿 → 落库；律师外出时用浏览器打开
Web 端接着改 → 保存；回到所里在 EXE 上继续。整条链路的载体就是 ``la_draft``。

两个刻意的设计：

1. **保存是新版本，不是覆盖。** 律师改文书最常见的动作是"改了两段、
   发现还不如原来那版、退回去"。直接覆盖等于把退路砍掉。
   所以 ``save_draft`` 每次把 ``version`` 加一，历史内容不删。

2. **导出与正文分开存。** 库里 ``exported_path`` 指向某个版本导出的 docx；
   正文继续改不会动它。界面上要能显示"当前正文 v3，上次导出的是 v2" ——
   如果合并成一个字段，律师会以为导出的就是眼前这版。

   **但这个路径不出接口。** 响应里只有 ``exported``（是否导出过）与
   ``exported_name``（文件名）；导出回执也不回 ``path``。
   要下文件走 ``GET /drafts/{id}/download`` —— 服务端绝对路径对浏览器没有用，
   却会把部署用户名与目录结构带出去。
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query, status
from pydantic import BaseModel, Field

from backend.app.api.deps import Meta, Page, RequireUser, page_of
from backend.app.config import settings
from backend.app.services import draft_service
from backend.app.services.html_sanitize import sanitize

logger = logging.getLogger("legal_agent.api.docs")

router = APIRouter(prefix="/drafts", tags=["文书草稿"])


class DraftCreate(BaseModel):
    content: str = Field(..., min_length=1, description="正文")
    doc_type: str = Field(
        "complaint",
        description="见 /drafts/meta/doc-types（complaint / defense / arbitration_application / …）")
    title: str = ""
    case_id: str = ""
    task_id: str = ""
    citations: list[dict[str, Any]] = Field(default_factory=list)
    # 模板型文书（编辑页）。**Pydantic 模型不扩，下面那条路由就写不进这些列** ——
    # 服务层加好了字段、接口层却收不到，表现是"前端传了、后端没存"，最难查的一处。
    template_id: str = ""
    fills: dict[str, str] = Field(default_factory=dict)
    content_html: str = ""
    chat_log: list[dict[str, Any]] = Field(default_factory=list)
    snapshot: bool = Field(
        False, description="是否给这一版存历史快照（由编辑页传 true）")


class DraftSave(BaseModel):
    content: str = Field(..., min_length=1)
    title: str = ""
    citations: list[dict[str, Any]] | None = None
    template_id: str | None = None
    fills: dict[str, str] | None = None
    content_html: str | None = None
    chat_log: list[dict[str, Any]] | None = None
    snapshot: bool = False


class ExportMeta(BaseModel):
    """Word 导出可选的"按原模板回填"参数。

    模板型文书（如劳动争议起诉状）是表格形式，如果按 Markdown 转 Word 会丢失表格；
    传入 template_id + fills 即可在原始 .docx 模板上直接回填，保留表格和勾选框。
    """
    template_id: str = ""
    fills: dict[str, str] = Field(default_factory=dict)
    # 预览区当前正文。带上它有两个作用：
    # 1) 用户在预览里点了勾选框，按出现顺序把 ☑/☐ 应用到模板，保证"所见即所得"；
    # 2) 手工改过正文时（此时不传 template_id），导出直接用这份文本。
    content: str = ""
    # 编辑页当前的富文本。传它 = "导我屏幕上这一份"（含还没保存的改动）。
    # 不传就退回草稿里存的那份。
    content_html: str = ""


def _guard(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("", summary="草稿列表")
def list_drafts(
    user: RequireUser,
    page: Page,
    case_id: Annotated[str, Query()] = "",
    task_id: Annotated[str, Query()] = "",
    doc_type: Annotated[str, Query()] = "",
    keyword: Annotated[str, Query()] = "",
    all_users: Annotated[bool, Query(description="管理员可看全部用户的草稿")] = False,
) -> dict[str, Any]:
    """草稿列表。默认只看自己的；管理员可加 ``all_users=true`` 看全所。

    **默认按用户过滤**而不是按团队：草稿是"某位律师正在写的文书"，
    同团队的人看到别人写了一半的草稿没有意义，而且容易误改。
    """
    user_id = None if (all_users and user.is_admin) else user.id
    rows = draft_service.list_drafts(user_id=user_id, case_id=case_id, task_id=task_id,
                                     doc_type=doc_type, keyword=keyword,
                                     limit=page.limit, offset=page.offset)
    total = draft_service.count_drafts(user_id=user_id, case_id=case_id, task_id=task_id,
                                       doc_type=doc_type, keyword=keyword)
    return page_of(rows, page, total=total)


@router.post("", summary="新建草稿", status_code=status.HTTP_201_CREATED)
def create_draft(payload: DraftCreate, user: RequireUser) -> dict[str, Any]:
    try:
        return draft_service.create_draft(
            content=payload.content, doc_type=payload.doc_type, title=payload.title,
            case_id=payload.case_id, task_id=payload.task_id, user_id=user.id,
            citations=payload.citations, template_id=payload.template_id,
            fills=payload.fills, content_html=sanitize(payload.content_html),
            chat_log=payload.chat_log, snapshot=payload.snapshot,
        )
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc


@router.get("/{draft_id}", summary="草稿详情（含正文）")
def get_draft(
    draft_id: str,
    user: RequireUser,
    with_content: Annotated[bool, Query()] = True,
) -> dict[str, Any]:
    row = draft_service.get_draft(draft_id, with_content=with_content)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"草稿不存在：{draft_id}")
    _assert_owner(user, row)
    return row


def _template_meta(tpl_id: str, fills: dict[str, Any], missing: list[str]) -> dict[str, Any]:
    """编辑页顶栏那行摘要要用的模板信息；``tpl_id`` 认不出来就是空字典。

    **键名与 ``/doc/compose`` 的返回保持一致**（``template.file`` /
    ``template.doc_type_label`` / ``filled_count`` …）：前端 ``docEditMeta()``
    一份取值逻辑同时喂两条来源 —— 刚生成（compose 直接给）与重新打开草稿
    （只能从 preview 拿）。两处键名一旦分叉，症状是"刚生成的顶栏有字、
    从「全部草稿」打开的顶栏空着"，很难联想到是后端字段名不一致。
    """
    from backend.app.services import template_service as ts

    tpl = ts.get_template(tpl_id)
    if tpl is None:
        return {}
    return {
        "template": {
            "id": tpl["id"], "file": tpl["file"], "title": tpl["title"],
            "doc_type": tpl["doc_type"], "kind": tpl["kind"],
            "doc_type_label": ts.DOC_TYPE_LABEL.get(tpl["doc_type"], tpl["doc_type"]),
            "region": tpl["region"],
        },
        "doc_type": tpl["doc_type"],
        "region": tpl["region"],
        "field_count": tpl.get("field_count") or 0,
        "filled_count": len([v for v in fills.values() if v]),
        # 与 compose 一致：只回前 24 条，总数另给
        "missing": missing[:24],
        "missing_count": len(missing),
    }


@router.get("/{draft_id}/preview", summary="编辑页预览（模板保真 HTML）")
def preview_draft(draft_id: str, user: RequireUser) -> dict[str, Any]:
    """返回编辑页要用的一份 HTML，**与 Word 导出同源**。

    三条来源，按优先级：

    1. ``content_html`` 非空 → 律师手动排版过，直接给这份（净化后）。
       **不能再拿模板重渲染** —— 那会把手动的改动整片抹掉，而且要等下次导出
       才会发现；
    2. 有 ``template_id`` → ``filled_template_doc`` 回填真模板再渲染成 HTML。
       这是"严格按照模板生成"的落点，和导出走的是**同一个函数**，所以页面上
       看到的和下载到的必然一致；
    3. 都没有 → 正文按纯文本段落渲染（老草稿、非模板型文书）。

    ``missing`` 是还没填的栏目，前端拿去高亮 —— 现在这信息只以「待填 6 项」
    的文字列表存在，律师得自己一行行找。
    """
    row = draft_service.get_draft(draft_id, with_content=True)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"草稿不存在：{draft_id}")
    _assert_owner(user, row)

    out: dict[str, Any] = {
        "draft_id": draft_id,
        "title": row.get("title") or "",
        "template_id": row.get("template_id") or "",
        "version": row.get("version"),
        # 左列对话流。跟着这一页的引导请求一起回，省一次 ``GET /drafts/{id}``
        # —— 重开一份旧草稿时，那次请求就为了拿这一列 JSON。
        "chat_log": row.get("chat_log") or [],
        "page": {},
        "missing": [],
    }
    # 先算一次，两条分支都要用 —— 手动排版过的草稿，顶栏照样该显示是套哪份模板
    tpl_id = row.get("template_id") or ""
    fills = row.get("fills") or {}
    tpl_meta: dict[str, Any] = {}
    if tpl_id:
        from backend.app.services import template_service as ts

        tpl = ts.get_template(tpl_id)
        if tpl is not None:
            # ``missing`` 只能靠 render_markdown 顺带算出来。放在这里算**一次**，
            # 下面两条分支共用，免得两边口径分叉。
            tpl_meta = _template_meta(tpl_id, fills, ts.render_markdown(tpl, fills)[1])

    edited = (row.get("content_html") or "").strip()
    if edited:
        # 存的时候净化过，读出来再净化一次：纵深防御。库里可能存着净化器
        # 上线之前写进去的老数据，也可能有人直接改库。
        out.update(html=sanitize(edited), source="edited", **tpl_meta)
        return out

    if tpl_meta:
        from backend.app.services.docx_html import docx_to_html, page_geometry

        # ⚠️ 与导出同一个函数。这里若各渲染一次，两者会在细节处悄悄分叉。
        doc = filled_template_doc(tpl_id, fills, row.get("citations") or [])
        # 渲染器自己已经 escape 过所有文本，这里再净化一遍是为了挡
        # "将来有人在渲染器里加了个直接拼字符串的分支"。
        out.update(html=sanitize(docx_to_html(doc)), source="template",
                   page=page_geometry(doc), **tpl_meta)
        return out

    body = row.get("content") or ""
    out.update(
        html="".join(f"<p>{html.escape(ln)}</p>" for ln in body.splitlines()),
        source="plain",
    )
    return out


class ReviseIn(BaseModel):
    instruction: str = Field(..., min_length=1, description="自然语言修改要求")
    force: bool = Field(
        False, description="草稿已有手动排版时，是否允许覆盖（前端要先让用户确认）")
    chat_log: list[dict[str, Any]] | None = None


@router.post("/{draft_id}/revise", summary="按指令让 AI 改模板字段")
async def revise_draft(draft_id: str, payload: ReviseIn, user: RequireUser) -> dict[str, Any]:
    """用户在左框说"把请求事项第 2 条改成 8 万"，AI 改 ``fills`` 里对应的字段。

    **只改字段，不碰自由文本**（见 ``template_service.revise_fills`` 的说明）。
    返回里带 ``changed``，前端要把"实际改了哪几栏"显示出来 —— 模型经常会说
    "好的已修改"但其实一处没对上，只显示它的自述就会让人以为改成了。
    """
    row = draft_service.get_draft(draft_id, with_content=True)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"草稿不存在：{draft_id}")
    _assert_owner(user, row)

    tpl_id = row.get("template_id") or ""
    if not tpl_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="这份草稿不是套模板生成的，没有可按栏目修改的字段。"
                   "请用文书中心重新起草，或直接在右侧编辑器里手动修改。")

    # 手动排版过的话，重渲染会把排版整片冲掉。**在服务端拦**，不只靠前端弹确认 ——
    # 前端可以旧、可以被绕过，而丢的是律师的排版。
    if (row.get("content_html") or "").strip() and not payload.force:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="这份文书有手动排版过的内容，按栏目重排会覆盖它。"
                   "确认要覆盖请带 force=true 重发。")

    from backend.app.services import template_service as ts

    tpl = ts.get_template(tpl_id)
    if tpl is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"模板不存在：{tpl_id}")

    try:
        out = await ts.revise_fills(tpl, row.get("fills") or {}, payload.instruction)
    except Exception as exc:  # noqa: BLE001
        logger.exception("AI 改字段失败：%s", draft_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"模型调用失败：{type(exc).__name__}。文书没有被改动，可以重试。") from exc

    # 一处都没改成时**什么都不动**：不清 content_html、不重渲染、不拍快照。
    # 否则律师问了一句落不到字段上的话，反而把之前的手动排版抹掉了 ——
    # "没帮上忙还帮了倒忙"。
    html_text = ""
    if out["changed_count"]:
        try:
            # 与预览、导出同一个函数 —— 改完页面上立刻就是新版式，且与下载同源
            from backend.app.services.docx_html import docx_to_html

            html_text = sanitize(docx_to_html(
                filled_template_doc(tpl_id, out["fills"], row.get("citations") or [])))
        except Exception:  # noqa: BLE001 渲染失败不该回滚改动，返回空 HTML 让前端重取预览
            logger.exception("改字段后重渲染失败：%s", draft_id)

    saved = draft_service.save_draft(
        draft_id, content=row.get("content") or "", user_id=user.id,
        template_id=tpl_id, fills=out["fills"],
        # 重渲染了就必须把手动 HTML 清掉，否则预览还会优先给旧的那份
        content_html="" if out["changed_count"] else None,
        chat_log=payload.chat_log,
        snapshot=out["changed_count"] > 0, snapshot_source="ai_revise",
        snapshot_note=(payload.instruction or "")[:120],
    )

    return {
        "ok": True,
        "draft_id": draft_id,
        "reply": out["reply"],
        "fills": out["fills"],
        "changed": out["changed"],
        "changed_count": out["changed_count"],
        "html": html_text,
        "version": saved.get("version"),
        "degraded": out["degraded"],
        "provider": out["provider"],
        # 前端据此提示"文件里的其他内容可能没跟着变"——改字段改不到模板里
        # 不存在的栏目，这是这套方案的已知边界，要说出来而不是假装全能
        "scope": "template_fields",
    }


@router.get("/{draft_id}/versions", summary="历史版本列表")
def list_versions(draft_id: str, user: RequireUser) -> dict[str, Any]:
    row = draft_service.get_draft(draft_id, with_content=False)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"草稿不存在：{draft_id}")
    _assert_owner(user, row)
    items = draft_service.list_versions(draft_id)
    return {"draft_id": draft_id, "total": len(items), "items": items}


@router.post("/{draft_id}/versions/{version_id}/restore", summary="恢复到某个历史版本")
def restore_version(draft_id: str, version_id: int, user: RequireUser) -> dict[str, Any]:
    """恢复不擦历史：恢复后的状态本身又是一版，所以"恢复错了"还能再退回去。"""
    row = draft_service.get_draft(draft_id, with_content=False)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"草稿不存在：{draft_id}")
    _assert_owner(user, row)
    out = draft_service.restore_version(draft_id, version_id)
    if not out.get("ok"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=out.get("reason", "恢复失败"))
    return out


@router.put("/{draft_id}", summary="保存草稿（版本 +1）")
def save_draft(draft_id: str, payload: DraftSave, user: RequireUser) -> dict[str, Any]:
    row = draft_service.get_draft(draft_id, with_content=False)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"草稿不存在：{draft_id}")
    _assert_owner(user, row)
    try:
        out = draft_service.save_draft(draft_id, content=payload.content,
                                       title=payload.title, user_id=user.id,
                                       citations=payload.citations,
                                       template_id=payload.template_id, fills=payload.fills,
                                       content_html=(None if payload.content_html is None
                                                     else sanitize(payload.content_html)),
                                       chat_log=payload.chat_log, snapshot=payload.snapshot)
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc
    if not out.get("ok"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=out.get("reason", "保存失败"))
    return out


@router.delete("/{draft_id}", summary="删除草稿")
def delete_draft(draft_id: str, user: RequireUser) -> dict[str, Any]:
    row = draft_service.get_draft(draft_id, with_content=False)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"草稿不存在：{draft_id}")
    _assert_owner(user, row)
    return draft_service.delete_draft(draft_id)


@router.post("/{draft_id}/export", summary="导出草稿（md / txt / docx / pdf）")
def export_draft(
    draft_id: str,
    user: RequireUser,
    meta: Meta,
    fmt: Annotated[str, Query(description="导出格式：md / txt / docx / pdf")] = "md",
    export_meta: Annotated[ExportMeta, Body()] = ExportMeta(),
) -> dict[str, Any]:
    """导出到 ``data/exports`` 并回写 ``exported_path``。

    ``docx`` 需要 python-docx；未安装时**明确报错并给出替代方案**，
    而不是偷偷导出成 .md 却顶着 .docx 的扩展名 —— 律师双击打不开才知道被骗了。
    """
    row = draft_service.get_draft(draft_id, with_content=True)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"草稿不存在：{draft_id}")
    _assert_owner(user, row)

    fmt = (fmt or "md").lower().lstrip(".")
    if fmt not in ("md", "txt", "docx", "pdf"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="导出格式只支持 md / txt / docx / pdf")

    # 预览区当前正文优先：用户在页面上勾了框、改了字，导出的必须是他看到的那一份
    preview = (export_meta.content or "").strip()
    content = preview or (row.get("content") or "")
    safe_title = _safe_name(row.get("title") or row["draft_id"])
    stamp = f"{datetime.now():%Y%m%d%H%M%S}"
    out_dir = Path(settings.EXPORT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{safe_title}_v{row['version']}_{stamp}.{fmt}"

    # 编辑页手动排版过的富文本。**优先级最高**：律师在页面上加粗、调字号、
    # 挪落款，导出的必须是他看到的那一份（用户选的就是"所见即所得"）。
    # 客户端带了就用客户端的（含未保存改动），否则用草稿里存的。
    edited_html = (export_meta.content_html or row.get("content_html") or "").strip()
    tpl_id = export_meta.template_id or row.get("template_id") or ""
    fills = export_meta.fills or row.get("fills") or {}
    # 交付合同（修订稿草稿）的版式开关：A4 + 两端对齐 + 页脚自动编页。
    # 修订稿没有 `template_id`/`fills`/`content_html`（`chat.py` 建草稿时就没传），
    # **永远走下面 `_write_docx(markdown)` / `_write_pdf(markdown)` 那个 fallback 分支**，
    # 所以开关只在那两处生效；其它文书一个字不变。
    # 用户手动编辑过（`edited_html`）时以所见即所得为准，不套这个开关。
    is_contract = (row.get("doc_type") or "") == "revision"

    if fmt == "docx":
        try:
            if edited_html:
                from backend.app.services.html_export import write_docx_from_html

                write_docx_from_html(target, sanitize(edited_html), tpl_id,
                                     row.get("citations") or [])
            elif tpl_id and fills:
                _write_docx_from_template(
                    target, tpl_id, fills,
                    row.get("title") or "", row.get("citations") or [],
                    checks=_checkbox_states(content))
            else:
                _write_docx(target, row.get("title") or "", content,
                            row.get("citations") or [], contract=is_contract)
        except ImportError as exc:
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail="未安装 python-docx，无法导出 Word。"
                       "请先 pip install python-docx，或改用 fmt=md 导出 Markdown。",
            ) from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("docx 导出失败：%s", draft_id)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                detail=f"导出失败：{type(exc).__name__}: {exc}") from exc
    elif fmt == "pdf":
        try:
            if edited_html:
                from backend.app.services.html_export import write_pdf_from_html

                write_pdf_from_html(target, sanitize(edited_html),
                                    row.get("title") or "", row.get("citations") or [])
            elif tpl_id and fills:
                # 与 docx 分支**同源**：没手动编辑过就回填真模板再转 PDF。
                # 少了这条分支，PDF 会走下面的 `_write_pdf(markdown)` —— 那份 md 里
                # 标题的居中、落款的右对齐全靠行内标记，reportlab 那条线认不出来，
                # 于是同一份文书下载成 Word 是好的、下载成 PDF 就掉了版式。
                from backend.app.services.docx_html import docx_to_html
                from backend.app.services.html_export import write_pdf_from_html

                doc = filled_template_doc(tpl_id, fills, row.get("citations") or [],
                                          checks=_checkbox_states(content))
                # ⚠️ citations 传空：`filled_template_doc` 已经把「引用依据」写进 doc 里了，
                #    再传给 `write_pdf_from_html` 会附两遍（它自己也会附加）。
                write_pdf_from_html(target, sanitize(docx_to_html(doc)),
                                    row.get("title") or "", [])
            else:
                _write_pdf(target, row.get("title") or "", content,
                           row.get("citations") or [], contract=is_contract)
        except ImportError as exc:
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail="未安装 reportlab，无法导出 PDF。"
                       "请先 pip install reportlab，或改用 fmt=md 导出 Markdown。",
            ) from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("pdf 导出失败：%s", draft_id)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                detail=f"导出失败：{type(exc).__name__}: {exc}") from exc
    else:
        target.write_text(content, encoding="utf-8")

    info = draft_service.mark_exported(draft_id, str(target))

    from backend.agents.harness.audit import audit_writer

    audit_writer.record("draft.exports", user_id=user.id, username=user.username,
                        team_id=user.team_id, client=meta.client, ip=meta.ip,
                        case_id=row.get("case_id") or "", task_id=row.get("task_id") or "",
                        resource="draft", resource_id=draft_id,
                        detail=f"导出 v{row['version']} 为 {fmt}：{target.name}")
    return {
        "ok": True,
        "draft_id": draft_id,
        "version": row["version"],
        "format": fmt,
        # **不回 ``path``。** 服务端绝对路径带部署用户名与目录结构，
        # 而 Web 端拿到它什么也做不了（浏览器读不了服务器文件系统）。
        # 要下文件走下面的 ``/download``；导出回执给文件名与大小就够界面对账了。
        "filename": target.name,
        "size_bytes": target.stat().st_size if target.exists() else 0,
        "record": info,
    }


@router.get("/{draft_id}/download", summary="下载最近一次导出的文件")
def download_exported(
    draft_id: str,
    user: RequireUser,
    meta: Meta,
):
    """下载"最近一次导出"的那个文件（浏览器里点"下载"走这里）。

    没有这个接口时，"导出"只能把服务端绝对路径回给客户端 ——
    Web 端拿到路径什么也做不了（浏览器不能读服务器文件系统），
    等于"导出"这个按钮是坏的。

    路径来自 ``draft_service.export_path()``（**唯一出口**），
    它是服务端绝对路径，所以这里必须自己确认它落在允许的目录里。

    **路径必须在 ``EXPORT_DIR`` 之内。** 这个值来自数据库，
    虽然只有服务端写它，但把它当作可信输入直接读文件是个坏习惯：
    将来任何一处能写这个字段的代码（导入脚本、迁移）都会立刻变成任意文件读取。
    这里用 ``resolve()`` 后比对前缀，挡住 ``../`` 与软链。
    """
    from fastapi.responses import FileResponse

    row = draft_service.get_draft(draft_id, with_content=False)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"草稿不存在：{draft_id}")
    _assert_owner(user, row)

    raw = draft_service.export_path(draft_id)
    if not raw:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="该草稿还没有导出过，请先调用导出接口")
    export_root = Path(settings.EXPORT_DIR).resolve()
    target = Path(raw).resolve()
    if export_root not in target.parents and target != export_root:
        logger.warning("拒绝下载导出目录之外的路径：%s", target)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="导出文件不在允许的导出目录内")
    if not target.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"导出文件已不在磁盘上：{target.name}")

    from backend.agents.harness.audit import audit_writer

    audit_writer.record("draft.download", user_id=user.id, username=user.username,
                        team_id=user.team_id, client=meta.client, ip=meta.ip,
                        resource="draft", resource_id=draft_id,
                        detail=f"下载导出文件 {target.name}")
    return FileResponse(target, filename=target.name,
                        media_type="application/octet-stream")


def _assert_owner(user: Any, row: dict[str, Any]) -> None:
    """草稿归属校验：本人或管理员。

    ``user_id`` 为空的历史草稿（服务端脚本建的）**归管理员** ——
    它们可能含内部测试内容，不该出现在普通律师的列表里。
    """
    if user.is_admin:
        return
    if row.get("user_id") is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="该草稿无归属人（服务端生成），仅管理员可见")
    if int(row["user_id"]) != int(user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问他人草稿")


def _draw_footer_page_number(canvas: Any, doc: Any, font: str) -> None:
    """画居中页脚页码 —— 交付合同的页数必须在**页脚**，不在正文里。

    用户 2026-09-29：「页数是页脚，不是里面的文字」。原件（`_pdfgeom.txt`）
    页码是宋体 9pt、居中、距页底约 48pt，这里照做。
    """
    from reportlab.lib import pagesizes

    canvas.saveState()
    canvas.setFont(font, 9)
    canvas.drawCentredString(pagesizes.A4[0] / 2.0, 45, str(canvas.getPageNumber()))
    canvas.restoreState()


def _write_pdf(target: Path, title: str, content: str,
             citations: list[dict[str, Any]], *, contract: bool = False) -> None:
    """把 Markdown 正文导出成 PDF。依赖 reportlab + 系统自带中文字体。

    只做模板渲染会产出的几种写法：标题 / 小节 / 加粗 / 列表 / 普通段落 / 引用注。

    ``contract=True``（**只有修订稿草稿**打开，见 :func:`export_draft`）按交付合同的
    版式来：正文两端对齐、章标题放大、页脚自动编页。其余文书的输出一个字不变。
    """
    from reportlab.lib import colors, enums, pagesizes
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer
    from xml.sax.saxutils import escape

    from backend.app.services.html_export import register_cjk_fonts

    # 字体注册与 HTML→PDF 那条链路共用（原先两边各写一份，改一处忘一处
    # 就会有一端输出成豆腐块）。
    base_font = register_cjk_fonts()

    # 中文 TrueType（simsun / msyh）里**没有** U+2610 ☐ 和 U+2611 ☑ 这两个字形，
    # 直接输出会印成空白方块（律师看到的就是"勾不见了"）。换成字体里确实存在的
    # U+25A1 □（未勾）和 U+25A0 ■（已勾）。
    content = (content or "").replace("☑", "■").replace("☐", "□")

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        "CN_Title", fontName=base_font, fontSize=(18), leading=26,
        alignment=enums.TA_CENTER, spaceAfter=(12), wordWrap="CJK"))
    styles.add(ParagraphStyle(
        "CN_Heading2", fontName=base_font, fontSize=(14), leading=22,
        alignment=enums.TA_CENTER, spaceAfter=(10), wordWrap="CJK"))
    styles.add(ParagraphStyle(
        "CN_Heading3", fontName=base_font, fontSize=(12), leading=20,
        alignment=enums.TA_LEFT, spaceBefore=(10), spaceAfter=(4),
        wordWrap="CJK"))
    styles.add(ParagraphStyle(
        "CN_Body", fontName=base_font, fontSize=(12), leading=20,
        alignment=enums.TA_LEFT, firstLineIndent=(24), spaceAfter=(6),
        wordWrap="CJK"))
    styles.add(ParagraphStyle(
        "CN_Bullet", fontName=base_font, fontSize=(12), leading=20,
        alignment=enums.TA_LEFT, leftIndent=(28), firstLineIndent=(-12),
        spaceAfter=(3), wordWrap="CJK"))
    styles.add(ParagraphStyle(
        "CN_Label", fontName=base_font, fontSize=(12), leading=20,
        alignment=enums.TA_LEFT, spaceBefore=(6), spaceAfter=(2),
        wordWrap="CJK"))
    styles.add(ParagraphStyle(
        "CN_Note", fontName=base_font, fontSize=(10), leading=16,
        textColor=colors.grey, alignment=enums.TA_LEFT, leftIndent=(12),
        spaceAfter=(6), wordWrap="CJK"))
    # 套模板带出的版式（落款右对齐，见 template_service._norm_align）。
    # ⚠️ **不能改 `CN_Body` 的 alignment** —— 它是模块内共享的正文样式，
    #    改一个段落会让全文都变右对齐。对齐段落一律单独套样式。
    #    也不带 `firstLineIndent`：落款要的是齐右，首行缩进的空档会把"齐"破坏掉。
    styles.add(ParagraphStyle(
        "CN_Right", fontName=base_font, fontSize=(12), leading=20,
        alignment=enums.TA_RIGHT, spaceAfter=(6), wordWrap="CJK"))
    styles.add(ParagraphStyle(
        "CN_Center", fontName=base_font, fontSize=(12), leading=20,
        alignment=enums.TA_CENTER, spaceAfter=(6), wordWrap="CJK"))
    # 靠左且**不缩进**（落款签名区：甲方/乙方/地址/盖章）。
    # 交付合同的落款要跟原件一样顶格，`CN_Body` 的首行缩进会把"甲"字推进去两格。
    styles.add(ParagraphStyle(
        "CN_Left", fontName=base_font, fontSize=(12), leading=20,
        alignment=enums.TA_LEFT, spaceAfter=(6), wordWrap="CJK"))
    if contract:
        # 交付合同：正文两端对齐（原件就是齐的）、章标题放到 14pt。
        # ⚠️ 章标题靠**字号**拉开层级是有原因的：reportlab 这条线的 `**bold**` 实际不生效
        #    （`register_cjk_fonts` 注册了 `Chinese-Bold` 却没调 `registerFontFamily`），
        #    12pt 的章标题和 12pt 正文在 PDF 里完全分不出来。
        #    不去动全局的字体族注册 —— 那会影响所有文书的 PDF。
        styles.add(ParagraphStyle(
            "CN_Body_C", fontName=base_font, fontSize=(12), leading=20,
            alignment=enums.TA_JUSTIFY, firstLineIndent=(24), spaceAfter=(6),
            wordWrap="CJK"))
        styles.add(ParagraphStyle(
            "CN_Heading3_C", fontName=base_font, fontSize=(14), leading=22,
            alignment=enums.TA_LEFT, spaceBefore=(10), spaceAfter=(4), wordWrap="CJK"))
    st_body = "CN_Body_C" if contract else "CN_Body"
    st_h3 = "CN_Heading3_C" if contract else "CN_Heading3"

    def _fmt(text: str) -> str:
        s = escape(str(text or ""))
        s = s.replace("\n", "<br/>")
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        # 修订稿的两种标记：`~~原文~~` 删除线、`++新增++` 蓝色下划线。
        # 合同修订版要靠它们才能一眼看出"改前是什么、改后是什么"。
        s = re.sub(r"~~(.+?)~~", r"<strike>\1</strike>", s)
        s = re.sub(r"\+\+(.+?)\+\+", r'<font color="#1F4FC0"><u>\1</u></font>', s)
        return s

    story: list[Any] = []
    if title and not (content or "").lstrip().startswith("#"):
        story.append(Paragraph(_fmt(title), styles["CN_Title"]))

    for raw_line in (content or "").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            story.append(Spacer(1, (6)))
            continue
        if stripped in ("---", "***", "___"):
            story.append(Spacer(1, (12)))
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            level, text = len(m.group(1)), m.group(2)
            if level == 1:
                story.append(Paragraph(_fmt(text), styles["CN_Title"]))
            elif level == 2:
                story.append(Paragraph(_fmt(text), styles["CN_Heading2"]))
            else:
                story.append(Paragraph(_fmt(text), styles[st_h3]))
            continue

        if stripped.startswith(">"):
            story.append(Paragraph(_fmt(stripped.lstrip("> ").strip()), styles["CN_Note"]))
            continue

        m = re.match(r"^[-*]\s+\*\*(.+?)\*\*\s*$", stripped)
        if m:
            story.append(Paragraph(_fmt(m.group(1)), styles["CN_Label"]))
            continue

        m = re.match(r"^[-*]\s+(.*)$", stripped)
        if m:
            story.append(Paragraph("• " + _fmt(m.group(1)), styles["CN_Bullet"]))
            continue

        # 套模板带出的**版式标记**（落款右对齐），见 template_service._norm_align。
        # `left` 是交付合同加的（落款签名区免缩进，见 `revise_service._contract_markdown`）；
        # 从前它匹配不上，会**原样印出 `<p align="left">甲方…</p>` 这串标记**。
        m = re.match(r'^<p align="(right|center|left)">(.*)</p>$', stripped)
        if m:
            story.append(Paragraph(
                _fmt(m.group(2)),
                styles["CN_Right" if m.group(1) == "right" else
                       "CN_Center" if m.group(1) == "center" else "CN_Left"]))
            continue

        story.append(Paragraph(_fmt(stripped), styles[st_body]))

    if citations:
        story.append(PageBreak())
        story.append(Paragraph("引用依据", styles["CN_Heading2"]))
        for i, c in enumerate(citations, 1):
            name = c.get("name") or c.get("doc_name") or ""
            article = c.get("article") or c.get("article_no") or ""
            status_text = c.get("status") or ""
            flag = "" if c.get("verified") else "（未通过核验，仅供核对）"
            story.append(Paragraph(
                _fmt(f"{i}. {name}{article}　{status_text}{flag}"), styles["CN_Body"]))

    doc = SimpleDocTemplate(
        str(target), pagesize=pagesizes.A4,
        rightMargin=(72), leftMargin=(72),
        topMargin=(72), bottomMargin=(72))
    if contract:
        doc.build(story,
                  onFirstPage=lambda c, d: _draw_footer_page_number(c, d, base_font),
                  onLaterPages=lambda c, d: _draw_footer_page_number(c, d, base_font))
    else:
        doc.build(story)


def _iter_docx_elements(doc: Any):
    """按文档真实顺序产出段落 / 表格对象。

    ⚠️ **必须与解析端 ``template_service._iter_body`` 产出同一串元素** ——
    回填是拿本函数的产出顺序去索引模板的 ``blocks``（``bi`` 游标），两边差一个，
    后面全体错位一位。而 ``_iter_body`` 是**只收非空段落**的（`if text.strip()`），
    所以这里也必须跳过空段落。

    不跳的后果是**静默且严重**的：实测 31 份带段落字段的模板、434 个字段里
    有 341 个被写进了**别的段落** —— 例如「申请人（签名）：＿＿＿」的值落到
    「4. 考勤记录、加班审批单；」那一行上，把证据清单条目覆盖掉，
    真正的签名栏还留着下划线。全程不报任何错。
    """
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for child in doc.element.body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            p = Paragraph(child, doc)
            if (p.text or "").strip():
                yield ("p", p)
        elif tag == "tbl":
            yield ("t", Table(child, doc))


def _row_cell_texts(row: Any) -> list[str]:
    """去重合并单元格后的单元格文本。"""
    seen: set[int] = set()
    out: list[str] = []
    for c in row.cells:
        if id(c._tc) in seen:
            continue
        seen.add(id(c._tc))
        out.append((c.text or "").strip())
    return out


def _checkbox_states(content: str) -> list[bool]:
    """按出现顺序取出预览正文里的勾选状态（☑=True / ☐=False）。

    预览和模板共用同一套渲染顺序，所以第 N 个方框在两边指的是同一个格子。
    """
    return [ch == "☑" for ch in re.findall(r"[☐☑]", content or "")]


def _checkbox_word(key: str, hint: str, *, had_colon: bool) -> bool:
    """判断这一格是不是"打勾用的选项"。

    ⚠ 必须与预览端 `render_markdown` 用的判定**完全一致**（就是 `ts._is_option`）。
    曾经这里额外放宽过（"是/否/有/无"一律给框），结果 Word 比预览多出两个方框，
    "第 N 个框"整体错位，预览里勾"男"会勾到 Word 的"女"上。
    """
    from backend.app.services import template_service as ts
    return ts._is_option(key, hint, had_colon=had_colon)


def _fill_cell_from_block(cell: Any, block: dict[str, Any], fills: dict[str, str],
                          bi: int, checks: list[bool] | None = None,
                          state: dict[str, int] | None = None) -> None:
    """根据模板块和已填字段，重填 Word 表格第二格。

    ``checks`` 是预览里方框的勾选序列；给了就**按出现顺序**覆盖模板自身的勾选，
    这样律师在预览里点一下，下载出来的 Word 那一格就是勾上的。
    """
    from backend.app.services import template_service as ts

    state = state if state is not None else {"i": 0}
    lines = block.get("lines") or [""]
    cell.text = ""
    for li, line in enumerate(lines):
        p = cell.paragraphs[0] if li == 0 else cell.add_paragraph()
        had_colon = bool(re.search(r"[：:]", line or ""))
        fragments = ts._split_line(line)
        # 先收集 cells，过滤掉被"出生日期"覆盖的"年/月/日"
        cells = []
        for k, (key, hint) in enumerate(fragments):
            if not key:
                continue
            fid = f"r{bi}.{li}.{k}"
            is_opt = _checkbox_word(key, hint, had_colon=had_colon)
            cells.append((fid, key, hint, is_opt))
        cells = ts._filter_redundant_date_cells(cells, fills)

        parts: list[str] = []
        for fid, key, hint, is_opt in cells:
            val = (fills.get(fid) or "").strip()
            if is_opt:
                words = ts._option_words(key, hint)
                for w in words:
                    # 预览里勾了哪个，就勾哪个：按方框出现顺序取状态
                    if checks is not None and state["i"] < len(checks):
                        checked = checks[state["i"]]
                        state["i"] += 1
                    else:
                        checked = bool(val) and (val == w or val == key)
                    parts.append(f"{'☑' if checked else '☐'} {w}")
            else:
                if val:
                    parts.append(f"{key}：{val}")
                elif hint and not ts._PLACEHOLDER_RE.search(hint):
                    parts.append(f"{key}：{hint}")
                else:
                    parts.append(f"{key}：")
        _run(p, "  ".join(parts))





def filled_template_doc(
    template_id: str, fills: dict[str, str],
    citations: list[dict[str, Any]] | None = None,
    checks: list[bool] | None = None,
) -> Any:
    """打开真模板 → 就地回填 → **返回 Document，不落盘**。

    这是「严格按照模板生成」的**唯一实现**：预览页与 Word 导出都调它，
    所以**页面上看到的和下载到的必然同源** —— 各渲染一次的话，两者会在
    细节处悄悄分叉（用户看到的是缩进的正文，拿到 Word 却顶格），
    而且这种分叉只在特定模板上出现，很难被发现。

    ``checks`` 来自预览正文的勾选序列，用于让结果与页面所见一致。

    **不再收 ``title`` 参数**：上一版带着它但函数体里一次都没读，
    是个会让人以为"标题会被写进去"的假参数。
    """
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    from backend.app.services import template_service as ts

    state = {"i": 0}
    tpl = ts.get_template(template_id)
    if tpl is None:
        raise ValueError(f"模板不存在：{template_id}")
    src = ts.template_dir() / tpl["file"]
    if not src.exists():
        raise FileNotFoundError(f"模板文件不存在：{src}")

    doc = Document(str(src))
    citations = citations or []
    blocks = tpl.get("blocks") or []
    # 清掉被"出生日期"覆盖的年/月/日拆分字段，避免 Word 里重复显示
    fields = ts.fields_for_prompt(tpl)
    fills = ts._dedupe_date_fills(fills, fields)
    # 出生日期/性别以身份证号为准。**预览与导出必须走同一个函数** ——
    # 各算各的话，网页上写着 1990年5月12日、下载的 Word 里是别的日子，
    # 而这种分叉只有把两份并排比对才看得出来。
    fills = ts.apply_identity(fills, fields, blocks)
    fills = ts.apply_amount_consistency(fills, fields, blocks)
    bi = 0
    drop: list[Any] = []       # 待删段落。**不能当场删**，见下。

    for kind, obj in _iter_docx_elements(doc):
        if bi >= len(blocks):
            break
        if kind == "p":
            block = blocks[bi]
            if block["t"] == "para":
                val = (fills.get(f"p{bi}") or "").strip()
                if val == ts.DELETE_MARK:
                    # 当事人没主张这一条 → 整段从文书里拿掉（预览侧同款处理见
                    # `render_markdown`）。⚠️ 只登记不删：这个 for 正走在
                    # `doc.element.body.iterchildren()` 上，边遍历边抽元素会让
                    # **后面的段落整片漏读** —— 历史上有一次就是这么把 434 个
                    # 字段里的 341 个写进了错误的段落，而且不报任何错。
                    drop.append(obj._p)
                else:
                    # 版式（落款右对齐）跟着模板块走，见 template_service._norm_align。
                    # **只改对齐、不清理文字**：模板里那串"手打空格"留着是无害的 ——
                    # 右对齐是整行推到右边，它只把行撑宽、可见文字照样齐右；
                    # 而 `obj.clear()` 重填会把模板给这一段的字体/字号一起抹掉，不值当。
                    align = block.get("align") or ""
                    if align:
                        obj.alignment = (WD_ALIGN_PARAGRAPH.RIGHT if align == "right"
                                         else WD_ALIGN_PARAGRAPH.CENTER)
                    if val:
                        obj.clear()
                        _run(obj, val)
            elif block["t"] == "hint":
                obj.clear()
            bi += 1
        else:  # table
            table = obj
            # 同一条不变量：解析端 `_iter_body` 是 `if any(cells)` 才收这一行，
            # 这里不滤就会多消费一个块，游标一样会错位一位。
            rows = [r for r in table.rows if any(_row_cell_texts(r))]
            if not rows:
                continue
            # 单列表（说明/分节标题）只消费 section 块，不动内容
            if all(len(_row_cell_texts(r)) <= 1 for r in rows):
                while bi < len(blocks) and blocks[bi]["t"] == "section":
                    bi += 1
                continue
            for r in rows:
                if bi >= len(blocks):
                    break
                if blocks[bi]["t"] == "section":
                    bi += 1
                    continue
                if blocks[bi]["t"] != "row":
                    break
                block = blocks[bi]
                bi += 1
                cells = _row_cell_texts(r)
                if len(cells) >= 2:
                    _fill_cell_from_block(r.cells[1], block, fills, bi - 1,
                                          checks=checks, state=state)
            while bi < len(blocks) and blocks[bi]["t"] == "section":
                bi += 1

    # 遍历结束，现在可以安全地抽段落了（`drop` 的注释在上面）
    for el in drop:
        el.getparent().remove(el)

    # 引用依据附在最后
    if citations:
        doc.add_page_break()
        p = doc.add_paragraph()
        _run(p, "引用依据", size=14, bold=True)
        for i, c in enumerate(citations, 1):
            name = c.get("name") or c.get("doc_name") or ""
            article = c.get("article") or c.get("article_no") or ""
            status_text = c.get("status") or ""
            flag = "" if c.get("verified") else "（未通过核验，仅供核对）"
            p = doc.add_paragraph()
            _run(p, f"{i}. {name}{article}　{status_text}{flag}", size=10)

    return doc


def _write_docx_from_template(
    target: Path, template_id: str, fills: dict[str, str],
    title: str, citations: list[dict[str, Any]],
    checks: list[bool] | None = None,
) -> None:
    """``filled_template_doc`` 的落盘版。

    ``title`` 保留但不用（见 ``filled_template_doc`` 的说明），只为不动调用点。
    """
    filled_template_doc(template_id, fills, citations, checks=checks).save(str(target))


def _safe_name(name: str) -> str:
    from backend.app.services.case_service import safe_filename

    return safe_filename(name) or "draft"


def _add_footer_page_number(doc: Any, *, size: int = 9) -> None:
    """在页脚加**自动编号**的居中页码（Word 的 `PAGE` 域）。

    真页脚，不是正文里的数字：律师增删几段之后页数会自己跟着变（用户 2026-09-29
    「页数是页脚，不是里面的文字」）。域码必须走裸 XML —— python-docx 没有
    域（field）的 API。

    `w:sz` 的单位是**半磅**，9pt 要写 18。
    """
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    footer = doc.sections[0].footer
    p = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), "PAGE")
    run = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    font = OxmlElement("w:rFonts")
    font.set(qn("w:ascii"), _CN_FONT)
    font.set(qn("w:eastAsia"), _CN_FONT)
    rpr.append(font)
    sz = OxmlElement("w:sz")
    sz.set(qn("w:val"), str(int(size * 2)))
    rpr.append(sz)
    run.append(rpr)
    txt = OxmlElement("w:t")
    txt.text = "1"                 # 域未更新时显示这个：Word 打开会自己算
    run.append(txt)
    fld.append(run)
    p._p.append(fld)


def _write_docx(target: Path, title: str, content: str,
                citations: list[dict[str, Any]], *, contract: bool = False) -> None:
    """写 docx。引用清单附在正文之后 —— 交付时法律依据必须随文。

    ⚠ **这里必须认 Markdown，不能一行一个 `add_paragraph`。**
    正文是 Markdown（模板渲染与模型草稿都是），原样塞进去律师打开会看到
    满屏 `#`、`**原告**`、`- 姓名：张三` —— 一份不能直接交给法院的文稿。
    所以这里只做一层"够用的 Markdown → Word"转换：标题/小节/加粗标签/缩进/引用注，
    正是模板渲染会产出的那几种写法（不追求通用）。

    ``contract=True``（**只有修订稿草稿**打开，见 :func:`export_draft`）按交付合同的
    版式来：A4 页、正文两端对齐、页脚自动编页。其余文书的输出一个字不变。
    """
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Mm, Pt

    doc = Document()
    _set_base_font(doc)

    if contract:
        # 同一份草稿导出 Word 是 Letter、导出 PDF 是 A4 —— 用户拿到的两份尺寸都不一样。
        # 合同的版式跟着 PDF 那条线走 A4。
        section = doc.sections[0]
        section.page_width = Mm(210)
        section.page_height = Mm(297)
        _add_footer_page_number(doc)

    if title and not content.lstrip().startswith("#"):
        doc.add_heading(title, level=1)

    for raw_line in (content or "").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if not stripped:
            doc.add_paragraph()
            continue
        if stripped in ("---", "***", "___"):
            doc.add_paragraph()          # 分隔线：Word 里用空段即可，不画字符线
            continue

        # `# 标题` / `## 副标题` / `### 小节`
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            level, text = len(m.group(1)), m.group(2)
            if level == 1:
                p = doc.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                _run(p, text, size=16, bold=True)
            elif level == 2:
                p = doc.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                _run(p, text, size=12)
            else:
                p = doc.add_paragraph()
                p.paragraph_format.space_before = Pt(8)
                _run(p, text, size=12, bold=True)
            continue

        # `> 说明`（免责声明等）
        if stripped.startswith(">"):
            p = doc.add_paragraph()
            _run(p, stripped.lstrip("> ").strip(), size=10, gray=True)
            continue

        # `- **标签**` 与 `  - 键：值`
        indent = len(line) - len(line.lstrip())
        m = re.match(r"^[-*]\s+\*\*(.+?)\*\*\s*$", stripped)
        if m:
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(6)
            _run(p, m.group(1), size=12, bold=True)
            continue
        m = re.match(r"^[-*]\s+(.*)$", stripped)
        if m:
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Pt(21 if indent >= 2 else 0)
            _emit_inline(p, m.group(1))
            continue

        # 套模板带出的**版式标记**（落款右对齐），见 template_service._norm_align。
        # `left` 是交付合同加的（落款签名区免缩进，见 `revise_service._contract_markdown`）；
        # 从前它匹配不上，会**原样写进 `<p align="left">甲方…</p>` 这串标记**。
        m = re.match(r'^<p align="(right|center|left)">(.*)</p>$', stripped)
        if m:
            p = doc.add_paragraph()
            p.alignment = (WD_ALIGN_PARAGRAPH.RIGHT if m.group(1) == "right"
                           else WD_ALIGN_PARAGRAPH.CENTER if m.group(1) == "center"
                           else WD_ALIGN_PARAGRAPH.LEFT)
            _emit_inline(p, m.group(2))
            continue

        # 普通段落：首行缩进两字（公文习惯）
        # ⚠️ 必须包 `Pt(...)`：python-docx 的长度属性要的是 EMU（`Length`），
        #    裸 int `24` 是 **24 EMU ≈ 0.0007 磅**，等于没缩进 ——
        #    这行注释一直写着"首行缩进两字"，但从来没生效过。
        p = doc.add_paragraph()
        p.paragraph_format.first_line_indent = Pt(24)
        if contract:
            # 交付合同正文两端对齐（原件就是齐的）；落款那种 `<p align="left">`
            # 在上面那个分支就 continue 掉了，走不到这里，所以不会误加缩进。
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        _emit_inline(p, stripped)

    if citations:
        doc.add_page_break()
        doc.add_heading("引用依据", level=2)
        for i, c in enumerate(citations, 1):
            name = c.get("name") or c.get("doc_name") or ""
            article = c.get("article") or c.get("article_no") or ""
            status_text = c.get("status") or ""
            flag = "" if c.get("verified") else "（未通过核验，仅供核对）"
            p = doc.add_paragraph()
            _run(p, f"{i}. {name}{article}　{status_text}{flag}", size=10)
    doc.save(str(target))


def _split_bold(text: str) -> list[tuple[str, bool]]:
    """把 `前**粗**后` 拆成 [(段, 是否加粗)]。（保留给老调用方）"""
    return [(seg, style == "b") for seg, style in _split_inline(text)]


def _emit_inline(paragraph: Any, text: str, *, size: int = 12) -> None:
    """按行内标记分段写入段落：加粗 / 删除线（原文）/ 蓝色下划线（新增）。"""
    for seg, style in _split_inline(text):
        _run(paragraph, seg, size=size, bold=(style == "b"),
             strike=(style == "strike"), ins=(style == "ins"))


def _split_inline(text: str) -> list[tuple[str, str]]:
    """拆行内标记：返回 [(文字, 样式)]，样式取 ``""`` / ``"b"`` / ``"strike"`` / ``"ins"``。

    为什么要有 ``strike`` 与 ``ins``：合同修订版必须能显出"哪里被去掉、哪里是新增"，
    而 Markdown 里没有通用写法 —— 这里约定 ``~~原文~~`` 与 ``++新增++``。
    两种标记都会被导出层（_write_docx / _write_pdf）翻成真实格式。
    """
    pattern = re.compile(r"\*\*(.+?)\*\*|~~(.+?)~~|\+\+(.+?)\+\+", re.S)
    out: list[tuple[str, str]] = []
    pos = 0
    for m in pattern.finditer(text or ""):
        if m.start() > pos:
            out.append((text[pos:m.start()], ""))
        if m.group(1) is not None:
            out.append((m.group(1), "b"))
        elif m.group(2) is not None:
            out.append((m.group(2), "strike"))
        else:
            out.append((m.group(3), "ins"))
        pos = m.end()
    if pos < len(text or ""):
        out.append((text[pos:], ""))
    return out or [(text or "", "")]


_CN_FONT = "宋体"


def _set_base_font(doc: Any) -> None:
    """中文公文惯用字体：宋体（Windows/WPS 基本都有，避免"仿宋_GB2312"缺失时整页空白）。"""
    from docx.oxml.ns import qn
    from docx.shared import Pt

    style = doc.styles["Normal"]
    style.font.name = _CN_FONT
    style.font.size = Pt(12)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), _CN_FONT)


def _run(paragraph: Any, text: str, *, size: int = 12, bold: bool = False,
         gray: bool = False, strike: bool = False, ins: bool = False) -> None:
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    if not text:
        return
    run = paragraph.add_run(text)
    run.font.size = Pt(size)
    run.bold = bold
    run.font.name = _CN_FONT
    run._element.rPr.rFonts.set(qn("w:eastAsia"), _CN_FONT)
    if gray:
        run.font.color.rgb = RGBColor(0x80, 0x80, 0x80)
    # 修订标记：删除线 = 被去掉的原文；蓝色下划线 = AI 新增/替换的建议
    if strike:
        run.font.strike = True
        run.font.color.rgb = RGBColor(0x80, 0x80, 0x80)
    if ins:
        run.font.underline = True
        run.font.color.rgb = RGBColor(0x1F, 0x4F, 0xC0)


@router.get("/meta/doc-types", summary="支持的文书类型")
def draft_doc_types() -> dict[str, Any]:
    """草稿支持的文书类型。数据源是 ``draft_service.DOC_TYPES``，不另抄一份。"""
    from backend.agents.nodes.draft_node import DRAFT_LABELS

    return {
        "items": [
            {"doc_type": k, "name": DRAFT_LABELS.get(k, k)}
            for k in draft_service.DOC_TYPES
        ]
    }
