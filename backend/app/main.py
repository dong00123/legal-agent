"""FastAPI 应用装配与启动自检。

这个文件只管"把东西接起来"，不含任何业务判定。四块内容：

1. **``configure_logging()``** —— 显式挂 root handler。
   Python 的 ``logging.lastResort`` **只输出 WARNING 及以上**，所以
   ``logger.info("向量库已启用")`` 这类启动状态在默认 uvicorn 下**一个字都看不到**，
   等于白写。启动自检的意义就是"配了到底生效没有"，看不见就等于没有。
   注意：**已被配置过就不覆盖** —— uvicorn / pytest 自己会配 handler，
   抢过来会把它们的格式与级别搞乱。

2. **``run_startup_check()``** —— 把「实际生效的后端」和「哪里降级了」打出来，
   并做结构对账（``init_db``）。**关键项用 WARNING 打**：
   外键指向 legacy 备份表、出厂 SECRET_KEY 没换（容器里也算，见
   ``_exposed_environment``）、``.env`` 里有未映射的键 —— 这三类问题都不影响启动，但会在某天突然变成故障，
   必须在启动日志里显式出现，不能只在 ``/health`` 里躺着（没人会主动去看）。

3. **``create_app()``** —— 路由挂载、CORS、静态托管、异常处理器。
   挂载顺序有硬要求，见下方 ``_mount_frontend`` 的注释。

4. **异常处理器** —— 服务层的 ``UserError`` / ``CaseError`` / ``DraftError`` /
   ``KnowledgeError`` 统一在这里转 HTTP。**不在每个路由里写 try/except**：
   漏一个就变成 500，而 500 的响应体里不带 detail，前端只能弹"系统错误"。

**已知边界**：``create_app()`` 会在 import 时被调用一次（``app = create_app()``），
uvicorn 用 ``backend.app.main:app`` 启动时直接拿它。测试里若需要独立实例，
调 ``create_app()`` 即可 —— 但它会**复用同一个全局 engine**，
所以测试仍然打在同一个库上（见 ``tests/test_api.py`` 的说明）。
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
import sys
from typing import Any, AsyncIterator

# 直接 `python backend/app/main.py` 启动时，Python 不会知道项目根在哪，
# 下面的 `from backend.app...` 会炸成 ModuleNotFoundError。
# 所以必须在任何 backend 导入之前把项目根塞进 sys.path。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.app.api.v1 import api_router
from backend.app.api.v1 import ws as ws_routes
from backend.app.api.v1.meta import probe_router
from backend.app.config import WEB_DIR, describe_settings, settings
from backend.app.services.evolution_scheduler import (
    start_evolution_scheduler,
    stop_evolution_scheduler,
)

logger = logging.getLogger("legal_agent.app")

_INITIAL_SECRET = "change-me-in-production-legal-agent"


def _exposed_environment() -> bool:
    """当前进程是否处在"已经对外冒头"的环境里（容器或被显式标为 prod）。

    **为什么不能只看 ``ENV``。** 这条判据的前身是 ``ENV == "prod"``，
    它有个致命前提：部署的人会记得把 ENV 从 dev 改成 prod。实际不会 ——
    ``.env`` 是从 ``.env.example`` 复制来的，里面写死 ``ENV=dev``，
    而容器编排又把这份文件原样挂进容器的情况很常见（本项目早期就是这样）。
    于是后果是：**一个印在公开文档里的示例密钥被用来签发 JWT，
    而启动日志一行警告都没有** —— 告警的前置条件恰恰被那个示例配置满足了。

    所以这里看**进程实际在哪跑**，而不是它自称处于什么环境：
    容器十有八九是要对外提供服务的。
    """
    if settings.ENV == "prod":
        return True
    # Docker 会在容器根放这个标记文件（Podman / containerd 同样有）。
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return any(m in cgroup for m in ("docker", "kubepods", "containerd"))

# 前端挂在 ``/ui`` 而不是 ``/``。原因见 ``_mount_frontend``：
# 挂在 ``/`` 的 ``StaticFiles`` 会吃掉 WebSocket 的 scope 并直接断言失败。
WEB_ROUTE = "/ui"


# =========================================================================== 日志
def configure_logging(level: int | None = None) -> None:
    """配置 root logger（幂等；已被配置则不动）。

    ``level`` 缺省时按 ``DEBUG`` 开关决定：dev/test 用 INFO，prod 用 INFO 也一样 ——
    生产环境不该靠"日志少"来省磁盘，该关的是每条请求的明细（那部分在
    middleware 里按 DEBUG 单独降级）。这里保持 INFO，因为启动自检正是 INFO 级。
    """
    root = logging.getLogger()
    if root.handlers:
        # uvicorn 默认会配 ``uvicorn.access`` / ``uvicorn.error`` 两个 handler，
        # 但它们的 handler 挂在 **子 logger** 上，root 上可能仍是空的。
        # 只要 root 上已经有 handler（比如 pytest 的 caplog），就尊重现状。
        return

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    ))
    handler                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  .setLevel(logging.INFO)
    root.addHandler(handler)
    root.setLevel(level if level is not None else logging.INFO)

    # 第三方库在 INFO 下太吵，把真正有用的那几行埋掉了。
    for noisy in ("httpx", "httpcore", "urllib3", "pymilvus", "jieba", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# =========================================================================== 启动自检
def run_startup_check() -> dict[str, Any]:
    """启动自检。**每项单独 try** —— 一个可选组件没装不该让服务起不来。

    返回的 dict 挂到 ``app.state.startup_check``，之后可以这样查：
    ``/meta/config`` 返回它的**完整**内容（管理员可见），``/health`` 只取
    其中的 ``warmup`` 一项 —— 后者是免登录探针，不能带初始账号名这类信息。

    （这句以前写的是"/health 与 /meta/config 都能读它"，但当时两个地方
    其实都没有读。docstring 承诺的接口如果没人实现，比不写更害人。）
    """
    check: dict[str, Any] = {}

    def _safe(label: str, fn, *args: Any, **kw: Any) -> Any:
        try:
            return fn(*args, **kw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("启动自检子项 %s 失败：%s: %s", label, type(exc).__name__, exc)
            return {"error": f"{type(exc).__name__}: {exc}"}

    # ---- 1. 配置：实际生效的后端 + .env 对拍
    logger.info("启动自检\n%s", describe_settings())
    unmapped = settings.unmapped_env_keys()
    check["config"] = {
        "backends": settings.runtime_backends(),
        "unmapped_env_keys": unmapped,
    }
    if unmapped.get("missing_file"):
        logger.warning("未找到 .env（%s），当前全部使用代码默认值", unmapped.get("file"))
    elif unmapped.get("unmapped"):
        # 未映射的键 = 用户以为配了、其实被 pydantic 忽略掉的配置。静默失效的头号元凶。
        logger.warning("检出 %d 个未映射的 .env 配置键（配了但不生效）：%s",
                       len(unmapped["unmapped"]), ", ".join(unmapped["unmapped"]))

    # ---- 2. 出厂密钥（判据见 ``_exposed_environment``：容器也算，不看 ENV）
    if _exposed_environment() and settings.SECRET_KEY == _INITIAL_SECRET:
        logger.warning(
            "SECRET_KEY 仍是公开示例值 —— 任何看过本项目文档的人都能伪造任意用户的 JWT，"
            "直接以律师身份调取案件。请立刻设置 SECRET_KEY"
            "（改完已签发的令牌全部失效，属预期）。"
        )
    if len(settings.SECRET_KEY.encode("utf-8")) < 32:
        # HMAC-SHA256 的推荐密钥长度下限是 32 字节（RFC 7518 §3.2）。
        # 短密钥不会报错、不会失败，只是**悄悄变弱**，所以必须显式提示。
        logger.warning(
            "SECRET_KEY 只有 %d 字节，低于 HMAC-SHA256 的推荐下限 32 字节（RFC 7518 §3.2）。"
            "请换一段更长的随机串：python -c \"import secrets;print(secrets.token_urlsafe(48))\"",
            len(settings.SECRET_KEY.encode("utf-8")),
        )

    # ---- 3. 关系库：建表 + 结构对账 + 外键修复
    from backend.app.db import session as db_session
    from backend.app.services import user_service

    report = _safe("init_db", db_session.init_db)
    check["database"] = _safe("db_status", db_session.db_status)
    check["schema"] = _safe("schema_health", db_session.schema_health)

    if isinstance(report, dict):
        # init_db 自己就失败了（连接问题等），错误内容已在 _safe 里打过 WARNING
        check["schema_report"] = report
    else:
        if report.failed_fks:
            # **必须响亮地报出来**：失败的每一条都意味着对应的子表插入会报 1452。
            logger.warning(
                "外键修复有 %d 条未成功（其子表插入会报 1452）：%s",
                len(report.failed_fks),
                "; ".join(f"{t}.{c}" for t, c in report.failed_fks),
            )
        if report.orphan_cleared:
            logger.warning(
                "外键修复时清空了 %d 处孤儿引用（原因是旧备份表 id 已不存在）：%s",
                len(report.orphan_cleared),
                "; ".join(f"{t}.{c}:{n}行" for t, c, n in report.orphan_cleared),
            )
        check["schema_report"] = {
            "renamed": report.renamed,
            "restored_fks": report.restored_fks,
            "failed_fks": report.failed_fks,
            "ok": report.ok,
        }

    # ---- 4. 用户种子（空库时建管理员与演示律师）
    check["seed"] = _safe("seed_defaults", user_service.seed_defaults)
    if isinstance(check["seed"], dict) and check["seed"].get("created"):
        logger.warning(
            "已创建 %d 个初始账号：%s（初始口令为出厂值，请登录后立即修改）",
            len(check["seed"]["created"]), ", ".join(check["seed"]["created"]),
        )

    # ---- 5. 工具注册表 / Agent 目录 / 技能 / 配置白名单
    check["tools"] = _safe("registry_status", _call, "backend.tools.registry", "registry_status")
    check["agents"] = _safe("a2a_health", _call, "backend.agents.protocol", "health")
    check["skills"] = _safe("skills_status", _call, "backend.skills", "skills_status")
    cfg = _safe("config_service.health", _call,
                "backend.app.services.config_service", "health")
    check["editable_config"] = cfg
    if isinstance(cfg, dict) and cfg.get("problems"):
        # 白名单里写了不存在的配置键 = 管理后台点了"保存"却什么也没发生，且不报错。
        logger.warning("可热改配置白名单自检失败：%s", "; ".join(cfg["problems"]))

    if isinstance(check["tools"], dict) and check["tools"].get("problems"):
        logger.warning("工具注册表自检发现问题：%s", "; ".join(check["tools"]["problems"]))

    return check


def _call(module: str, attr: str, *args: Any) -> Any:
    import importlib

    return getattr(importlib.import_module(module), attr)(*args)


# 要预热的 BM25 语料库。**列全** —— 漏掉的那个就是慢的那个，
# 而它只会在某个律师第一次检索到那一类时突然现身。
_WARMUP_BM25 = ("statute", "case", "knowledge", "memory")


async def run_warmup() -> dict[str, Any]:
    """预热检索链路：Embedding 模型 / BM25 索引 / 向量库 / 精排。

    **为什么必须有这一步。** 这些组件全是懒加载 —— 只有第一次检索才去
    import torch、读 BGE 权重、连 Milvus、载 BM25 索引。实测进程内第一次
    检索耗时 **30 秒以上**（其中约 29 秒花在首次 import sentence_transformers
    与 torch 上，第二次就只剩 2 秒），之后同一进程内降到 20~30 毫秒。
    不预热的话，这半分钟会精确地落在**第一个真实用户**头上 ——
    而那个人往往就是演示时坐在旁边看的那位。

    **为什么是逐个预热组件，而不是跑一次真实检索。** 真实检索得走
    ``call_tool``，而它受 ``TOOL_TIMEOUT``（默认 30 秒）管辖 ——
    冷启动恰好要 30 秒出头，于是**预热自己被超时掐断**，白等满 30 秒
    再重试（实测启动日志：『工具 statute_search 第 1 次失败（超时（30s））』）。
    加载模型是基础设施行为，不该受业务超时约束；直接调底层才拿得到
    "要多久就多久"的资格。

    **失败不阻断启动。** 组件没装/没起时这里必然失败，那时检索本来
    就会自行降级，服务仍然可用 —— 预热的意义只是把代价从"请求时"
    挪到"启动时"，挪不过去也不该让服务起不来。
    """
    from backend.rag.embedding import embedding_client
    from backend.rag.retriever.bm25 import get_bm25
    from backend.rag.retriever.rerank import get_reranker
    from backend.rag.retriever.vector import get_vector_store

    out: dict[str, Any] = {}
    t0 = time.perf_counter()

    async def _one(label: str, fn: Any, *args: Any) -> None:
        """单项预热。**每项单独 try**：一个组件没装不该让别的也预热不成。"""
        s = time.perf_counter()
        try:
            res = fn(*args)
            if asyncio.iscoroutine(res):
                res = await res
            out[label] = {"ok": True, "ms": round((time.perf_counter() - s) * 1000, 1)}
        except Exception as exc:  # noqa: BLE001
            logger.warning("预热 %s 失败（不影响启动）：%s: %s",
                           label, type(exc).__name__, exc)
            out[label] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # embedding 排第一：它最慢（要 import torch），让日志里立刻看到"正在等什么"，
    # 而不是卡在一个看不出在干什么的阶段上。
    await _one("embedding", embedding_client.ensure)
    for name in _WARMUP_BM25:
        await _one(f"bm25:{name}", asyncio.to_thread, get_bm25, name)
    await _one("vector_store", asyncio.to_thread, get_vector_store)
    await _one("reranker", asyncio.to_thread, get_reranker)

    # ---- 精排**真实预推理**（2026-10-07 加）----------------------------------
    # 为什么 ``get_reranker()`` 不够：它只把权重读进内存，**第一次真正算
    # CrossEncoder.forward 还要现场做 torch 的算子预热与内存分配**。实测
    # （20 核 CPU / 无 GPU / bge-reranker-base）跑完 ``run_warmup`` 之后，
    # 第一条 ``knowledge_search`` 仍要 **8.9 秒**，profile 显示其中
    # ``torch._C._nn.linear`` 独占 6.8 秒 —— 那就是精排本身，不是加载。
    #
    # 为什么要用真实长度而不是一句话：CrossEncoder 的耗时会随文本长度增长，
    # 用短样本预热出的算子缓存覆盖不了合同条款那种长文本。所以这里刻意用
    # 一段有实际长度的中文文本，跑一次与线上一致的 pool 规模。
    #
    # 代价：启动多花几秒。换来的是第一个真实请求不再替所有人付这笔钱。
    await _one("reranker_warm_inference", _warm_reranker_inference)

    out["ok"] = all(v.get("ok") for v in out.values() if isinstance(v, dict))
    out["total_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    logger.info("检索链路预热完成：%.1fs（一次性冷启动成本，之后同进程内命中缓存）",
                out["total_ms"] / 1000)
    return out


# 预推理用的检索式与文本。长度贴近真实条款（约 200+ 字），
# 太短会让 torch 的算子缓存覆盖不到长文本路径，预热等于白做。
_WARM_RERANK_QUERY = "请审查这份合同，逐条指出风险条款、法律依据与修改建议。"
_WARM_RERANK_DOC = (
    "第{n}条 违约责任：任何一方违反本合同约定，应向守约方支付违约金，"
    "违约金比例为合同总金额的百分之三十；如违约情形持续超过三十日，守约方"
    "有权单方解除本合同并要求赔偿实际损失，守约方亦有义务在解除前书面通知对方，"
    "通知自送达之日起生效。争议解决方式为向合同签订地有管辖权的人民法院提起诉讼。"
)


async def _warm_reranker_inference() -> None:
    """跑一次真实精排，把 CrossEncoder 的首次算子开销挪到启动期。

    规模按 ``RETRIEVE_TOP_K`` 与 ``RERANK_TOP_K`` 推算的实际上界
    （合同审查那条链走 ``top_k=15``，``pool = max(top_k*4, retrieve_k)``）
    取一个中等值：太大拖慢启动，太小覆盖不到线上耗时档位。
    """
    from backend.rag.retriever.rerank import get_reranker

    pool_size = max(12, min(30, int(settings.RERANK_TOP_K) * 3))
    docs = [{"text": _WARM_RERANK_DOC.format(n=i + 1)} for i in range(pool_size)]
    await asyncio.to_thread(get_reranker().rerank, _WARM_RERANK_QUERY, docs,
                            int(settings.RERANK_TOP_K))


# =========================================================================== 生命周期
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动自检 + 优雅关闭。

    启动阶段是同步阻塞的（建库、跑对账 SQL）：这个时点还没有流量，
    把它丢进线程池只会让"启动失败"的日志和实际原因错开，反而难查。
    """
    t0 = time.perf_counter()
    app.state.startup_check = run_startup_check()
    logger.info("启动自检完成，用时 %.2fs", time.perf_counter() - t0)

    # 预热放在自检**之后**：自检会打印"哪些组件处于降级"，那些日志必须在
    # 预热之前出现 —— 否则预热失败的原因会被埋在它自己那一堆日志中间。
    if settings.WARMUP_ENABLED:
        app.state.startup_check["warmup"] = await run_warmup()
    else:
        logger.warning(
            "检索链路预热已关闭（WARMUP_ENABLED=false）—— "
            "第一个检索请求要自己承担模型加载时间（实测约 18 秒）"
        )

    if settings.AUDIT_ENABLED:
        logger.info("审计落盘目录：%s", settings.AUDIT_LOG_DIR)
    if not settings.DESENSITIZE_ENABLED:
        logger.warning("脱敏已关闭（DESENSITIZE_ENABLED=false）—— 仅限本地调试，勿用于真实案件")

    # 周期进化调度器放在最后启动：它一旦跑起来就会读写 state.json、建进化任务，
    # 必须等自检与预热都结束之后再开始，免得两类日志交织在一起难以分辨。
    if settings.EVOLUTION_SCHEDULER_ENABLED:
        start_evolution_scheduler(tick_seconds=settings.EVOLUTION_SCHEDULER_TICK_SECONDS)
    else:
        logger.warning(
            "周期进化调度器已关闭（EVOLUTION_SCHEDULER_ENABLED=false）—— "
            "飞轮只在人工点「触发一轮周期」时前进"
        )

    try:
        yield
    finally:
        await stop_evolution_scheduler()
        from backend.app.db import session as db_session

        db_session.dispose_engine()
        logger.info("已释放数据库连接池，进程退出")


