"""对话与任务发起接口：智能问答、合同审查、文书起草、类案检索、法规检索。

双端入口是同一个 ``orchestrator.execute``（方案 12.4「同一后端、同一 Agent 编排」），
所以这里**不按意图分叉业务逻辑**，只把不同界面的入参映射成同一个 ``RunRequest``：

- ``POST /chat``          —— 通用问答（意图由 IntentNode 判定，也可显式指定）
- ``POST /chat/stream``   —— 同上，但以 SSE 推送逐节点进度
- ``POST /contract/review`` —— 合同审查。要求必须有材料（正文或文件），
                              否则会退化成"泛泛的合同风险清单"，看起来像结果但其实是模板。
- ``POST /doc/draft``     —— 文书起草。``doc_type`` 决定文书类型。
- ``POST /search/statute`` / ``POST /search/case`` —— 只检索、不出报告，
  走工具通道（保留配额、审计、Trace 记账），不启动完整 Agent 流程。

**关于 SSE 断开**：客户端断开时**不取消任务**。一次合同审查要跑 6~9 个节点、
可能几分钟；律师换个页面/网络抖一下就丢掉整次分析是不可接受的。
任务继续跑完并落库，律师在「任务中心」能看到结果。这一点在断开时会写日志，
不能让人误以为"关掉页面 = 任务停了"。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, File, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.app.api.deps import Meta, RequireUser
from backend.app.services.orchestrator import RunRequest, orchestrator
from backend.app.services.evolution_service import evolution_service
from backend.app.services.expert_registry import expert_registry
from backend.agents.harness.router import INTENT_ROUTES

logger = logging.getLogger("legal_agent.api.chat")

router = APIRouter(tags=["对话与任务"])

# 后台任务的强引用集合。asyncio 只持弱引用，不这么存着，
# "客户端断开后任务继续跑"就会变成"任务被 GC 掉、悄悄没了"。
_BACKGROUND: set[asyncio.Task] = set()

MAX_QUERY_CHARS = 10_000
MAX_MATERIAL_CHARS = 500_000


def _keep(task: asyncio.Task) -> None:
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


# SSE 响应头。**两处（Agent 链路与纯 RAG 链路）必须一致**：
# Nginx 反代下不关缓冲，SSE 会被攒成一坨再吐出来，界面上就是"卡住半天然后一次性出现"。
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _sse_frame(event: str, data: Any) -> str:
    """一条 SSE 帧。``ensure_ascii=False`` 是为了中文可读（调试时能直接看日志）。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


# --------------------------------------------------------------------------- 模型
class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=MAX_QUERY_CHARS, description="用户提问")
    case_id: str = Field("", description="关联案件编号（可空）")
    intent: str = Field("", description="显式指定意图；留空则由 Agent 判定")
    material_text: str = Field("", max_length=MAX_MATERIAL_CHARS, description="随附正文")
    client_role: str = Field(
        "",
        max_length=32,
        description="审查立场（甲方/乙方/出租人/劳动者……，可空=中立）。只对合同审查有效，"
                    "决定风险点往哪边判、修改建议替谁争取。**留空不等于没传** —— "
                    "ReviewNode 会退回从 query 里正则抓，那是给纯文字提问用的老路；"
                    "界面上选立场必须走这个字段，否则选了不生效且不报错。",
    )
    template_text: str = Field(
        "",
        max_length=MAX_MATERIAL_CHARS,
        description="范本合同正文（**比对基准，可选**）。只对合同审查有效："
                    "有它时 ReviewNode 会以它为基准逐条比对，缺失条款与改写条款都对着它判。"
                    "注意它**不算「有材料」** —— 只有范本、没有待审合同，仍然是 400。",
    )
    material_paths: list[str] = Field(default_factory=list, description="服务端已有材料路径")
    material_ids: list[int] = Field(
        default_factory=list,
        description="案件材料 id 列表（**Web/EXE 端应当用这个，而不是 material_paths**）",
    )
    doc_type: str = Field("unknown", description="材料类型：contract / litigation / consult / evidence ...")
    use_graph: bool = Field(True, description="是否走 LangGraph 编排（关闭则按节点顺序直跑）")
    enqueue: bool = Field(
        False,
        description="true = 只入队，立刻返回 task_id（由 worker 执行）；"
                    "false = 在本次请求里跑完再返回（默认，短任务更快）",
    )
    no_cache: bool = Field(
        False,
        description="true = 跳过相似问题缓存，强制重跑完整链路。"
                    "对复用的结论有疑问时用它 —— 否则换个说法再问，很可能又命中同一条缓存。",
    )


class DraftRequest(BaseModel):
    query: str = Field(..., min_length=1, description="起草要求")
    doc_type: str = Field("complaint", description="文书类型：complaint / defense / opinion / revision")
    case_id: str = ""
    enqueue: bool = Field(False, description="true = 只入队，立刻返回 task_id（由 worker 执行）")
    no_cache: bool = Field(False, description="true = 跳过相似问题缓存，强制重跑完整链路")


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, description="检索式")
    top_k: int = Field(6, ge=1, le=50, description="返回条数")
    case_id: str = ""


class RagTurn(BaseModel):
    """一轮历史对话。**只用来改写检索式、消解指代，不作为证据**。"""

    q: str = Field("", max_length=1000, description="该轮提问")
    a: str = Field("", max_length=2000, description="该轮回答（服务端还会再截断）")


class RagAskRequest(BaseModel):
    """纯 RAG 问答入参（AI 助手页专用）。"""

    query: str = Field(..., min_length=1, max_length=MAX_QUERY_CHARS, description="本轮提问")
    case_id: str = Field("", description="关联案件编号（可空，仅用于审计）")
    history: list[RagTurn] = Field(
        default_factory=list, max_length=4,
        description="最近几轮对话。**上限 4 条（2 轮）**：多轮上下文不设限会让 Prompt 线性膨胀，"
                    "最后要么超上下文、要么把参考资料挤出模型注意力（表现为越聊越不引法条）。",
    )
    top_k: int = Field(8, ge=1, le=20, description="法条检索条数")
    conversation_id: str = Field(
        "", max_length=48,
        description="继续某段已有会话时带上（取自上一轮 done 帧）。留空 = 开一段新的。"
                    "传了但查不到 / 无权限 → 当作新会话，**不报错**。",
    )


def _has_material(payload: ChatRequest) -> bool:
    """三种给材料的方式任意一种即可：材料 id / 服务端路径 / 粘贴正文。"""
    return bool((payload.material_text or "").strip()
                or payload.material_paths
                or payload.material_ids)


def _resolve_material_paths(payload: ChatRequest, user: Any) -> list[str]:
    """把 ``material_ids`` 解析成服务端路径。

    **为什么不让客户端传路径。** ``stored_path`` 是服务端绝对路径，接口
    刻意不外泄（见 ``case_service.material_to_dict`` 的 ``with_path``）——
    内网路径会把目录结构暴露出去。但合同审查又必须能消费已上传的材料，
    于是只有两条路：把路径回传给客户端再收回来（等于承认"路径由客户端指定"，
    这是任意文件读取的入口），或者只收 id、由服务端查库解析。这里是后者。

    逐份走 ``case_service.get_material``，**权限校验在服务层**——
    不能只用 ``INTERNAL`` 拿路径就完事，那样任何人猜到 id 就能读别人的合同。

    单次最多 10 份：``IntakeNode`` 本来也只用前 5 份（防止步数被材料耗光），
    这里放宽一倍是留给将来的，再多没有意义。
    """
    if not payload.material_ids:
        return []

    from backend.app.services import case_service

    paths: list[str] = []
    for mid in list(payload.material_ids)[:10]:
        try:
            # 路径由服务层解析，权限校验也在里面（``case_service.material_path``）。
            # 不能只用 INTERNAL 拿路径就完事 —— 那样任何人猜到 id 就能读别人的合同。
            paths.append(case_service.material_path(int(mid), user))
        except case_service.CaseForbidden as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except case_service.CaseNotFound as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return paths


def _to_run_request(payload: ChatRequest, user: Any, meta: Meta, *,
                    intent: str = "", doc_type: str = "", top_k: int = 0) -> RunRequest:
    material_paths = list(payload.material_paths) + _resolve_material_paths(payload, user)
    return RunRequest(
        query=payload.query,
        case_id=payload.case_id,
        user_id=str(user.id),
        team_id=str(user.team_id or ""),
        material_text=payload.material_text,
        template_text=payload.template_text,
        client_role=payload.client_role,
        material_paths=material_paths,
        doc_type=doc_type or payload.doc_type or "unknown",
        # ``intent`` 现在**真的**会钉住路由（经 execute → state.intent_override
        # → IntentNode 优先采用）。以前它只进任务表，传什么都白传。
        intent=intent or payload.intent or "",
        top_k=top_k,
        use_graph=payload.use_graph,
        no_cache=payload.no_cache,
        client=meta.client,
        username=user.username,
        ip=meta.ip,
    )


def _enqueue(req: RunRequest, user: Any, meta: Meta, *,
             kind: str = "chat") -> dict[str, Any]:
    """把请求交给 worker 执行，立刻返回。

    **材料路径在这里就解析好、写进快照**（``_to_run_request`` 已经做了）。
    不能留到 worker 侧再解析 ``material_ids``：入队到执行之间可能隔几分钟，
    那时再走一次 ``case_service.material_path`` 会用到**已经过期的权限判断** ——
    律师在这段时间里把案件移交出去，worker 仍然会按旧权限把材料读出来。

    ``task_id`` 是这里生成的，并且**写进请求快照**：worker 认领的那一行
    就是它（见 ``RunRequest.task_id``），否则 worker 跑出来的结果会落在
    另一条新建的任务上，而被认领的那行永远停在 ``running``。
    """
    from backend.app.services import task_queue
    from backend.agents.state import new_task_id

    req.task_id = new_task_id()
    result = task_queue.enqueue(
        req.to_payload(),
        task_id=req.task_id,
        query=req.query,
        case_id=req.case_id,
        kind=kind,
        intent=req.intent,
        user_id=user.id,
        team_id=user.team_id,
        source_client=meta.client,
    )

    if not result.get("queued"):
        # 入队失败（写库都没成功）**不假装成功**：返回 503 而不是
        # 200 + 一个永远跑不起来的 task_id —— 后者会让律师一直刷新任务中心。
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=result.get("reason") or "任务入队失败",
        )

    return {
        "ok": True,
        "queued": True,
        "task_id": req.task_id,
        "status": "pending",
        "intent": req.intent or "auto",
        "notified": bool(result.get("notified")),
        "notify_note": result.get("notify_note", ""),
        # 说清"现在什么都没有"，否则前端会以为返回体里应该有 report 字段。
        "message": "任务已排队，尚未开始。进度与结果请看任务中心 "
                   f"（GET /tasks/{req.task_id}），或订阅 /tasks 的 WebSocket。",
        "poll": f"/tasks/{req.task_id}",
    }