# =========================================================================== 静态托管
class _RevalidatingStatic(StaticFiles):
    """静态资源每次回源校验：``no-cache`` + ETag 304。

    **为什么要改默认行为。** Starlette 默认不发 ``Cache-Control``，浏览器
    于是走启发式缓存（Chrome 拿 Last-Modified 的一小段当新鲜期，期间连
    reload 都不回源）。后果是前端资源更新后用户按 F5 仍然看到旧界面 ——
    实测改完登录页样式刷新没变化，白排查了一阵，以为是 CSS 写错了。

    ``no-cache`` 不等于"不缓存"，而是"用之前先问一句"：没改动的文件回
    304（几十字节），改动过的立刻生效。内网这点往返成本可以忽略，换来的是
    **发布即所见**，不用教用户按 Ctrl+F5。

    不用 ``no-store``：那会让每次都完整重传，白白牺牲掉 304 的好处。
    """

    async def get_response(self, path: str, scope: Any) -> Response:
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


def _mount_frontend(app: FastAPI) -> None:
    """托管 ``web/``（律师工作台与管理后台），挂在 ``/ui``。

    **为什么不挂在 ``/``。** 曾经就是这么挂的，结果是**所有 WebSocket 连接都连不上**：
    ``StaticFiles`` 在 ``__call__`` 里第一行就 ``assert scope["type"] == "http"``，
    而挂在 ``/`` 的 Mount 会把 WebSocket 的 scope 也吃进去，直接抛
    ``AssertionError``（报错栈里只有 starlette 的 staticfiles.py，看不出跟前端有关）。
    浏览器端看到的现象是"WS 一会儿就断，没有任何原因"。

    挂在 ``/ui`` 之后，``/ws/...`` 与 ``/api/v1/...`` 都不会被静态文件抢走。
    代价是首页多一跳重定向（``/`` → ``/ui/``），而 HTML 里的资源引用要写
    ``/ui/css/...`` 这种绝对路径 —— 这个由前端文件自己保证。

    目录不存在时**不挂**：``check_dir=True`` 时缺目录会抛 ``RuntimeError``，
    让"只部署 API"的环境完全起不来。
    """
    if not WEB_DIR.exists():
        logger.warning("前端目录不存在（%s），仅提供 API；请先构建 web/", WEB_DIR)
        return
    if not (WEB_DIR / "index.html").exists():
        logger.warning("未找到 %s，静态资源可访问但首页不可用", WEB_DIR / "index.html")
    app.mount(WEB_ROUTE,
              _RevalidatingStatic(directory=str(WEB_DIR), html=True, check_dir=False),
              name="web")
    logger.info("前端已托管：%s -> %s", WEB_DIR, WEB_ROUTE)