# --------------------------------------------------------------------------- 意图
@router.get("/chat/intents", summary="可用意图与节点路线")
def intents() -> dict[str, Any]:
    """列出意图、中文标签与各自的节点路线。

    给前端做"强制指定意图"的下拉。数据源就是 Router 的路由表本身，
    **不另抄一份** —— 抄的那份一定会和实际路由分叉，然后界面显示的和跑的不是一回事。

    ⛔ ``off_topic`` / ``quick_answer`` **不给律师手动选**：它们是系统按问题复杂度
    自动降档的结果，不是一种"业务类型"。放进下拉只会被误选 ——
    律师点"非法律问题"提交一份合同审查请求，得到的答复与他的诉求完全无关，
    而这在他看来就是"系统坏了"。
    """
    return {
        "items": [
            {
                "intent": key,
                "label": value.get("label", key),
                "nodes": list(value.get("nodes") or []),
                "description": value.get("description", ""),
            }
            for key, value in INTENT_ROUTES.items()
            if not value.get("direct") and key != "quick_answer"
        ]
    }


# --------------------------------------------------------------------------- 同步
@router.post("/chat", summary="智能问答 / 发起任务")
async def chat(payload: ChatRequest, user: RequireUser, meta: Meta) -> dict[str, Any]:
    """跑一次完整流程。

    两种模式，由 ``enqueue`` 选：

    - ``enqueue=false``（默认）—— **等它跑完**再返回。适合短任务
      （问答、法规检索）：结果就在响应体里，前端不用再轮询一次。
    - ``enqueue=true`` —— 只入队，立刻返回 ``task_id``。适合长任务
      （合同审查）：HTTP 连接不会挂着几分钟，网关也不会掐断它；
      即使这只客户端关掉了，worker 依然在跑。

    ``q=`` 里的 ``use_graph`` 也一并透传 —— 关闭走的是顺序直跑，
    两条路的执行器不同，排查"结果不一致"时这是第一个要看的开关。
    """
    req = _to_run_request(payload, user, meta)
    if payload.enqueue:
        return _enqueue(req, user, meta)

    outcome = await orchestrator.execute(req)
    _log_usage(user, expert_id="", intent=outcome.state.get("intent") or "",
               confidence=outcome.state.get("confidence") or None,
               elapsed_ms=outcome.elapsed_ms)
    _observe_evolution(outcome)
    body = outcome.to_dict(with_trace=False)
    body["task_id"] = outcome.task_id
    if not outcome.ok:
        # **不返回 5xx**：流程跑了一半也有交付物（已检索到的法条、已出的部分报告），
        # 而且 task_id 已经在库里，律师可以去任务中心看详情。
        # 返回 500 会让前端只看到一个错误弹窗，把已有产出全丢掉了。
        body["warning"] = f"流程未完整执行：{outcome.error}"
    return body


def _public(items: Any) -> list[dict[str, Any]]:
    """放进响应体的证据明细（法条 / 类案 / 引用 / 知识库）：**剥掉 ``source_path``**。

    ``source_path`` 是语料文件在**服务端**的绝对路径
    （``…\\data\\raw\\劳动法\\中华人民共和国劳动法.pdf``），没有任何调用方用它，
    但它会把服务器的目录结构带出去 —— 律师把接口结果贴给客户、或者把响应日志
    发出去的时候，泄的是本机的目录布局。

    这件事在 ``task_service._strip_source_paths`` / ``citation_service`` /
    ``rag_answer`` 三处都已经堵过，**SSE 这条是漏的**：``citations`` 一直带着它
    （见 ``harness/validator.py`` 的两个 ``_build_*_citation``），只是没人查过。

    复用同一份实现、按值递归剥，别另写一套"删这几个键"：漏一个键它就一路带到前端，
    而**半修比不修更糟** —— 问题还在，却没人再去找了。
    """
    from backend.app.services.task_service import _strip_source_paths

    return list(_strip_source_paths(list(items or [])))


# 关键决策在 Trace 里的标记。**两个字面量只在这里写一次**：
# 写方是 ``graph._controller_review`` / ``controller._act``（``node="Controller"``、
# ``event="decision"``），读方是下面这个函数与前端那四个 ``onFrame``。
# 它们之间没有类型约束，改名不会有任何一处报错 —— 只会静默地不再推送。
CONTROLLER_NODE = "Controller"
CONTROLLER_EVENT = "decision"


def frame_of(ev: dict[str, Any]) -> dict[str, Any]:
    """把一条 Trace 事件分帧（约束 4 的"过程中"层）。

    控制器那一句**关键决策**单独走 ``event=decision``，其余一律 ``event=trace``。

    **为什么在服务端分流，而不是让前端从 trace 里自己挑**：trace 帧是**节点级**的
    （"StatuteNode · 法规检索"），而 2026-09-29 已经把执行轨迹面板整块摘掉，
    前端再渲染 trace 就等于把那个面板请回来。这里只把控制器那一句发出去，
    节点级细节一个字都不推。

    ⚠ 后端照旧**全量落 trace**（控制器写的是同一条 tracer 流）。分流只决定
    "推不推给浏览器"，审计不丢 —— 想看细节走 ``GET /tasks/{id}/trace``。

    单独成函数是为了**可被单测触达**。它留在 ``_sse_stream`` 的闭包里时，
    "决策帧到底发没发"只能端到端跑一次才知道 —— 而它坏掉的表现是界面上什么都不显示，
    与"这次压根没裁决"长得一模一样（对比 ``orchestrator._cache_wanted`` 的同一理由）。
    """
    if ev.get("node") == CONTROLLER_NODE and ev.get("event") == CONTROLLER_EVENT:
        return {"event": "decision", "data": {"text": ev.get("detail") or ""}}
    return {"event": "trace", "data": ev}


def _log_usage(user: Any, *, expert_id: str = "", intent: str = "",
               confidence: float | None = None, elapsed_ms: int = 0) -> None:
    """监控中心：写一条专家 / 技能使用记录（``la_usage_log``）。**失败仅记日志，绝不抛**。

    专家对话（``/agents/assistant/stream``）带 ``expert_id``；AI 助手 / 产品入口
    统一记录意图，由 ``skill_for_intent`` 反查命中的技能；未命中（普通提问）也记
    一行、技能列留空。纯 RAG 链路（``/chat/rag*``）不跑意图分类，记为 ``rag``。

    轻量留痕，与任务中心解耦：**不写 ``la_task``**（RAG 链路有测试锁定该契约），
    不参与相似问题缓存；落库失败不影响作答。
    """
    from backend.app.db.models import UsageLog
    from backend.app.db.session import session_scope
    from backend.skills import skill_for_intent

    try:
        skill = skill_for_intent(intent or "")
        with session_scope() as db:
            db.add(UsageLog(
                user_id=user.id,
                expert_id=expert_id or "",
                skill_id=skill.id if skill else "",
                intent=intent or "",
                confidence=confidence,
                elapsed_ms=int(elapsed_ms or 0),
            ))
    except Exception:  # noqa: BLE001 - 使用留痕失败绝不能影响作答
        logger.exception("监控中心使用记录落库失败（答案照常返回）")


def _observe_evolution(outcome: Any) -> None:
    """监控中心：把一次 Agent 运行折算成 evolution 观测落 state.json。

    与 ``_log_usage`` 同一语义：**留痕失败绝不影响作答** —— 观测只是监控
    中心的输入之一，写不进去就记日志，绝不把异常抛给调用方。
    """
    try:
        evolution_service.observe_outcome(
            outcome.state,
            elapsed_ms=outcome.elapsed_ms,
            ok=bool(outcome.ok),
        )
    except Exception:  # noqa: BLE001 - 观测失败不能影响作答
        logger.exception("evolution 观测落盘失败（答案照常返回）")


async def _sse_stream(req: RunRequest, user: Any, meta: Meta, *,
                      before_done: Any = None, usage: dict[str, Any] | None = None):
    """SSE 事件流。

    **先给 task_id 再开跑。** 客户端要拿它做两件事：断开后回到任务中心
    找回结果，以及把界面上的进度与库里的那条任务对上号。
    不预先分配的话，task_id 只在 ``done`` 事件里才出现 ——
    而任务跑到一半客户端断开了，就再也拿不到它。

    ``before_done``：在推 ``done`` 帧**之前**对 body 做收尾（AI 助手用它落会话、
    补一条 ``conversation_id``）。位置是硬的 —— 前端收到 ``done`` 会立刻刷
    历史会话列表，落库反过来做的话，刚问完的这条不会出现在侧栏里，
    看起来像"没保存"。与 ``/chat/rag/stream`` 同一套顺序，理由也一样。

    它抛异常**不影响作答**：答案这时已经生成完了，抛出去只会让律师看到
    "答完了但屏幕上什么都没有"。
    """
    from backend.agents.state import new_task_id

    if not req.task_id:
        req.task_id = new_task_id()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def _on_event(ev: dict[str, Any]) -> None:
        # 这个回调跑在节点调用栈里，必须快且不抛异常
        try:
            queue.put_nowait(frame_of(ev))
        except Exception:  # noqa: BLE001
            pass

    async def _run() -> None:
        try:
            outcome = await orchestrator.execute(req, on_event=_on_event)
            # 监控中心：专家 / 技能使用留痕（expert_id 由调用方在 ``usage`` 里传入，
            # 意图用执行结果里的真实值 —— 不写死入口参数，避免"实际跑的和记的不一致"）。
            if usage is not None:
                _log_usage(
                    user,
                    expert_id=usage.get("expert_id", ""),
                    intent=outcome.state.get("intent") or "",
                    confidence=outcome.state.get("confidence") or None,
                    elapsed_ms=outcome.elapsed_ms,
                )
                _observe_evolution(outcome)
            body = outcome.to_dict(with_trace=False)
            # **必须补上结构化结果**：``to_dict`` 只给汇总（counts/confidence/report），
            # 不含 risk_points / missing_clauses / revision_draft。非流式的
            # ``/contract/review`` 是手动补的；流式这里不补，前端「风险点」「建议补充
            # 条款」「修改对照稿」三个页签就永远是 0/空 —— 合同审查最核心的产出
            # 恰好就在这三个里。
            st = outcome.state
            body["risk_points"] = list(st.get("risk_points") or [])
            body["missing_clauses"] = list(st.get("missing_clauses") or [])
            body["revision_draft"] = st.get("revision_draft") or ""
            body["clauses"] = len(st.get("clauses") or [])
            # 双模型交叉复核摘要 + 人工抽查标记（ReviewNode 产出；没配复审模型时缺省，
            # 前端据此不渲染那一块 —— 别让"没复审"装成"复审过了"）。
            body["second_pass"] = st.get("second_pass") or {}
            body["spot_check"] = st.get("spot_check") or {}
            # 争议焦点 / 应对策略（AnalysisNode 产出）同样只在 state 里，必须显式带上，
            # 否则前端无法渲染「争议焦点」「应对策略」两块。
            body["issues"] = list(st.get("issues") or [])
            body["strategy"] = list(st.get("strategy") or [])
            # 事实 / 当事人（IntakeNode 产出）：案件分析页的「事实与证据」区块要用。
            body["facts"] = list(st.get("facts") or [])
            body["parties"] = list(st.get("parties") or [])
            # 文书草稿正文（DraftNode 产出）：文书起草页要直接拿到可编辑的初稿。
            body["draft"] = st.get("draft") or ""
            # 逐条引用（ValidatorNode 核过的）：界面要把"依据 + 是否通过核验"摊开给
            # 律师看，只给 citation_summary 的计数做不到这件事。
            body["citations"] = _public(st.get("citations"))
            # 检索到的法条与类案**明细**。``to_dict`` 只给 ``counts`` 里的个数，
            # 而类案检索页要渲染的就是那张命中列表 —— 不给明细，页面就只剩
            # 一句"命中 N 件"却一件都列不出来。
            #
            # 这里给的是**经 ValidatorNode 核过**的那一份（``verified`` 字段已回填），
            # 而不是工具通道的原始命中：页面上那个"未通过核验"的角标读的就是它。
            body["statutes"] = _public(st.get("statutes"))
            body["cases"] = _public(st.get("cases"))
            body["knowledges"] = _public(st.get("knowledges"))
            if before_done is not None:
                try:
                    before_done(body)
                except Exception:  # noqa: BLE001 - 收尾失败绝不能影响作答
                    logger.exception("done 帧前的收尾处理失败（结果照常返回）")
            await queue.put({"event": "done", "data": body})
        except Exception as exc:  # noqa: BLE001
            logger.exception("SSE 任务异常：%s", exc)
            await queue.put({"event": "error", "data": {"error": f"{type(exc).__name__}: {exc}"}})

    task = asyncio.create_task(_run())
    _keep(task)

    async def _gen():
        # 第一条就给 task_id，客户端断线后能凭它回任务中心找回结果。
        yield _sse_frame("meta", {"client": meta.client, "username": user.username,
                                  "intent": req.intent or "auto", "task_id": req.task_id})
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    # 心跳：长静默会让某些反代（Nginx 默认 60s）掐掉连接
                    yield ": keep-alive\n\n"
                    continue
                yield _sse_frame(msg["event"], msg["data"])
                if msg["event"] in ("done", "error"):
                    break
        except asyncio.CancelledError:
            # 客户端断开。**不取消任务**（见模块 docstring）：
            # 让它在后台跑完并落库，律师能在任务中心找回结果。
            logger.info("SSE 客户端断开，任务继续在后台执行：%s", req.query[:60])
            raise

    return StreamingResponse(_gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.post("/chat/stream", summary="智能问答（SSE 流式进度）")
async def chat_stream(payload: ChatRequest, user: RequireUser, meta: Meta):
    """与 ``/chat`` 同参数，但逐条推送 Trace 事件，结束推 ``done``。

    事件类型：``meta`` / ``trace`` / ``done`` / ``error``；
    以 ``: keep-alive`` 注释行维持连接。
    """
    return await _sse_stream(_to_run_request(payload, user, meta), user, meta,
                             usage={"expert_id": ""})


# --------------------------------------------------------------------------- 产品 Agent 入口
# 侧边栏对外的 4 个入口（登记在 ``harness.router.PRODUCT_AGENTS``）。
# 这里放**不撞已有端点**的两个：AI 助手与类案检索。
# 合同审查用既有的 ``/contract/review/stream``（本就钉着 intent="contract_review"），
# 文书中心用 ``/doc/compose``（原地改造，见下）。
#
# ⚠ 「4 个入口都走编排」这件事**不能靠命名约定**：``/agents/*`` 是给前端调的新路径，
#    而 ``/chat``、``/search/case`` 这些老路径**一个都没删**。
#    收敛发生在前端（``web/app.js`` 改调新端点），老路径留着给脚本与回归测试用。
#    两套路径指向同一条编排链路，才是"老路径没被放弃"的意思。
class AgentAskRequest(BaseModel):
    """AI 助手 Agent 入口的入参。

    **刻意不复用 ``RagAskRequest``。** 那份里 ``top_k`` 是"法条检索条数"、
    ``history`` 是交给检索式改写器的；这条链路上前者不生效、后者只用来消解指代。
    照抄一份形状的后果是调用方以为"我传的 top_k 生效了" ——
    参数名一样而语义不同的接口，比少一个参数难查得多。
    """

    query: str = Field(..., min_length=1, max_length=MAX_QUERY_CHARS, description="本轮提问")
    case_id: str = Field("", description="关联案件编号（可空，仅用于审计）")
    conversation_id: str = Field(
        "", max_length=48,
        description="继续某段已有会话时带上（取自上一轮 done 帧）。留空 = 开一段新的。",
    )
    history: list[RagTurn] = Field(
        default_factory=list, max_length=4,
        description="最近几轮对话。**只用于消解指代**（把上一轮问题并进本轮检索文本），"
                    "不作为证据，也不参与检索式改写。",
    )
    expert_id: str = Field(
        "", max_length=64,
        description="专家 id：从专家页点击进入对话时由前端传入；"
                    "加载该专家的人设描述（description）作为本次对话的 system 人设前缀。"
                    "留空 = 普通 AI 助手（保持原逻辑）。",
    )


async def _agent_ask_stream(payload: AgentAskRequest, user: Any, meta: Meta):
    """AI 助手 Agent 的流式实现（两个入口共用：``/agents/assistant/stream`` 与它的非流式兄弟）。

    追问处理：把**上一轮的问题**（不含回答）并进本轮检索文本。
    这条链路上没有 ``/chat/rag`` 那个检索式改写器，不补上文的话
    "那竞业限制呢"会被当成一个独立问题去判意图 —— 分类器看不出它在问什么，
    检索式也检索不到东西，而界面上只会显示"A 类案没找到"。
    只并提问不并回答：回答动辄两千字，并进去会把本条问题的关键词稀释掉。
    """
    query = payload.query
    prev = [(t.q or "").strip() for t in payload.history if (t.q or "").strip()]
    if prev:
        query = f"{query}\n\n（上一轮提问，供消解指代用：{prev[-1][:200]}）"

    # 专家人设注入：只负责把 ``description`` 挂到 RunRequest 上，
    # 由编排器写进 State，生成节点按「人设 → 基础纪律提示词」拼接。
    # 专家不存在或未传时是空串 —— 行为与普通 AI 助手逐字一致。
    persona = ""
    enhance: dict[str, Any] = {}
    if payload.expert_id:
        persona = expert_registry.persona_for(payload.expert_id)
        enhance = expert_registry.enhance_for(payload.expert_id)
        if persona or enhance:
            logger.info("AI 助手按专家运行（persona=%s enhance=%s）：%s",
                        bool(persona), list(enhance), payload.expert_id)

    req = RunRequest(
        query=query,
        case_id=payload.case_id,
        user_id=str(user.id),
        team_id=str(user.team_id or ""),
        # **不钉意图**：AI 助手是唯一一个"问什么都可能"的入口，钉死了等于把
        # 分类器关掉。走几步由问题复杂度定（见 ``INTENT_ROUTES`` 与 IntentNode
        # 的降档判据）—— 问"试用期最长几个月"跑 3 个节点，问一整个案情跑 7 个。
        #
        # 交付物形态：助手页要的是**对话正文＋报告尾巴**（四节问答，好读），
        # 而不是七节报告骨架。另外三个入口（类案检索/合同审查/文书中心）不传，
        # 保持 report —— 它们的页面各自依赖报告骨架里的区块（见 LegalState.answer_style）。
        answer_style="conversational",
        expert_persona=persona,
        # 专家增强配置（定向检索领域 / 检索增强词 / 专属回答模板）。
        # 与 persona 同源：专家未配置时为空，行为与普通 AI 助手逐字一致。
        expert_domains=list(enhance.get("domains") or []),
        expert_hints=list(enhance.get("retrieval_hints") or []),
        expert_template=str(enhance.get("answer_template") or ""),
        client=meta.client,
        username=user.username,
        ip=meta.ip,
    )
    return await _sse_stream(
        req, user, meta,
        before_done=lambda body: body.update(
            conversation_id=_persist_agent_turn(user, payload, body)),
        usage={"expert_id": payload.expert_id or ""},
    )


def _persist_agent_turn(user: Any, payload: AgentAskRequest, body: dict[str, Any]) -> str:
    """把这一轮落进 AI 助手自己的会话表，返回会话号；**失败返回空串，绝不抛**。

    **为什么必须先把 ``report`` 归一化成 ``answer``。** 会话重放读的是
    ``payload.answer``（``web/app.js`` 的 ``formatRagAnswer``）。这条链路的交付物
    叫 ``report``（报告模板：引用清单 / 证据缺口 / 综合置信度三节）。
    不归一化就落库的话，历史会话栏点开来是**一片空白** ——
    库里明明有这一轮，界面上什么都没有，而且不会有任何一处报错。

    与 :func:`_persist_rag_turn` 同样吞掉所有异常：答案这时已经生成完了，
    抛出去只会让律师看到"答完了但屏幕上什么都没有"。历史缺一轮可以接受，答案是。
    """
    from backend.app.services import chat_history_service

    body["answer"] = body.get("report") or ""
    try:
        return chat_history_service.append_turn(
            user=user, query=payload.query, payload=body,
            conversation_id=payload.conversation_id, case_id=payload.case_id,
        )["conversation_id"]
    except Exception:  # noqa: BLE001 - 落库失败绝不能影响作答
        logger.exception("AI 助手会话落库失败（答案照常返回）")
        return ""


@router.post("/agents/assistant/stream", summary="AI 助手 Agent（完整编排 + 会话留痕）")
async def agent_assistant_stream(payload: AgentAskRequest, user: RequireUser, meta: Meta):
    """AI 助手页的入口：**跑完整编排**，并把这一轮落进 AI 助手自己的会话表。

    与 ``/chat/rag/stream`` 的区别只有一条，但是关键的那条：**这里有 Agent。**
    意图判定、法条检索、（按需）类案检索、本所指引、争议焦点、引用终审、报告汇总
    依次跑过，走几步由问题复杂度定。代价是慢一些、且会往任务表写一条记录
    （任务中心里能看到）—— 换来的是每条结论都附引用清单与证据缺口，
    "这句话出自哪条法条、核过没有"在页面上有答案。

    ``done`` 帧的 ``report`` 是报告模板正文，同时归一化了一份到 ``answer``，
    前端两套渲染都能用（见 :func:`_persist_agent_turn`）。
    """
    return await _agent_ask_stream(payload, user, meta)


@router.post("/agents/case-search/stream", summary="类案检索 Agent（类案 + 支撑法条 + 引用终审）")
async def agent_case_search_stream(payload: SearchRequest, user: RequireUser, meta: Meta):
    """类案检索页的入口：**钉住 ``case_search`` 走编排**，而不是直接调检索工具。

    与 ``/search/case`` 的区别：那条只调一次 ``case_search`` 就返回原始命中；
    这条会先检索支撑法条、再检类案、然后逐条核验案号存在性、最后汇总报告 ——
    页面上"未通过核验"的角标、支撑法条、证据缺口都出自这一步。
    案号核验尤其重要：本项目 93 个判例**全部没有案号**，
    直接调工具的返回值里没有任何一处会说"这条不能作为正式引用"。

    ``top_k`` 经 ``RunRequest.top_k`` → ``state["top_k"]`` → CaseNode 生效。
    这条通路是这次补上的：以前页面上「返回条数」发出去**没有接收方**
    （CaseNode 里写死 5），选 3 条还是 20 条返回值一模一样，且不报错 ——
    一个不生效也不报错的面板，比没有这个面板更糟。
    """
    req = RunRequest(
        query=payload.query,
        case_id=payload.case_id,
        user_id=str(user.id),
        team_id=str(user.team_id or ""),
        # 钉住意图。``doc_type`` 留空由 ``explicit_result`` 按意图推成 litigation，
        # 这里不重复写死一份 —— 两处各写一份，改了一处另一处就成了静默的旧值。
        intent="case_search",
        top_k=payload.top_k,
        client=meta.client,
        username=user.username,
        ip=meta.ip,
    )
    return await _sse_stream(req, user, meta, usage={"expert_id": ""})


# --------------------------------------------------------------------------- 合同审查
@router.post("/contract/review", summary="合同审查")
async def contract_review(payload: ChatRequest, user: RequireUser, meta: Meta) -> dict[str, Any]:
    """合同审查。**必须提供合同内容**（``material_text`` 或 ``material_paths``）。

    为什么强制校验：不校验的话，接口会"正常返回"一份通用合同风险清单 ——
    它会命中规则引擎里的全部检查项（保密条款、争议解决、送达地址……），
    看起来像一份完整的审查意见，但和这份合同毫无关系。
    律师很可能直接把这份清单交给客户。**一份看起来对的空报告，
    比一个明确的 400 更危险。**
    """
    if not _has_material(payload):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="合同审查需要提供合同内容：请上传合同文件（material_ids / material_paths）"
                   "或粘贴合同正文（material_text）。"
                   "为空时会输出与具体合同无关的通用风险清单，故拒绝执行。",
        )
    req = _to_run_request(payload, user, meta, intent="contract_review", doc_type="contract")
    if payload.enqueue:
        # 合同审查**最该走排队**：6~9 个节点、几分钟，同步等着等于把
        # "网关超时"与"任务失败"绑在一起 —— 而超时掐断后任务其实还在跑，
        # 律师只看到一个报错，会重复提交。
        return _enqueue(req, user, meta, kind="contract")

    outcome = await orchestrator.execute(req)
    _log_usage(user, intent="contract_review", elapsed_ms=outcome.elapsed_ms)
    _observe_evolution(outcome)
    body = outcome.to_dict(with_trace=False)
    body["risk_points"] = list(outcome.state.get("risk_points") or [])
    body["missing_clauses"] = list(outcome.state.get("missing_clauses") or [])
    body["clauses"] = len(outcome.state.get("clauses") or [])
    body["revision_draft"] = outcome.state.get("revision_draft") or ""
    # 双模型交叉复核摘要 + 人工抽查标记（见流式那条的同名注释）。
    body["second_pass"] = outcome.state.get("second_pass") or {}
    body["spot_check"] = outcome.state.get("spot_check") or {}
    if not outcome.ok:
        body["warning"] = f"流程未完整执行：{outcome.error}"
    return body