# =========================================================================== 异常处理
def _error_id() -> str:
    return uuid.uuid4().hex[:12]


async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """HTTP 异常统一响应体。

    自己接 ``HTTPException`` 是为了在 404 里**带上请求路径**：
    默认的 ``{"detail": "Not Found"}`` 在排查"路由是不是写错了"时
    等于没有信息量。带上 ``path`` 后，一眼能看出是路径拼错还是资源不存在。

    **这里不做 SPA 回退**（不把未知路径返回 ``index.html``）：
    回退会让拼错的接口路径返回 200 + 一坨 HTML，前端 ``res.json()`` 报
    ``Unexpected token '<'``，而真正的原因（路径写错）被彻底掩盖。
    前端用 hash 路由，本来也不需要这个回退。
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "path": request.url.path},
        headers=getattr(exc, "headers", None) or None,
    )


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 校验错误：把 pydantic 的错误压成"字段 + 原因"。

    默认响应体是一层套一层的 ``loc`` 数组，前端要写不少代码才能拼出一句人话。
    这里直接给 ``fields``，并保留原始 ``errors`` 供调试。
    """
    fields = [
        {
            "field": ".".join(str(x) for x in err.get("loc", ()) if x != "body"),
            "reason": err.get("msg", ""),
            "type": err.get("type", ""),
        }
        for err in exc.errors()
    ]
    summary = "；".join(f"{f['field']}: {f['reason']}" for f in fields) or "请求参数不合法"
    return JSONResponse(
        # 用字面量 422 而不是 ``status.HTTP_422_*``：starlette 1.x 把
        # ``HTTP_422_UNPROCESSABLE_ENTITY`` 改名为 ``..._CONTENT``，
        # 写符号常量会让代码绑死在某个版本上并持续打印弃用警告。
        status_code=422,
        content={"detail": f"参数校验失败 —— {summary}", "fields": fields,
                 "path": request.url.path},
    )


async def _business_error(request: Request, exc: Exception) -> JSONResponse:
    """服务层业务异常 → 4xx。

    三类映射分开是因为它们对调用方的含义完全不同：
    - ``CaseForbidden`` / PermissionError → 403：登录了，但没这个权限。
      返回 404 会把"存在但无权"伪装成"不存在"，律师报障时说不清。
    - ``CaseNotFound`` / LookupError → 404
    - 其余 ``*Error``（ValueError 子类）→ 400：入参不合法
    """
    if isinstance(exc, PermissionError):
        code, label = status.HTTP_403_FORBIDDEN, "无权限"
    elif isinstance(exc, LookupError):
        code, label = status.HTTP_404_NOT_FOUND, "未找到"
    else:
        code, label = status.HTTP_400_BAD_REQUEST, "请求不合法"

    logger.info("业务异常 [%s] %s %s -> %d：%s",
                type(exc).__name__, request.method, request.url.path, code, exc)
    return JSONResponse(
        status_code=code,
        content={"detail": str(exc), "kind": type(exc).__name__, "label": label,
                 "path": request.url.path},
    )


async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    """未预期异常 → 500。

    **只在 DEBUG 下把 ``str(exc)`` 带出去。** 生产环境里异常文本可能包含
    内网路径、SQL 片段、甚至 SQLite 的文件名 —— 这些正好是安全扫描器爱报的东西。
    所以给一个 ``error_id``，日志里有完整堆栈可对。
    """
    error_id = _error_id()
    logger.exception("未处理异常 [%s] %s %s", error_id, request.method, request.url.path)
    body: dict[str, Any] = {"detail": "服务内部错误，请把错误编号提供给管理员", "error_id": error_id,
                            "path": request.url.path}
    if settings.DEBUG:
        body["exception"] = f"{type(exc).__name__}: {exc}"
    return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content=body)