@router.post("/contract/review/stream", summary="合同审查（SSE 流式进度）")
async def contract_review_stream(payload: ChatRequest, user: RequireUser, meta: Meta):
    if not _has_material(payload):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="合同审查需要提供合同内容（material_ids / material_paths / material_text）")
    req = _to_run_request(payload, user, meta, intent="contract_review", doc_type="contract")
    return await _sse_stream(req, user, meta, usage={"expert_id": ""})


# --------------------------------------------------------------------------- 修订版
class ReviseRequest(BaseModel):
    """按审查出的风险改写合同，或只做就地标注。"""

    task_id: str = Field("", description="合同审查任务 id（用它取服务端的风险点与合同原文，避免客户端改数）")
    contract_text: str = Field("", description="合同正文；task_id 取不到原文时用它兜底")
    save: bool = Field(True, description="是否落成草稿（草稿才能走 /drafts/{id}/exports 导出 Word/PDF）")
    title: str = Field("", description="修订稿标题；留空按合同类型自动命名")
    mode: str = Field(
        "rewrite",
        description="rewrite = 按风险重写条款，产出**改完的合同全文**（调模型，约 30~60s）；"
                    "annotate = 只做就地标注的对照稿（秒级，不调模型）",
    )


@router.post("/contract/revise", summary="生成修订版合同（按风险重写 / 就地标注 + 导出 Word/PDF）")
async def contract_revise(payload: ReviseRequest, user: RequireUser, meta: Meta) -> dict[str, Any]:
    """把 ""risk_points`` 就地标注回合同正文，产出可直接编辑、可导出的修订稿。

    四条纪律：

    1. **风险点只认服务端那份。** 传 ``task_id`` 时一律从任务快照取 ``risk_points`` ——
       让客户端把风险点传回来，等于允许它凭空造出"已修订"的结论。
    2. **只改有风险的条款**，其余一字不动；改写失败的那一条保留原文并如实列出，
       绝不悄悄留一条有风险的条款当成"已改完"。
    3. **不得超出检索到的依据**：改写的依据只从本次任务的证据池里给，
       prompt 里明确禁止写进未检索到的法条编号。
    4. **定位不上要如实说。** 扫描件转出来的合同常有错字与拆字，定位失败会被列进
       「修订说明 · 未能定位」清单，界面同时显示条数 —— 不能让人以为全改到了。
    """
    from backend.app.api.v1.tasks import _assert_task_access
    from backend.app.config import settings
    from backend.app.services import draft_service, revise_service
    # ⚠ ``task_service`` 要按**实例**导入：``from backend.app.services import task_service``
    # 拿到的是模块对象，``.get()`` 会 AttributeError（tasks.py 第 33 行专门写了这个坑）。
    from backend.app.services.task_service import task_service

    mode = (payload.mode or "rewrite").strip().lower()
    if mode not in ("rewrite", "annotate"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="mode 只支持 rewrite（重写成改后合同）/ annotate（就地标注对照稿）")

    state: dict[str, Any] = {}
    risk_points: list[dict[str, Any]] = []
    if payload.task_id:
        row = task_service.get(payload.task_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"任务不存在：{payload.task_id}")
        _assert_task_access(user, row)
        state = row.get("state") or {}
        risk_points = list(state.get("risk_points") or [])

    contract_text = (payload.contract_text or state.get("material_text") or "").strip()
    if not contract_text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="缺少合同正文：请在请求里带上 contract_text，或传一份已完成审查的 task_id。")
    if not risk_points:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="该任务没有风险点记录（可能未跑完或未识别出风险），无法生成修订版。")

    clauses = list(state.get("clauses") or [])
    contract_type = str(state.get("contract_type") or state.get("doc_type") or "合同")
    result = revise_service.build_revision(
        contract_text, risk_points, clauses=clauses,
        contract_type=contract_type, title=payload.title,
    )

    # 重写模式：在定位结果上逐条改写，再拼回原文 —— 这才是"改完的合同"。
    # 定位逻辑与标注模式共用（locate_changes），避免两条路算出不同的位置。
    rewritten: dict[str, Any] = {}
    if mode == "rewrite":
        loc = revise_service.locate_changes(contract_text, risk_points, clauses=clauses)
        picked = loc["picked"]
        if picked:
            rewrites = await revise_service.rewrite_clauses(
                picked, contract_type=contract_type,
                concurrency=int(getattr(settings, "REVISE_REWRITE_CONCURRENCY", 4) or 4),
            )
            # 规则改写兜底：模型不可用（额度耗尽/降级）或输出不合规的条款，
            # 用"审查建议条款化"的确定性改写补上 —— 否则降级期间这个功能等于不存在。
            rule_used = 0
            for c in picked:
                rw = rewrites.get(c["seq"]) or {}
                if rw.get("ok"):
                    continue
                fb = revise_service.rule_rewrite_one(c)
                if fb.get("ok"):
                    rewrites[c["seq"]] = {**rw, **fb}
                    rule_used += 1
            if rule_used:
                result["stats"]["rule_fallback"] = rule_used
            rewritten = revise_service.build_rewritten(contract_text, picked, rewrites)
            result["stats"]["rewritten"] = len(rewritten["applied"])
            result["stats"]["rewrite_skipped"] = len(rewritten["skipped"])
            result["stats"]["rewrite_deleted"] = sum(
                1 for a in rewritten["applied"] if a.get("deleted"))
        else:
            result["stats"]["rewritten"] = 0
            result["stats"]["rewrite_skipped"] = 0
            result["stats"]["rewrite_deleted"] = 0
        # 供界面做"改前 → 改后"逐条对照：把改写结果并回 changes
        by_seq = {a["seq"]: a for a in rewritten.get("applied", [])}
        for c in result["changes"]:
            a = by_seq.get(c["seq"])
            if a:
                c["rewritten"] = a["rewritten"]
                c["rewrite_note"] = a["note"]
                c["deleted"] = bool(a["deleted"])
        # 标题要区分交付物：改后合同是"能直接用的那一份"，
        # 顶着「·修订版」会被当成批注稿（导出文件名也跟着它走）
        result["title"] = result["title"].replace("·修订版", "（改后版）")

    warning = ""
    if mode == "rewrite":
        done = int(result["stats"].get("rewritten") or 0)
        if not done:
            # **一条都没改成，绝不能返回 200 把原文当"改后合同"交出去。**
            # 真机上撞到过：模型网关 400 → 全部降级 → rewritten=0，
            # 而 rewritten_text 就是原文（非空），于是接口"成功"返回一份**没改过的合同**。
            # 那种交付物比报错危险得多。
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"改写未产出任何条款（{len(rewritten.get('skipped') or [])} 条全部失败，"
                       "通常是模型服务不可用或输出不合规）。本次**未生成**改后合同，"
                       "请稍后重试或先用 annotate 模式拿对照稿。")
        if result["stats"].get("rewrite_skipped"):
            warning = (f"{result['stats']['rewrite_skipped']} 条条款改写失败，已保留原文"
                       f"（未改动的部分不等于没有风险），请人工补审。")
        if result["stats"].get("rule_fallback"):
            warning = ((warning + " ") if warning else "") + (
                f"模型服务不可用，其中 {result['stats']['rule_fallback']} 条按审查建议"
                f"做规则改写（未走模型润色），请律师逐条核对措辞。")
        # 跨条款重复的安全网：条款是各改各的，同一个法律问题可能被两条都写一遍
        # （真机：试用期规则同时写进第一条和第二条）。**只报不改** ——
        # 自动删掉一处有可能把该留的那条删了，那比重复更糟。
        duplicates = revise_service.find_duplicate_sentences(rewritten["applied"])
        deduped = revise_service.dedupe_across_clauses(rewritten["applied"], rewrites)
        if deduped:
            # 去重改了条款正文 → 必须用改过的 rewrites **重新拼一遍合同**
            rewritten = revise_service.build_rewritten(contract_text, picked, rewrites)
            result["stats"]["deduped"] = len(deduped)
            by_seq = {a["seq"]: a for a in rewritten.get("applied", [])}
            for c in result["changes"]:
                a = by_seq.get(c["seq"])
                if a:
                    c["rewritten"] = a["rewritten"]
            dedup_note = "、".join(f"{d['clause_no']}（与 {d['kept_in']} 重复）" for d in deduped[:3])
            warning = ((warning + " ") if warning else "") + (
                f"已自动去掉 {len(deduped)} 处跨条款重复表述：{dedup_note}，"
                f"保留在先出现的那条里；请留意这些条款是否仍完整。")
            duplicates = revise_service.find_duplicate_sentences(rewritten["applied"])
        if duplicates:
            pairs = "、".join(f"{d['a_clause']}↔{d['b_clause']}" for d in duplicates[:3])
            warning = ((warning + " ") if warning else "") + (
                f"另有 {len(duplicates)} 处跨条款重复（{pairs}）无法自动处理（删掉会把条款删空），"
                f"建议人工删掉一处。")
        # 反向检查：防重复不能把要求"让没了"。建议里的硬要求（百分比/年限/期限/经济补偿）
        # 必须在改后条文里出现，否则就是漏改 —— 这比重复严重。
        uncovered = revise_service.find_uncovered_requirements(picked, rewritten["applied"])
        if uncovered:
            items = "；".join(f"{u['clause_no']} 缺 {', '.join(u['missing'][:3])}" for u in uncovered[:3])
            warning = ((warning + " ") if warning else "") + (
                f"{len(uncovered)} 条条款的修改建议里有硬性要求没落进条文（{items}），"
                f"请人工补写 —— 这类要求漏了等于风险还在。")
    else:
        duplicates = []
        uncovered = []

    # 交付正文：重写模式给"改完的合同"，标注模式给带标记的对照稿。
    # 草稿里存它，导出 Word/PDF 拿到的就是这一份。
    # ⚠ 必须放在上面那个"去重后重新拼一遍"（`rewritten = build_rewritten(...)`）**之后**：
    #   早先它在分支里就算好了，去重一旦命中，草稿与导出拿到的还是**去重前**的正文 ——
    #   界面上逐条对照显示的是去重后的，导出的却是旧的，两份对不上。
    deliverable = (rewritten.get("rewritten_text") if mode == "rewrite" else result["clean_text"]) or ""

    draft_id = ""
    if payload.save:
        # 落成草稿才走得到 /drafts/{id}/exports → 导出 Word/PDF 是现成能力，不重复造
        draft = draft_service.create_draft(
            title=result["title"], doc_type="revision",
            content=deliverable, user_id=user.id,
            case_id=state.get("case_id") or "", task_id=payload.task_id or "",
        )
        draft_id = draft.get("draft_id") or ""

    from backend.agents.harness.audit import audit_writer

    audit_writer.record("contract.revise", user_id=user.id, username=user.username,
                        team_id=user.team_id, client=meta.client, ip=meta.ip,
                        case_id=state.get("case_id") or "", task_id=payload.task_id or "",
                        resource="draft", resource_id=draft_id,
                        detail=f"生成修订版（{mode}）：{result['stats']['applied']}/{result['stats']['total']} 处已定位"
                               + (f"，改写 {result['stats'].get('rewritten', 0)} 条" if mode == "rewrite" else ""))
    return {
        "ok": True,
        "mode": mode,
        "task_id": payload.task_id,
        "draft_id": draft_id,
        "title": result["title"],
        "stats": result["stats"],
        "changes": result["changes"],
        "warning": warning,
        "duplicates": duplicates,
        "uncovered": uncovered,
        # rewrite：可直接使用的改后合同全文；annotate：带标记的对照稿
        "contract_text": deliverable,
        "rewritten_text": rewritten.get("rewritten_text") or "",
        "skipped": rewritten.get("skipped") or [],
        "revised_markdown": result["revised_markdown"],
        # 「复制改后合同」按钮复制的就是它（`web/app.js:1073` → `:1197` copyText）。
        # ⚠ 重写模式下这里曾经回的是 `result["clean_text"]` —— 那是**对照稿去掉标记**，
        #   也就是**改之前的原文**：律师点"复制改后合同"，粘出来的是没改过的合同。
        #   现在按模式分开：重写 = 交付正文脱掉版式标记（剪贴板是纯文本）；
        #   标注 = 对照稿去标记（那份本来就没改过条文，符合语义）。
        "clean_text": (revise_service.to_plain_text(deliverable) if mode == "rewrite"
                       else result["clean_text"]),
        # 导出走 /drafts/{draft_id}/exports?fmt=docx|pdf 再 /download，不在这里回路径
        "export_hint": "/drafts/{draft_id}/exports?fmt=docx 然后 /drafts/{draft_id}/download",
    }