def _register_handlers(app: FastAPI) -> None:
    from backend.app.services.case_service import (
        CaseError,
        CaseForbidden,
        CaseNotFound,
    )
    from backend.app.services.chat_history_service import (
        ChatHistoryError,
        ChatHistoryForbidden,
        ChatHistoryNotFound,
    )
    from backend.app.services.draft_service import DraftError
    from backend.app.services.knowledge_service import KnowledgeError
    from backend.app.services.user_service import UserError

    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    # 顺序无关，但都要注册：**不注册的异常会变成 500**，
    # 而 500 的响应体不带 detail，前端只能弹"系统错误"。
    for cls in (UserError, CaseError, DraftError, KnowledgeError, ChatHistoryError):
        app.add_exception_handler(cls, _business_error)
    app.add_exception_handler(CaseNotFound, _business_error)
    app.add_exception_handler(CaseForbidden, _business_error)
    app.add_exception_handler(ChatHistoryNotFound, _business_error)
    app.add_exception_handler(ChatHistoryForbidden, _business_error)
    app.add_exception_handler(Exception, _unhandled)


# =========================================================================== 中间件
def _add_middleware(app: FastAPI) -> None:
    origins = settings.cors_origin_list
    if "*" in origins and settings.ENV == "prod":
        # 带凭证的请求不允许 ``*``。生产上开 ``*`` 等于任何站点都能拿着律师的
        # 浏览器会话调这个后端。允许，但要吵一句。
        logger.warning(
            "CORS_ORIGINS=* 且 ENV=prod —— 建议改成具体域名（如 https://law.internal），"
            "否则任意站点都能在律师浏览器里调用本后端"
        )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials="*" not in origins,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Elapsed-Ms"],
    )

    @app.middleware("http")
    async def _request_context(request: Request, call_next):
        """请求 ID + 耗时。

        ``X-Request-ID`` 由网关传入则沿用（这样一次调用能跨服务串起来），
        没传就生成 —— 前端把它带进报障信息里，就能直接从日志定位这一次请求。
        """
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request.state.request_id = rid
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # 交给异常处理器；这里只补耗时日志，**不要吞掉异常**。
            logger.warning("[%s] %s %s 抛异常，耗时 %.0fms", rid, request.method,
                           request.url.path, (time.perf_counter() - t0) * 1000)
            raise
        elapsed_ms = (time.perf_counter() - t0) * 1000
        response.headers["X-Request-ID"] = rid
        response.headers["X-Elapsed-Ms"] = f"{elapsed_ms:.0f}"

        # 慢请求按 WARNING 打：健康检查每 5 秒一次、静态资源每次页面加载都几十个，
        # 全打 INFO 会把日志淹掉。只有"慢"才是信号。
        if elapsed_ms > 1000:
            logger.warning("[%s] %s %s -> %d 耗时 %.0fms（慢）", rid, request.method,
                           request.url.path, response.status_code, elapsed_ms)
        elif settings.DEBUG:
            logger.info("[%s] %s %s -> %d %.0fms", rid, request.method, request.url.path,
                        response.status_code, elapsed_ms)
        return response