# 合同审查页「选择合同文件（可多选）」支持的本机文件类型（与前端 accept 对齐）
_PARSE_SUFFIXES = {".pdf", ".docx", ".doc", ".txt", ".md",
                   ".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp"}
# 图片：走多模态「整页转写」；其余走文档解析器
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp"}


def count_chars(text: str) -> int:
    """正文字数：**不计空白**（空格 / 制表 / 换行 / 全角空格都不算），
    也**不计占位符**（下划线 ``_``／``＿``、看不清占位 ``□``）。

    ⛔ 为什么不能用 ``len(text)``：前端原来显示的就是 ``text.length``，而同一张扫描件
    每次识别的翻页位置、空格数、填空线下划线长度都不一样 —— 实测同一张图跑 6 次得到
    6 种字数（811/807/809/797/810/804，极差 14），用户看到的是「每次识别字数都不一样」。
    逐字比对归类后，47 处差异里 **38 处是空白**、5 处是填空线长度、只有 4 处是真实汉字分歧。
    ⇒ 换成"不计空白"后极差从 14 落到 **1**。

    ⛔ 为什么占位符也不计：填空线是一条连续的排版线条，``□`` 是"看不清"的块数，
    模型每次只能**估**它们有几个/多长（这不是认字认错，是量一条线有多长）。
    它们不是正文，数进去只会让字数跟着"估读"漂。正文一个字不动（见
    normalize_scan_text），只是统计口径上把这类"估出来的长度"排除掉。

    剩下的极小分歧是模型确实读错了字 —— 属于识别质量问题，该如实暴露，不该抹平；
    同一张图的重复识别已由 vision/client.py 的**内容哈希缓存**兜住（同一文件永远
    返回同一份转写），所以用户重复上传同一份合同时字数严格一致。
    """
    return sum(
        1 for ch in (text or "")
        if not ch.isspace() and ch not in "_＿□"
    )



@router.post("/contract/parse", summary="提交合同文件解析任务（异步，返回 task_id）")
async def contract_parse(
    user: RequireUser,
    files: Annotated[list[UploadFile], File(description="合同文件（PDF / Word / 文本 / 图片，可多选）")],
    meta: Meta,
) -> dict[str, Any]:
    """受理一次上传，**立刻**返回 ``task_id``；解析本身由 worker 执行。

    这里只做三件事：把字节落盘、写一条 ``kind=contract_parse`` 的队列任务、
    告诉前端去哪儿看进度。**不再同步解析** —— 一份扫描件要多模型整页转写几十秒，
    而前端 ``req()`` 有 20 秒全局超时兜底，结果是文件明明没问题却报
    「解析失败：signal is aborted without reason」（abort 掉的是等结果的请求）。

    进度：``GET /tasks/{task_id}/stream``（SSE，逐份上报）。
    正文：解析完成后 ``GET /contract/parse/{task_id}`` —— 那条接口的返回体
    与改造前的本接口**完全同形**（``files[].text`` / ``merged`` / ``merged_chars``），
    所以前端只是把"等一次响应"换成了"订阅进度 + 取结果"。

    逐份的受理结果在这一层就回给前端（``accepted`` / ``rejected``）：
    类型不支持、空文件、超过 ``settings.MAX_PARSE_MB``（默认 50M）都在这里拒绝，
    不必等任务跑完才告诉用户"第 3 份没被收下"。**一份坏文件不影响同批的好文件**，
    所以只有"一份都没收下"才 400。
    """
    from backend.app.services import contract_parse_service, task_queue

    # 每次上传都是一条**新任务**（不复用某条"会话任务"）：这个 task_id 与
    # ``_enqueue`` 里一样由受理方生成并写进快照 —— worker 认领的那一行就是它。
    task_id = f"ctparse_{uuid.uuid4().hex[:16]}"
    limit = contract_parse_service.max_parse_bytes()
    items: list[tuple[str, bytes]] = []
    for f in files:
        # 多读 1 字节：够判断"超了"就行，不必把超限文件整个读进内存。
        # 这里原来是无上限的 ``await f.read()`` —— 拖一个 2G 的文件进来
        # 就是一次 2G 的内存分配，而界面上写着「不超过 50M」。
        items.append((f.filename or "未命名", await f.read(limit + 1)))

    entries = contract_parse_service.stage_uploads(task_id, items)
    accepted = [e for e in entries if e.get("stored")]
    rejected = [{"name": e["name"], "error": e["error"]}
                for e in entries if not e.get("stored")]
    if not accepted:
        # 一份都没收下 → 不建任务（建了 worker 也只能全部失败）。
        # 400 而不是 200：这是"这次请求本身有问题"，前端要展示成上传错误，
        # 而不是让律师去任务中心看一条注定失败的任务。
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=("没有可解析的文件：" + "；".join(
                f"{r['name']}（{r['error']}）" for r in rejected))
            if rejected else "没有上传文件",
        )

    result = task_queue.enqueue(
        contract_parse_service.parse_snapshot(
            task_id, entries, user_id=user.id, team_id=user.team_id,
            client=meta.client),
        task_id=task_id,
        kind=contract_parse_service.KIND,
        user_id=user.id,
        team_id=user.team_id,
        source_client=meta.client,
    )
    if not result.get("queued"):
        # 与 ``_enqueue`` 同一条纪律：写库没成功就返回 503，
        # 不返回"200 + 一个永远跑不起来的 task_id"让律师干等。
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=result.get("reason") or "解析任务入队失败",
        )

    logger.info("合同解析任务已入队：%s（收下 %d 份，拒绝 %d 份）",
                task_id, len(accepted), len(rejected))
    return {
        "ok": True,
        "queued": True,
        "task_id": task_id,
        "kind": contract_parse_service.KIND,
        "status": "pending",
        "notified": bool(result.get("notified")),
        "notify_note": result.get("notify_note", ""),
        "count": len(accepted),
        "accepted": [{"index": e["index"], "name": e["name"],
                      "suffix": e["suffix"], "size": e["size"]} for e in accepted],
        "rejected": rejected,
        "poll": f"/tasks/{task_id}",
        "stream": f"/tasks/{task_id}/stream",
        "result_url": f"/contract/parse/{task_id}",
        "message": ("解析任务已排队，尚未开始。订阅 /tasks/"
                    f"{task_id}/stream 看逐份进度，完成后到 "
                    f"GET /contract/parse/{task_id} 取正文。"),
    }


@router.get("/contract/parse/{task_id}", summary="取合同解析结果（正文）")
def contract_parse_result(task_id: str, user: RequireUser) -> dict[str, Any]:
    """取回解析任务的正文。结构**与改造前的同步接口一致**，前端取数逻辑不用改。

    为什么不把正文塞进 ``GET /tasks/{task_id}``：``la_task.result`` 是列在
    任务列表里、每次 ``GET /tasks`` 都要拖出来的轻量列。一份 30 页合同的正文
    进去，就是任务中心每刷一次都搬一遍几万字（这一版之前正是被它撑爆的：
    正文被截断，界面上出现半截合同）。所以正文落在磁盘
    （``PROCESSED_DIR/contract_parse/<task_id>/text/``），这里现读现拼。

    任务还没跑完时返回 ``status="pending"`` 且正文为空 —— 那**不是**"解析完了但是空的"，
    前端要继续等 ``/tasks/{task_id}/stream``，别把空正文当成结果送进审查。
    逐份失败原因在 ``files[].error`` 里（例如"未从文件中识别出文字"）。
    """
    from backend.app.services import contract_parse_service
    from backend.app.services.task_service import task_service

    if not task_id.startswith("ctparse_") or not task_id.isascii():
        # 路径参数会被拼进文件系统路径，先挡掉奇怪输入（``../``、URL 编码后的分隔符）。
        # 校验方式是**白名单前缀 + 纯 ASCII**，不是拉黑几个字符 ——
        # 拉黑永远漏，而这里合法取值本来就只有一个形状。
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="非法的任务 ID")

    payload = contract_parse_service.result_payload(task_id)
    if payload is not None:
        return payload

    # 清单还没有 = 还没被 worker 跑完（或从来没跑起来）。查一下任务表，
    # 把状态如实带出去，让前端能区分"排队中"与"任务不存在" ——
    # 这两种情况前端要做的事完全不同（继续等 / 报错重传）。
    # 查表失败只影响这一句提示，**不该把 409 变成 500**。
    try:
        row = task_service.get(task_id) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("查询解析任务状态失败：%s | %s", task_id, exc)
        row = {}
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="解析尚未完成，正文还没有落盘；请先订阅 "
               f"/tasks/{task_id}/stream（当前状态：{row.get('status') or '任务不存在'}）",
    )


# --------------------------------------------------------------------------- 文书
@router.post("/doc/draft", summary="文书起草")
async def doc_draft(payload: DraftRequest, user: RequireUser, meta: Meta) -> dict[str, Any]:
    """文书起草。产出会**自动存为草稿**（``la_draft``），双端可续编。

    ``doc_type`` 决定文书类型，且**优先于关键词推断** ——
    写"劳动争议答辩状"时如果只按关键词走，很容易被判成起诉状。

    ``enqueue=true`` 时只入队、立刻返回 ``task_id``：这时候响应体里
    **没有** ``draft`` 正文（还没生成）。草稿生成后由 worker 侧写入
    ``la_draft``，去 ``GET /drafts`` 取，不要拿响应体里的空 ``draft`` 当结果。
    """
    from backend.agents.nodes.draft_node import DRAFT_LABELS

    if payload.doc_type not in DRAFT_LABELS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"文书类型不支持：{payload.doc_type}；可选：{'、'.join(DRAFT_LABELS)}",
        )
    req = RunRequest(
        query=payload.query, case_id=payload.case_id,
        user_id=str(user.id), team_id=str(user.team_id or ""),
        doc_type="contract" if payload.doc_type == "revision" else "consult",
        intent="doc_draft", no_cache=payload.no_cache,
        client=meta.client, username=user.username, ip=meta.ip,
        # **必须带上**：不带的话上面那行校验形同虚设 ——
        # 节点会拿提问去猜类型，用户选的「答辩状」可能变成「民事起诉状」。
        draft_type=payload.doc_type,
    )
    if payload.enqueue:
        body = _enqueue(req, user, meta, kind="draft")
        body["draft_type"] = payload.doc_type
        body["draft_label"] = DRAFT_LABELS.get(payload.doc_type, payload.doc_type)
        body["message"] = ("文书起草已排队。生成完成后草稿会写入草稿箱，"
                           f"去 GET /drafts?task_id={req.task_id} 取正文。")
        return body

    outcome = await orchestrator.execute(req)
    _log_usage(user, intent="doc_draft", elapsed_ms=outcome.elapsed_ms)
    _observe_evolution(outcome)
    state = outcome.state
    body = outcome.to_dict(with_trace=False)
    body["draft"] = state.get("draft") or ""
    # 用节点**实际选中**的类型回报，而不是回显请求里的入参：
    # 节点在缺省时会自己推断（``_default_draft_type``），
    # 回显入参会让"实际生成的是答辩状、界面显示起诉状"这种偏差无处发现。
    body["draft_type"] = state.get("draft_type") or payload.doc_type
    body["draft_label"] = DRAFT_LABELS.get(body["draft_type"], body["draft_type"])
    from backend.app.services import draft_service

    saved = draft_service.save_from_state(outcome.task_id, state, user_id=user.id)
    body["draft_record"] = saved
    if not outcome.ok:
        body["warning"] = f"流程未完整执行：{outcome.error}"
    return body


@router.get("/doc/types", summary="支持的文书类型")
def doc_types() -> dict[str, Any]:
    from backend.agents.nodes.draft_node import DRAFT_LABELS

    return {"items": [{"doc_type": k, "name": v} for k, v in DRAFT_LABELS.items()]}


# --------------------------------------------------------------------------- 文书模板
class TemplateComposeRequest(BaseModel):
    """一段文字 → 自动选模板 → 填充。"""

    text: str = Field(..., min_length=2, description="当事人提供的原始文字（案情、请求、事实经过）")
    template_id: str = Field("", description="指定模板 id；留空则自动识别类型与地区")
    doc_type: str = Field("", description="偏好类型：complaint / defense / arbitration_application")
    # 关联案件：带上它就会把该案证据链作为起草依据（见 _compose_chain）。
    # 留空 = 只按 text 起草，与这个功能上线前完全一致。
    case_id: str = Field("", max_length=128, description="关联案件编号，用于带入本案证据链")
    # 「调度证据链」的开关。**注意它的语义是"可以去取/生成一条链"，
    # 不是"必须有链"** —— 链取不到、没生成、案件是非诉，都由 _compose_chain
    # 降级成一段说明，文书照常产出（见其 docstring）。
    use_chain: bool = Field(True, description="是否调度本案证据链（不足/缺失都不阻断生成）")
    rebuild: bool = Field(False, description="强制重新扫描模板目录")


@router.get("/doc/templates", summary="文书模板清单（扫描 DOC_TEMPLATE_DIR 下的 .docx）")
def doc_templates(user: RequireUser, rebuild: bool = False) -> dict[str, Any]:
    """文书模板清单。

    **必须登录。** 这条原来没有 ``RequireUser``，而 ``stats()`` 当时把模板目录的
    绝对路径（``C:\\Users\\<部署者>\\Desktop\\文书模板``）放在 ``dir`` 里返回 ——
    等于向匿名调用者公开服务器用户名与目录结构。``stats()`` 已改成只回目录名，
    这里再补上鉴权，两道一起上。
    """
    from backend.app.services import template_service

    items = template_service.list_templates(rebuild=rebuild)
    return {
        **template_service.stats(),
        "items": [
            {
                "id": x["id"], "file": x["file"], "title": x["title"],
                "doc_type": x["doc_type"],
                "doc_type_label": template_service.DOC_TYPE_LABEL.get(x["doc_type"], x["doc_type"]),
                "region": x["region"], "kind": x["kind"],
                "field_count": x["field_count"], "chars": x["chars"],
            }
            for x in items
        ],
    }


async def _compose_evidence(text: str, user: Any, meta: Meta) -> dict[str, Any]:
    """文书套用前的「检索 + 引用终审」段。**任何异常都降级成一条证据缺口，绝不抛。**

    为什么失败不抛：律师来文书中心是为了一份能改的正文，不是为了一份可核验的依据。
    检索挂了也照样要把文书交付出去，缺口如实写明原因 —— 反过来（
    检索失败就 500）的后果是他连文书都拿不到，而且不知道少的是哪一半。

    为什么用完整 ``execute()`` 而不是直接调两个工具：这样它才和另外三个入口
    走同一条链路（任务表、Trace、证据缺口、缓存指纹、配额都是一套），
    "4 个页面都是 Agent"这句话才是可验证的，而不是四处各拼一套。
    """
    empty: dict[str, Any] = {
        "task_id": "", "statutes": [], "knowledges": [], "citations": [],
        "citation_check": "pending", "evidence_gap": [], "elapsed_ms": 0,
    }
    req = RunRequest(
        query=text[:MAX_QUERY_CHARS],
        user_id=str(user.id),
        team_id=str(user.team_id or ""),
        intent="doc_compose",
        client=meta.client,
        username=user.username,
        ip=meta.ip,
    )
    try:
        outcome = await orchestrator.execute(req)
        _observe_evolution(outcome)
    except Exception as exc:  # noqa: BLE001 - 检索失败不阻断文书交付
        logger.exception("文书套用前的检索终审失败（文书照常产出）")
        empty["evidence_gap"] = [{
            "kind": "tool",
            "reason": f"检索终审未完成：{type(exc).__name__}: {exc}",
            "impact": "本次文书未附可核验的法条依据，请人工核对后再交付",
        }]
        return empty

    st = outcome.state
    return {
        "task_id": outcome.task_id,
        # 与 ``_sse_stream`` 的 done 帧给的是同一份：**经 ValidatorNode 核过**的
        # 那批（``verified`` 字段已回填），不是工具通道的原始命中。
        "statutes": _public(st.get("statutes")),
        "knowledges": _public(st.get("knowledges")),
        "citations": _public(st.get("citations")),
        "citation_check": st.get("citation_check") or "pending",
        "evidence_gap": list(st.get("evidence_gap") or []),
        "elapsed_ms": outcome.elapsed_ms,
    }


# --------------------------------------------------------------------------- 证据链调度
# 文书生成时"顺带把本案证据链取过来"。三段式：读 → （必要时）生成 → 压成 prompt 能吃的摘要。
#
# ⚠️ **这一整段没有一条"链不完整就退"的分支，那是刻意的。**
#    用户的原话是"证据链不足也可以进行文书生成"：律师来文书中心是为了一份能改的正文。
#    链没生成、案件是非诉、材料一份没有 —— 每一件都只该让"依据"那一栏少一段说明，
#    不该让他连文书都拿不到（与 ``_compose_evidence`` 是同一条原则：交付优先）。
_CHAIN_MAX_CLAIMS = 12
_CHAIN_MAX_CHARS = 3000