# =========================================================================== 应用
DESCRIPTION = """
律所内网部署的**律师专用多 Agent 法律咨询助手**后端。

- **同一后端、双端入口**：EXE 瘦客户端与 Web 工作台调的是同一套接口与同一套 Agent 编排。
- **案件隔离**：案件按「承办律师本人 / 所属团队」隔离，管理员可跨团队查看。
- **可追溯**：每次任务都留 Trace（节点级）与引用记录（法条级），并落审计文件。
- **降级可见**：向量库、Redis、消息队列任何一个不可用时系统仍可出报告，
  但 `/health` 的 `degraded` 字段会如实列出降级项与回落实现。

> 免责声明：{}""".format(settings.DISCLAIMER)


def create_app() -> FastAPI:
    configure_logging()

    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.state.started_at = time.time()

    _add_middleware(app)
    _register_handlers(app)

    # 顺序硬要求：
    #  1) 根路径探针（/health）—— LB 探它，不能被 API 前缀影响
    #  2) 业务 API（/api/v1/...）
    #  3) WebSocket（/ws/...）—— **不带 API 版本前缀**，见 api/v1/__init__.py 的说明
    #  4) 前端静态（/ui）—— 它有自己的前缀，不会抢走上面三类
    app.include_router(probe_router)
    app.include_router(api_router, prefix=settings.API_PREFIX)
    app.include_router(ws_routes.router)
    _mount_frontend(app)

    @app.get("/", include_in_schema=False)
    def root():
        """首页：前端存在就跳过去，不存在就给一段 JSON 说明。

        **它是路由而不是 ``RedirectResponse`` 写死的**：前端没构建时
        跳转会让浏览器停在一个 404 上，而真正的信息是"你还没构建前端"。
        """
        index = WEB_DIR / "index.html"
        if index.exists():
            return RedirectResponse(f"{WEB_ROUTE}/", status_code=307)
        return JSONResponse({
            "app": settings.APP_NAME,
            "version": settings.APP_VERSION,
            "docs": "/docs",
            "api": settings.API_PREFIX,
            "web": False,
            "note": f"未发现 {index}；当前仅提供 API。构建前端后访问 / 即跳转到 {WEB_ROUTE}/。",
        })

    logger.info("%s v%s 装配完成：API 前缀 %s，前端 %s",
                settings.APP_NAME, settings.APP_VERSION, settings.API_PREFIX,
                WEB_ROUTE if (WEB_DIR / "index.html").exists() else "未构建")
    return app


app = create_app()


__all__ = ["app", "configure_logging", "create_app", "run_startup_check"]


if __name__ == "__main__":
    import uvicorn

    _HOST, _PORT = "127.0.0.1", 8001
    print("=" * 56)
    print("  前端页面 : http://127.0.0.1:8001/ui/")
    print("  接口文档 : http://127.0.0.1:8001/docs")
    print("  按 Ctrl+C 停止服务")
    print("=" * 56)
    uvicorn.run("backend.app.main:app", host=_HOST, port=_PORT)