def _chain_digest(case: dict[str, Any], chain: list[dict[str, Any]]) -> str:
    """把案件证据链压成一段能进 prompt 的纯文本。

    **压成文本而不是塞 JSON**：这个摘要的读者是模型，它要的是"主张 A 由材料 B 支撑、
    缺口是 C"这种句子；JSON 只会多花 token 还容易被它改写。
    """
    lines = [f"案件：{case.get('title') or '（未命名）'}（{case.get('case_id') or ''}）"]
    if case.get("cause_of_action"):
        lines.append(f"案由：{case['cause_of_action']}")
    for i, c in enumerate((chain or [])[:_CHAIN_MAX_CLAIMS], 1):
        lines.append(f"主张{i}：{str(c.get('claim') or '（未命名主张）').strip()}")
        evs = c.get("evidence") or []
        if evs:
            items = [f"{str(e.get('material') or '材料').strip()}（{str(e.get('support') or '直接').strip()}）"
                     for e in evs[:4]]
            lines.append("  支撑证据：" + "、".join(items))
        else:
            lines.append("  支撑证据：无")
        gap = str(c.get("gap") or "").strip()
        if gap:
            lines.append(f"  缺口：{gap}")
    if len(chain or []) > _CHAIN_MAX_CLAIMS:
        lines.append(f"（另有 {len(chain) - _CHAIN_MAX_CLAIMS} 条主张未列出）")
    return "\n".join(lines)[:_CHAIN_MAX_CHARS]


async def _compose_chain(case_id: str, user: Any, *, use_chain: bool) -> dict[str, Any]:
    """给文书生成取一份「本案证据链」摘要。**任何失败都降级成说明，绝不抛。**

    ``use_chain=True`` 时，链为空会**现场尝试生成一次**；但只在本案**已有材料**时才试 ——
    没有材料的案子必然生成一条空链（见 ``case_service.generate_evidence_chain``），
    那是白烧一次模型调用，还让 compose 白等十几秒。

    返回的 ``digest`` 是给 prompt 的；其余字段给前端交代"这次到底带没带上"。
    """
    from backend.app.services import case_service

    info: dict[str, Any] = {
        "case_id": case_id, "available": False, "generated": False,
        "claims": 0, "gap_count": 0, "gaps": [], "reason": "", "digest": "",
    }
    if not case_id:
        info["reason"] = "未关联案件"
        return info

    try:
        case = case_service.get_case(case_id, user, with_materials=True)
    except Exception as exc:  # noqa: BLE001 - 案件读不到也要出文书
        info["reason"] = f"案件不可读：{exc}"
        return info

    chain: list[dict[str, Any]] = []
    # ⚠️ 时间戳要从 ``get_evidence_chain`` 拿，**不能从 ``case_to_dict``**：
    #    后者压根不返回 ``evidence_chain_at``（转换层只挑了常用字段），
    #    于是下面那道"别重复白跑生成"的闸会恒不成立，每次 compose 都白等十几秒。
    chain_at = ""
    try:
        got = case_service.get_evidence_chain(case_id, user)
        chain = list(got.get("chain") or [])
        chain_at = str(got.get("generated_at") or "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取案件证据链失败（文书照常生成）：%s", exc)

    if not chain and use_chain:
        if not (case.get("materials") or []):
            info["reason"] = "本案还没有证据材料，证据链为空"
        elif chain_at:
            # 梳理过、但没留下结构化结论（模型不可用时会走到这里：generated_at 有值而
            # chain 为空）。**不在这里反复重跑** —— compose 本来就要跑检索终审再填字段，
            # 每次再白等十几秒生成一条注定为空的链，体感就是"文书生成变卡了"。
            # 要重试请到「证据链」页，那里是显式动作。
            info["reason"] = ("本案证据链此前已梳理过但没有结构化结论"
                              "（多为模型不可用），可在「证据链」页重试")
        else:
            try:
                # 非诉案件在这里会被挡下（证据链只对诉讼开放），
                # 挡下只是"这次没链可用"，不是失败。
                case_service.assert_evidence_chain_allowed(case.get("case_type"))
                result = await case_service.generate_evidence_chain(case_id, user)
                chain = list(result.get("chain") or [])
                info["generated"] = bool(chain)
            except Exception as exc:  # noqa: BLE001
                info["reason"] = f"证据链未生成：{exc}"

    if not chain:
        info["reason"] = info["reason"] or "本案尚未梳理出证据链"
        return info

    gaps = [str(c.get("gap")).strip() for c in chain if str(c.get("gap") or "").strip()]
    info.update({
        "available": True,
        "claims": len(chain),
        "gap_count": len(gaps),
        "gaps": gaps[:8],
        "digest": _chain_digest(case, chain),
        "reason": "",
    })
    return info


@router.post("/doc/compose", summary="按模板套用：先检索终审 → 自动识别类型 → 抽字段 → 渲染成可编辑正文")
async def doc_compose(payload: TemplateComposeRequest, user: RequireUser, meta: Meta) -> dict[str, Any]:
    """文书中心的入口：**先跑一段 Agent 编排（检索 + 引用终审），再套模板。**

    为什么是"先检索终审、再套模板"：套模板本身是确定性动作，但"这份文书依据
    哪些法条、哪些依据没通过核验"只能由检索与终审给出。原先这两件事互不相干 ——
    页面上是一份填好的文书，背后没有任何人核过它的依据。

    为什么仍然**不让模型起草**：交付物是模板渲染出来的正文，段落形状、
    ``__DELETE__`` 哨兵、Word 导出链路都是确定性资产（31 份模板逐份重建成
    24 段样张版、起诉状/答辩状段落化，都是几轮返工的成果），让模型再写一遍
    只会把它们弄丢。所以编排只跑到 ValidatorNode 为止 —— 见 ``INTENT_ROUTES``
    的 ``doc_compose``，那条路**刻意不含 DraftNode / ReportNode**。

    这条路是**真终审而非空转**：``validator_node._validatable`` 的判据包含
    "检索到的法条/类案"这类**证据池**对象，与正文有没有引用它无关 ——
    在还没生成正文的阶段跑 ValidatorNode，核的正是条文号、版本与效力本身。

    ⚠ 新增的东西挂在 ``evidence`` 下、**不摊平**：``template_service.compose()``
    自己也返回 ``degraded`` / ``provider`` / ``note``，摊平早晚撞名，
    而撞名的表现是"文书渲染的降级说明被检索的降级说明覆盖"，两边都不报错。
    ``template`` / ``content`` / ``fills`` / ``doc_type`` 等既有键**一个不少**，
    前端照旧渲染。

    ---- 案件证据链（2026-10-12）----
    带上 ``case_id`` 时，这里会**顺带调度本案证据链**：读 → 链为空且本案有材料就生成一次
    → 压成「主张 / 支撑证据 / 缺口」的摘要，作为事实与证据的来源进填充 prompt。
    响应里的 ``chain`` 交代这次到底带没带上（``available`` / ``claims`` / ``gap_count``）。

    **链不足、没生成、案件是非诉、甚至案件被删了，都不阻断出文书** —— 它们只让
    ``evidence.evidence_gap`` 多一条说明。这是需求里那句"证据链不足也可以进行文书生成"
    的落点；接口这里**没有"链不完整就 400"的分支**，别加。
    """
    from backend.app.services import template_service

    # 先调度证据链（读，必要时生成），再跑检索终审 —— 两件事互不依赖，
    # 但链在前是因为它的产物要进下面 compose 的 prompt。
    chain_info = await _compose_chain(payload.case_id, user, use_chain=payload.use_chain)
    evidence = await _compose_evidence(payload.text, user, meta)
    try:
        result = await template_service.compose(
            payload.text, template_id=payload.template_id, prefer_doc_type=payload.doc_type,
            chain_digest=str(chain_info.get("digest") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    result["client"] = meta.client if hasattr(meta, "client") else "web"
    result["evidence"] = evidence
    # 证据链的"不足"也要进「依据」那一栏。它是**事实与证据**层面的缺口，
    # 与检索到的法条是两回事：只报后者的话，律师看到"法条依据 6 条、核验通过"，
    # 会以为整份文书的依据都齐了。
    if payload.case_id and not chain_info["available"]:
        evidence.setdefault("evidence_gap", []).append({
            "kind": "chain",
            "reason": chain_info.get("reason") or "本案没有可用证据链",
            "impact": "本次文书未引用本案证据链，事实与理由请人工核对后再交付",
        })
    # digest 不进响应：它是给模型的原始素材，给前端只会多一大段没人看的字。
    result["chain"] = {k: v for k, v in chain_info.items() if k != "digest"}
    return result


# --------------------------------------------------------------------------- 纯检索
def _search_task_id(user_id: int) -> str:
    """纯检索端点的 task_id。

    ⚠ **不能写成固定的 ``f"search_{user_id}"``**：工具配额
    （``STATUTE_SEARCH_MAX_CALLS`` / ``CASE_SEARCH_MAX_CALLS``）按 task_id 计数，
    固定值会让同一用户从进程启动起共享同一个配额桶 —— 检索两三次之后全部被
    「调用次数已达上限，跳过该检索」拒绝，而且用户完全看不出原因。
    这里给每次请求一个独立 id，让配额回到"每次请求各自有预算"的本意。
    """
    return f"search_{user_id}_{uuid.uuid4().hex[:8]}"


@router.post("/search/statute", summary="法规检索（只检索，不出报告）")
async def search_statute(payload: SearchRequest, user: RequireUser, meta: Meta) -> dict[str, Any]:
    """走工具通道而不是完整 Agent 流程：保留配额、审计、降级声明。

    返回体里 ``degraded`` / ``fallback_note`` 必须原样透出 ——
    降级了却看起来像正常结果，是法律场景最不该发生的事。
    """
    from backend.tools.registry import call_tool

    result = await call_tool("statute_search", query=payload.query, top_k=payload.top_k,
                             task_id=_search_task_id(user.id))
    _audit_search(user, meta, "statute_search", payload.query, len(result.data or []))
    return _tool_body(result)


@router.post("/search/case", summary="类案检索（只检索，不出报告）")
async def search_case(payload: SearchRequest, user: RequireUser, meta: Meta) -> dict[str, Any]:
    from backend.tools.registry import call_tool

    result = await call_tool("case_search", query=payload.query, top_k=payload.top_k,
                             task_id=_search_task_id(user.id))
    _audit_search(user, meta, "case_search", payload.query, len(result.data or []))
    return _tool_body(result)


@router.post("/search/knowledge", summary="知识库检索")
async def search_knowledge(payload: SearchRequest, user: RequireUser, meta: Meta) -> dict[str, Any]:
    from backend.tools.registry import call_tool

    result = await call_tool("knowledge_search", query=payload.query, top_k=payload.top_k,
                             task_id=_search_task_id(user.id))
    _audit_search(user, meta, "knowledge_search", payload.query, len(result.data or []))
    return _tool_body(result)


@router.post("/search/material", summary="在案件材料内检索")
async def search_material(payload: SearchRequest, user: RequireUser, meta: Meta) -> dict[str, Any]:
    """在**某个案件**的材料里检索。先做权限校验再检索 ——
    否则就成了"用检索接口绕过案件隔离"。"""
    from backend.app.services import case_service

    if not payload.case_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="需要指定 case_id")
    try:
        case_service.get_case(payload.case_id, user, with_materials=False)
    except case_service.CaseNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except case_service.CaseForbidden as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    from backend.rag.pipeline import retrieve_materials

    hits, trace = await retrieve_materials(payload.query, case_id=payload.case_id,
                                           top_k=payload.top_k)
    _audit_search(user, meta, "material_search", payload.query, len(hits))
    return {
        "items": hits,
        "count": len(hits),
        "degraded": bool(getattr(trace, "degraded", False)),
        # RetrievalTrace 里是 ``notes`` 列表（可能有多条降级说明），不是单个字符串。
        # 拼起来一起透出，别只取第一条 —— 少看一条就等于少知道一处降级。
        "fallback_note": "；".join(getattr(trace, "notes", None) or []),
        "retrieval": {
            "vector_hits": int(getattr(trace, "vector_hits", 0) or 0),
            "bm25_hits": int(getattr(trace, "bm25_hits", 0) or 0),
            "fused": int(getattr(trace, "fused", 0) or 0),
            "elapsed_ms": int(getattr(trace, "elapsed_ms", 0) or 0),
        },
    }


# --------------------------------------------------------------------------- 纯 RAG 问答
def _rag_history(payload: RagAskRequest) -> list[dict[str, str]]:
    return [{"q": t.q, "a": t.a} for t in payload.history if (t.q or "").strip()]


def _rag_body(out: Any, conversation_id: str = "") -> dict[str, Any]:
    """纯 RAG 问答的响应体。``conversation_id`` **恒存在**，空串表示本轮没落库。

    空串不是错误：落库失败时答案照样返回（见 :func:`_persist_rag_turn`），
    前端据此不点亮侧栏即可，不必再开一个错误分支。
    """
    body = out.to_dict()
    body["ok"] = True
    body["conversation_id"] = conversation_id
    return body


def _persist_rag_turn(user: Any, payload: RagAskRequest, out: Any) -> str:
    """把这一轮问答落进会话表，返回会话号；**失败返回空串，绝不抛**。

    为什么不写在 ``rag_answer.answer()`` 里：那条链路是纯检索 + 生成，
    ``tests/test_rag_answer.py`` 直接调它（没有 DB、没有登录主体），
    落库挂上去那三条单测全红。落库属于接口层的职责。

    为什么吞掉所有异常：答案这时**已经生成完了**，抛出去只会让律师看到
    "答完了但屏幕上什么都没有"。历史缺一轮是可以接受的，答案是。
    """
    from backend.app.services import chat_history_service

    try:
        return chat_history_service.append_turn(
            user=user, query=payload.query, payload=out.to_dict(),
            conversation_id=payload.conversation_id, case_id=payload.case_id,
        )["conversation_id"]
    except Exception:  # noqa: BLE001 - 落库失败绝不能影响作答
        logger.exception("RAG 会话落库失败（答案照常返回）")
        return ""


@router.post("/chat/rag", summary="纯 RAG 问答（检索 → 生成，不走 Agent 编排）")
async def chat_rag(payload: RagAskRequest, user: RequireUser, meta: Meta) -> dict[str, Any]:
    """AI 助手页的问答入口：**只做混合检索 + 生成**，不跑 LangGraph、不写任务表。

    为什么不复用 ``/chat``：那条路会经过 ReportNode，而它的交付物是**报告模板**
    （引用清单 / 证据缺口 / 综合置信度三节）。那三节是合同审查、文书起草要的东西，
    改成对话式会同时毁掉那三个入口。所以这里另开一条轻链路。

    留痕：本次问答落 ``la_chat_turn``（AI 助手页自己的会话表，一行 = 一轮），
    「历史会话」栏据此完整重放，响应里回 ``conversation_id``。
    **仍然不写 ``la_task``** —— 任务中心、进度轮询、相似问题缓存这三样是 ``/chat`` 的语义，
    接过来就等于把 orchestrator 又搬回来一遍，而那正是这个入口要摆脱的东西（有测试钉住）。
    会话记录只服务"我上次问过什么"，不是任务档案；需要交付物与留痕的提问走 ``/chat``。

    落库失败**不影响作答**：写库异常只记日志，``conversation_id`` 回空串。
    """
    from backend.app.services import rag_answer

    out = await rag_answer.answer(
        payload.query, history=_rag_history(payload), top_k_statutes=payload.top_k)
    _audit_search(user, meta, "rag_answer", payload.query, out.retrieval.get("statutes", 0))
    _log_usage(user, intent="rag", elapsed_ms=out.elapsed_ms)
    return _rag_body(out, _persist_rag_turn(user, payload, out))


@router.post("/chat/rag/stream", summary="纯 RAG 问答（SSE 推检索阶段）")
async def chat_rag_stream(payload: RagAskRequest, user: RequireUser, meta: Meta) -> StreamingResponse:
    """与 ``/chat/rag`` 同参数，但把阶段推给前端。

    **事件名是 ``stage`` 而不是 ``trace``。** ``trace`` 在本项目里专指
    LangGraph 的**节点**事件；这条链路上没有节点，沿用 ``trace`` 会让前端的
    执行轨迹面板显示「StatuteNode · 法规检索 Agent」这类根本没跑过的 Agent ——
    界面在编造执行过程。``stage`` 报的是检索器自己的真实计数（向量召回 / BM25 / 融合）。

    为什么不逐字流式：``llm_client`` 没有 ``stream()``（``stream: False`` 是硬编码的），
    后端拿不到 token 流。这里推的是**检索阶段进度**，不是逐字输出。

    落库写在 ``_run()`` 里、``done`` 帧**之前**，两个顺序都是硬的：

    - 在 ``done`` 之前 —— 前端收到 ``done`` 会立刻刷历史列表，
      反过来的话刚问完的这条不会出现在侧栏里（看起来像"没保存"）。
    - 在 ``_run()`` 里而不是响应生成器 ``_gen()`` 里 —— 客户端断开时 Starlette
      取消的是 ``_gen()``，``_run()`` 是被 ``create_task`` 独立跑的。所以
      **断开也照落库**：律师看到一半关掉页面，回来还能在历史里找到这段答案。
    """
    from backend.app.services import rag_answer

    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def _run() -> None:
        try:
            def _on_stage(name: str, detail: str = "") -> None:
                try:
                    queue.put_nowait({"event": "stage",
                                      "data": {"node": name, "detail": detail, "event": "step"}})
                except Exception:  # noqa: BLE001 - 推不进去不能影响出答案
                    pass

            out = await rag_answer.answer(
                payload.query, history=_rag_history(payload),
                top_k_statutes=payload.top_k, on_stage=_on_stage)
            _audit_search(user, meta, "rag_answer", payload.query, out.retrieval.get("statutes", 0))
            conv_id = _persist_rag_turn(user, payload, out)
            _log_usage(user, intent="rag", elapsed_ms=out.elapsed_ms)
            await queue.put({"event": "done", "data": _rag_body(out, conv_id)})
        except Exception as exc:  # noqa: BLE001
            logger.exception("RAG 问答异常：%s", exc)
            await queue.put({"event": "error", "data": {"error": f"{type(exc).__name__}: {exc}"}})

    task = asyncio.create_task(_run())
    _keep(task)

    async def _gen():
        yield _sse_frame("meta", {"client": meta.client, "username": user.username,
                                  "intent": "rag", "query": payload.query})
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=20.0)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            yield _sse_frame(msg["event"], msg["data"])
            if msg["event"] in ("done", "error"):
                break

    return StreamingResponse(_gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


# 三个检索工具的 ``data`` 是 **dict**，形如
# ``{"query": …, "count": N, "statutes": [...]}``。
# 命中列表在各自命名的键下，不在 ``items`` 上。
_HIT_KEYS = {
    "statute_search": "statutes",
    "case_search": "cases",
    "knowledge_search": "knowledges",
}


def _tool_body(result: Any) -> dict[str, Any]:
    """把 ``ToolResult`` 摊平成 HTTP 响应体。

    **``items`` 必须是命中列表，不能是 ``list(data)``。**
    工具返回的 ``data`` 是 dict，``list(dict)`` 拿到的是**键名**——
    于是接口返回 ``items: ["query", "count", "statutes", "trace"]``：
    状态码 200、``count`` 也是对的，看起来一切正常，
    但一条命中都没有，前端渲染出来是一排字段名。
    这个错**不会抛任何异常**，只会静默地把结果换成字段清单。

    所以这里按工具名取对应的命中键；工具改了字段名时兜底取第一个 list 值
    （``trace`` 虽然也是 list，但它排在命中列表之后，取第一个能命中正确项）。
    工具原始字段（``statutes`` / ``cases`` / ``knowledges`` / ``trace``）
    一并透出 —— 前端要按类型分别渲染，也需要 ``trace`` 里的降级说明。
    """
    tool = getattr(result, "tool", "") or ""
    data = getattr(result, "data", None)

    if isinstance(data, dict):
        body: dict[str, Any] = dict(data)
        key = _HIT_KEYS.get(tool, "")
        hits = body.get(key) if key else None
        if not isinstance(hits, list):
            # 兜底：取第一个「元素是字典的 list」。
            # 只挑元素为字典的列表，是为了避开 ``trace`` 里的 notes（字符串列表）、
            # 也避开将来可能出现的计数字段 —— 那些东西塞进 items 比空着更误导。
            hits = next((v for v in body.values()
                         if isinstance(v, list) and all(isinstance(x, dict) for x in v)), [])
    elif isinstance(data, list):
        body = {}
        hits = data
    else:
        body = {}
        hits = []

    hits = list(hits or [])
    body.update({
        "items": hits,
        "count": len(hits),
        "ok": bool(result.ok),
        "degraded": bool(result.degraded),
        "fallback_note": getattr(result, "fallback_note", "") or "",
        "error": getattr(result, "error", "") or "",
        "elapsed_ms": int(getattr(result, "elapsed_ms", 0) or 0),
        "tool": tool,
    })
    return body


def _audit_search(user: Any, meta: Meta, tool: str, query: str, hits: int) -> None:
    from backend.agents.harness.audit import audit_writer

    audit_writer.agent_retrieval(agent="api", tool=tool, query=query, hits=hits,
                                 user_id=user.id, username=user.username,
                                 team_id=user.team_id, client=meta.client, ip=meta.ip)
