"""全局配置中心。

设计要点（对应方案第三章技术选型总表）：

1. 所有基础设施都做到「生产用真组件、开发可降级」，降级开关集中在 ``*_ENABLED`` 字段，
   且每次启动都会把「实际生效的后端」打印出来，避免出现「配了但没生效」的静默失效。
2. ``.env`` 与配置字段严格一一对应，启动时做一次对拍（``unmapped_env_keys``），
   把未映射的键暴露出来。
3. 路径全部基于 ``PROJECT_ROOT`` 推导，避免到处硬编码。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> backend/app -> backend -> <project root>
PROJECT_ROOT = Path(__file__).resolve().parents[2]

BACKEND_DIR = PROJECT_ROOT / "backend"
DATA_DIR = PROJECT_ROOT / "data"
WEB_DIR = PROJECT_ROOT / "web"
CLIENT_DIR = PROJECT_ROOT / "client"
DOCS_DIR = PROJECT_ROOT / "docs"


class Settings(BaseSettings):
    """环境配置。字段名小写，环境变量大小写不敏感。"""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------------------------------------------------------------- 基础
    APP_NAME: str = "律师专用多 Agent 法律咨询助手"
    APP_VERSION: str = "1.0.0"
    ENV: Literal["dev", "test", "prod"] = "dev"
    DEBUG: bool = True
    SECRET_KEY: str = "change-me-in-production-legal-agent"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 12
    API_PREFIX: str = "/api/v1"
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    WORKERS: int = 1
    # 前端静态资源由后端直接托管，Web 端零构建、零安装
    SERVE_WEB: bool = True
    CORS_ORIGINS: str = "*"
    # 是否信任 X-Forwarded-For / X-Real-IP 里的来源 IP。
    # **默认关闭**：直连时任何人都能伪造这两个头，信了它审计里记的 IP 就是假的，
    # 而审计的作用正是"不可抵赖"。只在确实走 Nginx 反代的内网部署里打开。
    TRUST_PROXY_HEADERS: bool = False

    # ---------------------------------------------------------------- 模型推理
    # 生产：H20 + SGLang；也兼容 vLLM / DeepSeek / 任意 OpenAI 兼容网关
    LLM_PROVIDER: Literal["auto", "openai", "ollama", "mock"] = "auto"
    LLM_BASE_URL: str = "http://127.0.0.1:30000/v1"
    LLM_API_KEY: str = "EMPTY"
    LLM_MODEL: str = "qwen3-8b-legal-lora"
    LLM_TIMEOUT: int = 120
    LLM_MAX_TOKENS: int = 2048
    LLM_TEMPERATURE: float = 0.2
    LLM_ENABLE_THINKING: bool = False
    # 报告生成是否开启流式输出（默认关闭，行为与改造前一致）。
    # 开启前确认前端 SSE 已按流式消费 /agents/assistant/stream 的增量帧，
    # 否则会收到整段一次性返回但响应体仍是非流式格式。
    LLM_STREAM: bool = False
    # 被"规则引擎兜底"钉住后，隔多少秒重新探测一次真模型（0 = 关闭）。
    # 背景：provider 只在首次调用时探一次并缓存；后端刚启动那几秒进程在加载向量/嵌入模型，
    # 探针的 4 秒超时容易不够 → openai/ollama 都判为不可用 → 整个进程永久走规则引擎，
    # 而且**不会自愈**（真机 2026-09-21 踩到，现象是接口正常但结果全是模板味/空值）。
    LLM_REPROBE_INTERVAL: int = 20
    # 探测模型服务的超时（秒）。远端网关 + 冷启动时原来硬编码的 4 秒太紧，
    # 会误判"不可用"→ 整个进程退回规则引擎（首个请求必然降级）。
    LLM_PROBE_TIMEOUT: int = 8

    # 合同审查的**第二个模型**（双模型交叉复核）：初审走 LLM_*（广撒网、宁可多报），
    # 复审走 LLM_REVIEW_*（严核查：去误报、降虚高、补漏报），两边可来自不同厂商。
    # 未配置 / 关闭 → 跳过复审并登记缺口（复审是增强项，不是硬依赖）。
    LLM_REVIEW_ENABLED: bool = False
    LLM_REVIEW_BASE_URL: str = ""
    LLM_REVIEW_API_KEY: str = "EMPTY"
    LLM_REVIEW_MODEL: str = ""

    # Ollama 备用通道（本机开发常用）
    OLLAMA_BASE_URL: str = "http://127.0.0.1:11434"
    OLLAMA_MODEL: str = "qwen2.5:7b"

    # 多模态识别（扫描件 / 手写批注 / 结构图）
    VLM_ENABLED: bool = True
    VLM_BASE_URL: str = "http://127.0.0.1:30001/v1"
    VLM_API_KEY: str = "EMPTY"
    VLM_MODEL: str = "qwen2.5-vl-7b"
    VLM_TIMEOUT: int = 90
    # 多模态转写用贪心解码（0）。它只消掉「采样随机」这一层；
    # 「填空下划线被数成几根」那种内容本身的模糊由 vision.client.normalize_scan_text 兜底。
    VLM_TEMPERATURE: float = 0.0

    # ---------------------------------------------------------------- 人工抽查闭环
    # 人工抽查比例（0~1）：合同审查完成后按此概率把任务标记为「待人工抽查」。
    # 0 = 关闭。标记落在 state["spot_check"]，并随报告正文顶部显式呈现。
    REVIEW_SPOT_CHECK_RATE: float = 0.0
    # 复审分歧持久化文件（自我改进）：累计统计 + 最近若干条分歧样本，
    # 复审提示词从这里读回并作为常驻纪律（见 review_node._second_pass_feedback）。
    REVIEW_SECOND_PASS_STATE: str = str(DATA_DIR / "review" / "second_pass.json")
    # 人工抽查队列文件（待人工抽查任务 + 抽查结论）。低高频，文件足够、便于测试迁移。
    REVIEW_SPOT_CHECK_QUEUE: str = str(DATA_DIR / "review" / "spot_check.json")

    # ---------------------------------------------------------------- 向量检索
    EMBEDDING_PROVIDER: Literal["auto", "openai", "local", "hash"] = "auto"
    EMBEDDING_BASE_URL: str = "http://127.0.0.1:30000/v1"
    EMBEDDING_API_KEY: str = "EMPTY"
    EMBEDDING_MODEL: str = "BAAI/bge-small-zh-v1.5"
    EMBEDDING_DIM: int = 512
    EMBEDDING_BATCH: int = 32
    # 单机部署可直接填写 Hugging Face / sentence-transformers 模型目录。
    # 留空时才按项目 data/models 下的候选目录查找。
    EMBEDDING_LOCAL_MODEL_PATH: str = ""
    RERANKER_LOCAL_MODEL_PATH: str = ""

    MILVUS_ENABLED: bool = True
    MILVUS_URI: str = "http://127.0.0.1:19530"
    MILVUS_TOKEN: str = ""
    MILVUS_DB: str = "legal_agent"
    MILVUS_COLLECTION_STATUTE: str = "legal_statute"
    MILVUS_COLLECTION_CASE: str = "legal_case"
    MILVUS_COLLECTION_KNOWLEDGE: str = "legal_knowledge"
    MILVUS_COLLECTION_MEMORY: str = "legal_memory"

    # 向量不可用时的兜底实现（进程内 numpy 索引，落盘复用）
    VECTOR_FALLBACK_TO_LOCAL: bool = True

    # ---------------------------------------------------------------- 关键词检索
    BM25_ENABLED: bool = True
    # 默认 Whoosh（架构既定：Milvus 向量 + Whoosh BM25 混合检索）。
    # 内置纯 Python 实现降级为**兜底**：Whoosh 导入/初始化失败时自动回落，
    # 且首次启用 Whoosh 时若索引为空，会自动把内置索引里的语料迁过去（见
    # ``rag/retriever/bm25.py`` 的 ``get_bm25``）—— 切换引擎不会让检索结果变空。
    BM25_ENGINE: Literal["builtin", "whoosh"] = "whoosh"
    BM25_INDEX_DIR: str = str(DATA_DIR / "index" / "bm25")
    BM25_K1: float = 1.5
    BM25_B: float = 0.75
    RETRIEVE_TOP_K: int = 20

    # 启动时预热检索链路（加载 embedding 权重、连向量库、载 BM25 索引）。
    # 不预热时这些成本会落在**第一个真实请求**头上，实测 17.8 秒。
    # 关掉它的场景：只跑 API 不跑检索的管理节点 —— 那时 BGE 白占一份内存。
    WARMUP_ENABLED: bool = True
    RERANK_TOP_K: int = 6
    RRF_K: int = 60

    # 周期进化调度器（``services/evolution_scheduler.py``）：进程内按
    # ``state.scheduler.interval_days`` 到点触发 ``run_cycle``。
    # 此前 ``scheduler.enabled`` 只是个没人消费的标记，飞轮不点按钮就永远不前进。
    # 两级开关：这里 False → 进程内彻底不启动（运维急停钮）；
    # state 里 ``scheduler.enabled=False`` → 调度器照跑，但每次检查直接跳过。
    # 首次启动只排期、不立刻跑一轮，所以默认开启不会造成"重启即进化"。
    EVOLUTION_SCHEDULER_ENABLED: bool = True
    # 调度器的**检查间隔**（秒），不是进化周期 —— 周期由 interval_days 决定，
    # 这里只是"多久看一眼到点没有"。60 秒足够，单次开销仅一次 state 读。
    EVOLUTION_SCHEDULER_TICK_SECONDS: float = 60.0

    # ---------------------------------------------------------------- 存储
    # 生产 MySQL，开发可回落 SQLite（同一套 SQLAlchemy 模型）
    DB_ENABLED: bool = True
    DB_URL: str = "mysql+pymysql://root:123456@127.0.0.1:3306/legal_agent?charset=utf8mb4"
    DB_ECHO: bool = False
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    SQLITE_FALLBACK_URL: str = f"sqlite:///{(DATA_DIR / 'legal_agent.db').as_posix()}"

    REDIS_ENABLED: bool = True
    REDIS_URL: str = "redis://127.0.0.1:6379/0"
    REDIS_TTL_SECONDS: int = 60 * 60 * 24  # 短期记忆 24h

    RABBITMQ_ENABLED: bool = False
    RABBITMQ_URL: str = "amqp://guest:guest@127.0.0.1:5672/%2F"
    RABBITMQ_QUEUE: str = "legal_agent.tasks"

    # ---------------------------------------------------------------- A2A 多 Agent 协同
    # Agent 间的委派通道：
    # - ``in_process``：同进程直接调节点函数（默认，零网络开销，单机部署够用）；
    # - ``http``：把委派发到**独立部署**的 Agent 实例（跨进程/跨机），
    #   由 ``A2A_REMOTE_BASE`` 指向对方地址，对方用
    #   ``POST /api/v1/a2a/remote/{agent}`` 接住（见 api/v1/agents.py）。
    # 跨机能力发现走 ``GET /api/v1/a2a/remote/{agent}/card``。
    A2A_TRANSPORT: Literal["in_process", "http"] = "in_process"
    A2A_REMOTE_BASE: str = ""
    A2A_TIMEOUT_MS: int = 30_000
    # 远端实例的 Bearer 令牌；留空表示内网互信、不发鉴权头。
    A2A_TOKEN: str = ""
    # 委派的三个上限（原先硬编码在 ``protocol/delegation.py``，2026-10-07 提到这里）。
    # **默认值与原来逐字一致，行为不变**，提上来只为让它可调、可测。
    # ⚠ 别在 ``delegation.py`` 里再定义一套同名模块常量：它会和 settings 各说各话，
    # ``diagnostics()`` 报的数与实际生效的不是一回事（``graph.MAX_LOOP`` 踩过同一个坑）。
    DELEGATION_MAX_IN_ROUTE: int = 4      # 档一：把本次**该跑的**节点叫回来
    DELEGATION_MAX_EXPANSION: int = 1     # 档二：拉一个起手范围外的 Agent（更该被质疑，所以更严）
    DELEGATION_MAX_DEPTH: int = 2         # 委派链深度：挡 A→B→A 这类互相委派的环路

    # ---------------------------------------------------------------- 任务队列
    # worker 轮询间隔。5 秒是"入队到开始跑"的延迟上限：
    # 再短对"律师盯着进度条"没有可感知的差别，却会让空闲 worker 持续查库 ——
    # 而 worker 通常是好几份同时跑，空转的查询量是乘以份数的。
    # 有 broker 时它只是**兜底**间隔（正常路径靠消息唤醒，见 worker/app.py）。
    WORKER_POLL_SECONDS: float = 5.0
    # 一次轮询最多认领多少条。单 worker 并发度也由它决定 ——
    # 一次合同审查要占住检索与 LLM 通道几十秒到几分钟，
    # 批量取太多只是把队列挪到内存里，还会让一份 worker 把 CPU 吃满。
    WORKER_BATCH: int = 4
    # 单个任务执行的硬超时（秒）。0 表示不限。
    # 不限的风险是"卡死的任务永久占住一个 worker 名额"——
    # 表现是队列一直有货但吞吐越来越低，而日志里看不到任何错误。
    WORKER_TASK_TIMEOUT: float = 1800.0
    # 判定"卡死在 running"的阈值（秒）。0 = 按 WORKER_TASK_TIMEOUT 的两倍自动算。
    # **不要设得比任务超时小**：那会把正在跑的长任务也放回队列，
    # 接着被另一个 worker 认领，同一任务并发执行两遍。
    WORKER_STALE_SECONDS: float = 0.0

    # ------------------------------------------------- 采集（Playwright 浏览器自动化）
    # 四层：调度（条件队列 / 断点 / 单日配额）→ 访问（登录态 / 验证码 / 延迟 / 重试 /
    # 代理）→ 解析（DOM + 正则兜底）→ 存储（doc_id + 正文哈希双去重）。
    #
    # **默认关闭**：它依赖 playwright + 一份几百 MB 的浏览器内核，且面向外部站点，
    # 属于"要用才开"的能力。关闭时不 import playwright、不起浏览器，
    # 采集工具在编排里直接报降级（见 runtime_backends 的 crawler 组）——
    # 而不是让服务在启动阶段就 import 失败。
    CRAWL_ENABLED: bool = False
    # 无头模式。**验证码转人工时它决定能不能真的转**：无头下没人能看见画面，
    # 只能截图存档 + 挂起任务等有头模式重跑；需要实时人工过验证码时把它设 false。
    CRAWL_HEADLESS: bool = True
    # 相邻两次**站点请求**之间的随机停顿（秒）。给的是一个区间而不是定值：
    # 固定间隔本身就是一种可识别的机器特征，区间随机更接近真人翻页节奏。
    # 这是防封的主力，调小它提速最明显、也最容易被限流。
    CRAWL_DELAY_MIN: float = 3.0
    CRAWL_DELAY_MAX: float = 8.0
    # 文件下载（站点给的 OSS 直链，独立域名）的随机停顿，**独立于上面的接口间隔**。
    # 为什么可以更短：OSS 是对象存储，不是被 robots 约束的检索接口；站点自己的
    # "下载"按钮点一下就拉它，正常使用本就没有秒级间隔。与检索接口共用同一个
    # 间隔，结果是采集时间大半花在"等一个静态文件"上 —— 实测每条要等两次，
    # 11/13 秒的耗时都在sleep。站点接口仍严格走 CRAWL_DELAY_MIN/MAX。
    CRAWL_DOWNLOAD_DELAY_MIN: float = 1.0
    CRAWL_DOWNLOAD_DELAY_MAX: float = 2.0
    # 单次请求的失败重试次数与指数退避。退避封顶是硬需求：
    # 不封顶时 2^n 会很快涨到几十分钟，一个卡住的采集会把整个队列拖死。
    CRAWL_MAX_RETRY: int = 3
    CRAWL_RETRY_BACKOFF: float = 2.0
    CRAWL_RETRY_BACKOFF_MAX: float = 60.0
    # 单页导航与选择器等待超时（毫秒）。
    CRAWL_TIMEOUT_MS: int = 30_000
    # 单日每源配额（条）。0 = 不限。触顶后调度层把任务**挂起**而不是失败，
    # 次日零点（本地时区）自动可继续 —— 失败会把断点丢掉，挂起能接着跑。
    CRAWL_DAILY_QUOTA: int = 500
    # 有头模式下等人工过验证码的最长秒数；0 = 不等，直接转人工挂起。
    CRAWL_CAPTCHA_WAIT_SECONDS: float = 300.0
    # 命中任一关键词即判定为验证码/人机校验页（正文里出现，而非精确相等）。
    CRAWL_CAPTCHA_KEYWORDS: str = "验证码,请输入验证码,人机验证,拖动滑块,安全验证,访问过于频繁"
    # 代理池（逗号分隔，如 http://user:pass@host:port）。留空表示直连。
    # 预留语义：每次请求独占一个代理条目，用完归还并按 CRAWL_PROXY_MAX_USES 计毁损。
    CRAWL_PROXY_POOL: str = ""
    # 单个代理最多被复用多少次（超过即视为污染/被封，退出池子）。
    # 0 = 不限次数（仅做轮询）。
    CRAWL_PROXY_MAX_USES: int = 0
    # 采集产生的状态文件（登录态 storage_state、断点兜底）与工件（验证码截图、
    # 失败页面快照）目录。
    CRAWL_STATE_DIR: str = str(DATA_DIR / "crawl" / "state")
    CRAWL_ARTIFACT_DIR: str = str(DATA_DIR / "crawl" / "artifacts")
    CRAWL_USER_AGENT: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    # ---- 浏览器运行优化（详见 docs/playwright-crawl-optimization.md）----
    # 阻断图片/媒体/字体等对文本采集无价值、却显著拖慢加载的资源。
    # **不阻断 stylesheet**：正文抽取依赖渲染（``inner_text`` 会排除 CSS 隐藏节点），
    # 去掉样式会让本该隐藏的文本混进正文；纯接口驱动站点可自行再加 stylesheet。
    CRAWL_BLOCK_RESOURCES: bool = True
    # 单个浏览器上下文最多取多少页后重建（0=不限）。**与代理复用解耦**：不轮换时
    # 长跑内存只增不减，且会话 cookie 越老越容易触发风控，而默认代理复用上限是 0
    # （永不轮换），所以必须有这条独立阈值兜底。
    CRAWL_CONTEXT_MAX_PAGES: int = 200
    # 文件下载（``fetch_bytes``）失败重试次数。站点给的 OSS 直链偶发抖动，
    # 不该让一条已经检查过的法规整条作废。
    CRAWL_DOWNLOAD_RETRY: int = 2
    # 容器内以 root 运行时需要 ``--no-sandbox`` 才能启动 Chromium。默认关闭。
    CRAWL_LAUNCH_NO_SANDBOX: bool = False
    # 失败时落 Playwright trace（含页面快照，体积较大）便于复盘。默认关闭。
    CRAWL_TRACE_ON_ERROR: bool = False
    # 国家法律法规数据库采集的落盘目录。**刻意放在 data/raw 之下**：
    # 采到的 .docx 就是法规原文，直接复用 ``backend.corpus.ingest``
    # （它会 rglob 扫 RAW_DIR），不必为采集另建一条入库通道。
    # 这里直接拼路径而不是引用 ``RAW_DIR``：那个字段在"合规"段才定义，
    # 在类体里提前引用会 NameError（类体是按顺序求值的）。
    CRAWL_FLK_DIR: str = str(DATA_DIR / "raw" / "国家法律法规库")
    # **按主题目录落盘**：检索词在 ``data/raw/`` 下有同名目录时，该主题采到的法规
    # 就存进那个目录（``raw/婚姻法/`` → ``data/raw/婚姻法/``），而不是一股脑塞进
    # ``CRAWL_FLK_DIR``。没有同名目录时回落到 ``CRAWL_FLK_DIR``。
    # 关掉它 = 全部落 ``CRAWL_FLK_DIR``（旧行为）。
    CRAWL_FLK_GROUP_BY_TOPIC: bool = True

    # ---- 业务领域分类落盘（**让模型自己识别领域、自己建目录**）
    # 站点只给**效力层级**（法律/行政法规/司法解释…），**没有业务领域字段**，
    # 所以业务领域只能自己判断。打开本开关后，采集时把标题交给已配置的模型，
    # 由它决定归入哪个领域目录（``data/raw/<领域>/``），没有对应目录就新建。
    #
    # 为什么不用关键词规则表：规则表只能覆盖你**事先想到**的词，遇到没预料到的
    # 法规就漏分，而且表要人维护。模型能复用已有领域名、也能为新法规新建。
    CRAWL_FLK_AUTO_TOPIC: bool = False
    # 一次交给模型多少个标题。批太小 → 调用次数多、慢且贵；批太大 → 一次输出长、
    # 容易截断（截断会导致整批退回未分类）。20 实测稳妥。
    CRAWL_FLK_AUTO_TOPIC_BATCH: int = 20
    # **没有"标题 → 领域"的缓存文件**：分类结果就记在**文件所在目录**上
    # （``data/raw/<领域>/``）—— 已归位的法规永不重新判定，重跑幂等；也不会出现
    # "缓存说劳动法、文件在劳动关系"这种两边各自看都"没错"的偏差。
    # 详见 backend/crawler/topics.py。

    # ---- 兜底：关键词规则表（模型不可用时才会走到这里）
    # 格式：``领域=关键词1,关键词2``，多条用 ``;`` 分隔；也可写进文件（每行一条）。
    # 顺序即优先级（先匹配先赢）。**没配就是空的**，此时模型失败就落默认目录。
    CRAWL_FLK_TOPIC_RULES: str = ""
    # 规则文件路径（每行 ``领域=关键词1,关键词2``，``#`` 开头为注释）。
    CRAWL_FLK_TOPIC_RULES_FILE: str = ""

    # flk 检索接口一页的条数。站点默认 20，改大能少翻几页，
    # 但响应体也线性变大（含 titleHightLightList 高亮片段）。
    CRAWL_FLK_PAGE_SIZE: int = 20
    # 只采**现行有效**的法规（默认开）。站点在检索结果里用 ``sxx`` 标记时效性，
    # 实测取值只有三个：**3=现行有效、2=已修改、1=已废止**。
    #
    # 站点不给映射表（``enumData`` 里的 ``flcaStatus`` 恒为 0），这三个值是用
    # 已知状态的法规反推的：《婚姻登记条例》2025-04-06 新版=3；同一部条例的
    # 2024/2003 旧版=2；《中华人民共和国婚姻法》=1 —— 民法典 2021 年施行后
    # 它确已废止，这条是能独立验证的锚点。
    #
    # 为什么要过滤：站点会把历史版本和失效件一起返回，而且**标题里常常不带
    # "失效"字样**（实测 136 个已下文件里只有 4 个标了）。全采下来既费配额，
    # 又会让检索结果新旧混排 —— 律师引到一条已废止的条文是要出事的。
    CRAWL_FLK_ONLY_EFFECTIVE: bool = True
    # 上面认定的"现行有效"编码。站点若调整编码，改这里即可，不用动代码。
    CRAWL_FLK_SXX_EFFECTIVE: int = 3

    # ---------------- flk：配置驱动的采集清单
    # 以上几项都是"参数"，下面这组才是"我要采什么"。**空着就什么都不采**。
    # 规则：命令行显式给了 --names/--file/--keyword/--category 时，只用命令行的；
    # 一个都没给才读这里 —— 避免"改了配置没生效"和"配置与命令行打架"两种困惑。
    #
    # 法规名单：逗号分隔的**完整标题**，走精确匹配。
    # 名字本身含逗号时改用 CRAWL_FLK_NAMES_FILE。
    # 例：中华人民共和国民法典,中华人民共和国刑法,婚姻登记条例
    CRAWL_FLK_NAMES: str = ""
    # 名单文件路径（每行一个，# 开头为注释）。长名单用文件更可维护，
    # 也便于用版本管理跟踪"到底采了哪些法"。
    CRAWL_FLK_NAMES_FILE: str = ""
    # 关键词列表：逗号分隔，走**模糊**检索。适合"婚姻相关"这类范围不确定的需求。
    # 注意：单个词召回有限，且"婚姻"这类词采不到《民法典》（名字里没有它），
    # 所以关键词与上面的名单是**互补**关系，不是二选一。
    CRAWL_FLK_KEYWORDS: str = ""
    # 级别过滤：逗号分隔，取值是站点的分类名
    # （宪法 / 法律 / 行政法规 / 监察法规 / 地方法规 / 司法解释）。
    # 留空 = 不限级别。加它能避免被海量地方法规淹没。
    CRAWL_FLK_CATEGORIES: str = ""
    # 同名多版本是否全要。默认 false：只留公布日期最新的那份，
    # 否则旧版一并入库、检索结果里新旧混排。
    CRAWL_FLK_ALL_VERSIONS: bool = False

    # ---------------- flk：自动目录模式（**不需要手填清单**）
    # 打开后按**分类**从新到旧翻页增量采集：本地已有跳过、非现行有效过滤、
    # 其余下载。连续 STOP_PAGES 页全部已存在即判定更旧的都采过了，停止该分类。
    #
    # 为什么不做"按发布日期过滤"：站点的日期过滤参数格式随版本变动，写死会
    # **静默失效**（筛了跟没筛一样，表现为每轮都在重采全站）。改用"排序稳定 +
    # 文件幂等"这条不依赖私有参数的路径，代价是首次要多翻几页。
    CRAWL_FLK_AUTO: bool = False
    # 自动模式的分类范围。默认只含**国家级**，不含"地方法规"——
    # 地方法规有数万条且与本地业务相关性低，放进来自动跑会常年吃满配额。
    # 需要时加进来，或配合 CRAWL_FLK_AUTO_MAX_PAGES 限量。
    CRAWL_FLK_AUTO_CATEGORIES: str = "宪法,法律,行政法规,监察法规,司法解释"
    # 每个分类最多翻几页（0 = 不限，翻到连续整页已存在为止）。
    CRAWL_FLK_AUTO_MAX_PAGES: int = 0
    # 连续多少页"整页都已存在"就停止该分类。默认 1：翻到的最新一页若全是旧文件，
    # 说明这个分类已经追平了。调大更保守（多翻几页确认），调小更快。
    CRAWL_FLK_AUTO_STOP_PAGES: int = 1

    # ---------------- flk：从 data/raw/ 推导采集目标（**不用手填清单**）
    # 打开后按你在 data/raw/ 下的**目录名**当关键词去模糊检索 —— 目录名即业务主题
    # （如 raw/劳动法/ → 检索"劳动法"，把《劳动法》《劳动合同法》《劳动争议调解仲裁法》
    # 等一并采下来）。这就是"我在 raw 里怎么分，就怎么采"。
    #
    # 为什么目录名走**模糊**而不是精确标题：目录名是主题（"劳动法"）而不是法规全称；
    # 文件名里也常见简称（raw/劳动法/民法典.pdf），精确标题一律匹配不上。
    CRAWL_FLK_FROM_RAW: bool = False
    # 是否把**文件名**也一并当检索词（自动去掉扩展名与 `_日期` 后缀）。
    # 默认 false：本机 raw/ 有 80+ 个文件，文件名级会产生同样数量的检索任务，
    # 请求量很大；目录名（主题级）通常已够覆盖。要逐部精确补时再打开。
    CRAWL_FLK_RAW_INCLUDE_FILES: bool = False
    # 最多取多少个检索词（0 = 不限），防止 raw/ 很大时任务数失控。
    CRAWL_FLK_RAW_MAX_TERMS: int = 0
    # 额外排除的目录/文件名关键字（逗号分隔）。内置已排除：采集输出目录
    # （CRAWL_FLK_DIR）、文书模板目录、名字含"案例/模板"的项。
    CRAWL_FLK_RAW_EXCLUDE: str = ""

    # ---------------- 采集自动化（定时触发，见 backend/crawler/runner.py）
    # 由「Windows 任务计划程序 / cron」周期调用 `python -m backend.crawler.runner --once`。
    # **没有单独的定时开关**：采集总开关就是 CRAWL_ENABLED，定时与否由计划任务决定 ——
    # 两个开关里必然有一个会变成"配了不生效"的死配置。
    # 一轮里分别跑哪两路。分开是为了单独排查某一路（案例源往往要单独调选择器）。
    CRAWL_RUN_STATUTES: bool = True
    CRAWL_RUN_CASES: bool = True
    # 法条采完是否自动建索引（调 `backend.corpus.ingest --statutes`）。
    # 关掉它 = 只下载文件到 data/raw，入库另行手工做。
    CRAWL_INGEST_STATUTES: bool = True
    # 常驻模式（`--serve`）两轮之间的间隔（分钟）。用 Windows 任务计划时用不到。
    CRAWL_INTERVAL_MINUTES: int = 1440
    # 采集时间窗（本地时区），如 "22-6" 表示 22:00~06:00；留空 = 不限。
    # 跨零点必须显式支持（见 scheduler.TimeWindow）：写成 22→6 若无此支持会恒不执行。
    CRAWL_TIME_WINDOW: str = ""

    # ---------------- 案例源：中国裁判文书网（wenshu）采集清单
    # ⚠ 合规：该站有严格使用条款且部署了登录与验证码校验，必须**已获授权**再自动化。
    # 本适配器沿用 flk 的边界：不破验证码（命中即转人工）、随机间隔、单日配额、不伪装身份。
    #
    # 首次使用建议 `--headed` 跑一次：人工登录并过验证码，登录态会落盘
    # （CRAWL_STATE_DIR/wenshu.json），之后无头模式即可续跑。
    CRAWL_WENSHU_ENABLED: bool = False
    # 关键词（案由/争议焦点），逗号分隔。留空 = 只用列表页默认结果（谨慎）。
    CRAWL_WENSHU_KEYWORDS: str = ""
    # 法院名，逗号分隔。只记进 doc.extra 供人工核对，不做站点侧过滤
    # （站点的法院过滤参数随版本变动，硬编码会静默失效）。
    CRAWL_WENSHU_COURTS: str = ""
    # 每个关键词最多翻几页、站点一页的条数。
    CRAWL_WENSHU_MAX_PAGES: int = 1
    CRAWL_WENSHU_PAGE_SIZE: int = 20
    # 列表页地址。留空 = 用 sites/wenshu.py 的默认首页（检索页）。
    CRAWL_WENSHU_LIST_URL: str = ""
    CRAWL_WENSHU_PAGE_PARAM: str = "page"
    # 关键词拼进 URL 的参数名。**站点若不认 URL 参数**（列表由 JS 触发检索），
    # 就把它留空，并改用「固定列表页 URL + 关键词留空」的方式（见 docs/crawl-automation.md）。
    CRAWL_WENSHU_KEYWORD_PARAM: str = "searchKeyword"
    # 详情链接正则（须含命名组 u）。留空 = 用 sites/wenshu.py 默认值。
    # 站点改版后在这里覆盖，**不必改代码**。
    CRAWL_WENSHU_LINK_PATTERN: str = ""
    # 详情页选择器，形如 `body=#content,title=h1`。逗号分隔、等号赋值，
    # 与 `python -m backend.crawler --selectors` 同格式。留空 = 用默认值。
    CRAWL_WENSHU_SELECTORS: str = ""

    # ---------------------------------------------------------------- 业务参数
    # 步数上限：方案 4.3 写的是「max_steps = 6~8，超限即停」，落地时必须上调到 16。
    # 理由是方案自己还要求「Validator 失败回环」，两个约束按 6~8 设限会互相打架：
    #   · 最长单趟路径 contract_review = 1（Intent）+ 8（Intake/Statute/Case/Knowledge/
    #     Review/Validator/Draft/Report）= 9 步 —— 已经超出 8；
    #   · 回环还要重跑 5 个节点（见 graph.LOOP_RERUN_TAIL），
    #     而启动回环的预算检查是 steps + len(rerun) <= MAX_STEPS，MAX_STEPS=8 时
    #     这个条件恒不成立，回环直接退化成死代码。
    # 后果不是"更安全"，而是**每一单合同审查都会报「已达最大步数」**，
    # 并把这条噪声灌进报告的证据缺口清单 —— 律师会对整份清单失去信任，
    # 这比不设上限更危险。所以取 16 = 最长路径 9 + 一次完整回环 5 + 2 步余量。
    # 真正的失控防护是第一层之外的三层（工具配额 / 状态增量 / Validator 终检）
    # 以及图的 RECURSION_LIMIT 与执行器硬保险。
    MAX_STEPS: int = 16
    TOOL_TIMEOUT: int = 30
    TOOL_MAX_RETRY: int = 2
    TOOL_RETRY_BACKOFF: float = 0.5
    STATUTE_SEARCH_MAX_CALLS: int = 3
    CASE_SEARCH_MAX_CALLS: int = 2
    CITATION_MIN_CONFIDENCE: float = 0.6
    # 回环上限（原先硬编码在 ``graph.MAX_LOOP``，2026-10-07 提到这里）。
    # **默认 1 = 保持原行为**，提上来只为让它可调、可测。
    # ⚠ 调大到 2 时 ``MAX_STEPS`` 必须同步到 20：回环要重跑 5 个节点
    # （见 ``graph.LOOP_RERUN_TAIL``），9 步最长路径 + 2×5 + 1 余量。
    # 忘了同步不会报错，只会让第二轮回环的预算闸恒不成立 ——
    # 回环静默退化成死代码，正是上面 MAX_STEPS 那段注释说的坑。
    MAX_LOOP: int = 1

    # ------------------------------------------------------------ 动态编排
    # 固定路由表（INTENT_ROUTES）的问题是"意图相同 → 节点相同"：同一个 consult
    # 意图里，「竞业限制补偿金最低是多少」和「公司不签合同我能主张哪些权利」
    # 复杂度差一个量级，却跑同一条 7 节点链路（前者浪费、后者仍可能不够）。
    # 开启动态编排后，先按**确定性规则**算出候选节点，再可选让模型在区间内复核。
    DYNAMIC_PLAN_ENABLED: bool = True
    # 规则已能保证下限，模型只做"再挑一次"：它给的结果必须落在候选集内、
    # 且包含必需节点，否则丢弃沿用规则结果（模型不可用时同样沿用）。
    DYNAMIC_PLAN_LLM: bool = True

    # -------------------------------------------------------- Agent 自主循环
    # 让 4 个产品 Agent 真的能"思考 → 调工具 → 再思考"（2026-10-07 加）。
    #
    # 落点是**一轮跑完之后的裁决**：route 上最后一个待跑节点执行完时，控制器看一眼
    # 证据够不够，不够就自己调工具补、并把终审段排回 route 重跑。
    # 为什么不在每个节点后都思考：一次思考 = 一次 chat_json（本地模型 2~8s），
    # 8 个节点的链路就是 +16~64s，而类案检索本来就 45~60s。律师等不起，
    # 而且多数节点之后的证据状态并不会改变结论。只在"这一轮跑完了"这个点上问一次。
    #
    # ⚠ 记住这是**预算内的自由**：下面每个闸都是硬的，超了就地降级成"带证据缺口交付"，
    # 不会因为"agent 想再试一次"就多跑一轮。语义与 ``_should_loop`` 的四层防护一致。
    CONTROLLER_ENABLED: bool = True
    # 一个任务最多思考几轮（每轮 = 1 次裁决 + 最多 1 次行动）。
    # 取 2 而不是更大：第一轮补检索，第二轮要么成了、要么无新证据
    # （``state_manager.has_new_info`` 会判出来），再转只是烧 token。
    CONTROLLER_MAX_ROUNDS: int = 2
    # 控制器自己的 LLM 调用总上限（裁决一次算一次）。
    # **这是唯一兜住"新增成本"的闸**：节点自身的调用不受它管，也刻意不受管 ——
    # 若在 ``client.chat`` 上做全任务计数并在超限时回落规则引擎，
    # 合同审查会跑到一半静默降级成规则结果，交付物变样且不报错，比多花钱危险得多。
    CONTROLLER_LLM_MAX_CALLS: int = 4
    # 单次裁决的超时（秒）。超时就当"想不出来"，按 finish 处理 ——
    # 控制器是**可选增强**，它卡住绝不能拖住已经跑完的交付物。
    CONTROLLER_LLM_TIMEOUT: float = 12.0
    # 控制器自己直调工具的**正交**上限。必须单独设，不能只靠
    # ``STATUTE_SEARCH_MAX_CALLS`` / ``CASE_SEARCH_MAX_CALLS``：
    # ``knowledge_search`` 等 13 个工具压根没有配额，
    # 没有这一条，"agent 把知识库检索当免费工具刷"没有任何东西拦得住。
    CONTROLLER_MAX_TOOL_CALLS: int = 3

    # ------------------------------------------------ 产品 Agent 自主规划
    # 4 个产品入口从"门面配置 + 共享固定管线"改造为"各自独立的真正 Agent"后，
    # 每个产品拥有**自己的节点池 / 工具池 / 必需节点 / 规划与裁决提示词**
    # （见 backend/agents/products/runtime.py 的 PRODUCT_RUNTIMES）。
    # 这里控制的是"产品 Agent 是否在运行前用 LLM 自主规划本次节点序列"：
    # 开启后，钉了产品意图的任务由产品级规划器在**该产品的节点池内**决策
    # （必需节点不可删、force_report 控制收尾），失败自动回退原静态路由；
    # 关闭后走原 INTENT_ROUTES 固定路由，行为与改造前逐字一致（前端零变动兜底）。
    PRODUCT_PLAN_ENABLED: bool = True
    # 产品规划调用 LLM 的单次超时（秒）。超时/异常/模型不可用一律回退静态路由
    # （planning.refine_with_llm 同款纪律：可选增强不阻塞既定交付）。
    PRODUCT_PLAN_LLM_TIMEOUT: float = 12.0

    # ------------------------------------------------------------ 语义缓存
    # 相似问题直接复用已核验过的结论，不重跑整条链路。
    QUERY_CACHE_ENABLED: bool = True
    # 命中阈值（余弦相似度）。0.90 是保守值：宁可漏命中重跑，不可错命中复用。
    QUERY_CACHE_MIN_SIM: float = 0.90
    QUERY_CACHE_TOP_K: int = 3
    # 只缓存"值得复用"的结论：置信度过低或引用终审没过的，缓存它等于传播噪声。
    #
    # 0.60 而不是 0.70。依据是 compute_confidence 的可达上限，不是拍脑袋：
    # 无材料的咨询类问题拿不到"内容完整性 +0.10 / facts +0.05 / issues +0.05"
    # （这三项都要材料），法条 0.35 + 类案 0.20 + 引用核验 0.25 = 0.80 就是天花板，
    # 再扣掉几条证据缺口就落到 0.65 上下 —— 0.70 这一档实际上永不达标，
    # 缓存集合恒为空，"相似问题直接命中"等于没做（实测某次咨询 0.35）。
    # 主门槛是"引用终审通过且确实核过"（见 orchestrator._maybe_cache），
    # 置信度在这里只兜"内容太薄"这一类，0.60 足够。
    QUERY_CACHE_MIN_CONFIDENCE: float = 0.60
    QUERY_CACHE_TTL_DAYS: int = 30

    # ------------------------------------------------------- 合同审查扫描并发
    # 逐条风险扫描是整条链路里最慢的一步：一次要模型读完所有条款再吐结构化 JSON，
    # 实测 25 条款单次调用 56.8s（占整单 106s 的一半以上），瓶颈在**输出 token**
    # 而不是检索。按条款分块并发调用后，墙钟≈最慢的那一块，而不是各块之和。
    # REVIEW_SCAN_CHUNK_CLAUSES = 0 表示关掉分块，退回"一次全量扫描"。
    REVIEW_SCAN_CHUNK_CLAUSES: int = 8
    # 并发上限：实测每块本身就要 ~40s（单次调用的固定开销占了大部分），
    # 所以块数必须能**同时**发出去，否则第二批会把省下的时间又还回去。
    # 25 条款按 8 条切 = 4 块，取 4 让它们一波发完；块更多时自然排队。
    REVIEW_SCAN_CONCURRENCY: int = 4
    # 按风险重写合同条款时的并发（每条条款一次调用，只带这一条 + 风险 + 建议 + 依据，
    # 比把 10 条塞进一次调用更快，也更容易约束"只改这一条"）。
    # 10 条风险串行 ≈ 1~2 分钟，4 路并发后墙钟≈最慢的那一条，实测约 30~50s。
    REVISE_REWRITE_CONCURRENCY: int = 4
    # 法规检索的最大可接受「效力状态未知」比例，超过则整体标注证据缺口
    EVIDENCE_GAP_ALERT_RATIO: float = 0.34

    # ---------------------------------------------------------------- 合规
    DISCLAIMER: str = "本内容由 AI 辅助生成，仅供律师工作参考，不构成正式法律意见。"
    DESENSITIZE_ENABLED: bool = True
    AUDIT_ENABLED: bool = True
    AUDIT_LOG_DIR: str = str(DATA_DIR / "audit")
    UPLOAD_DIR: str = str(DATA_DIR / "uploads")
    EXPORT_DIR: str = str(DATA_DIR / "exports")
    PROCESSED_DIR: str = str(DATA_DIR / "processed")
    RAW_DIR: str = str(DATA_DIR / "raw")
    KNOWLEDGE_DIR: str = str(DATA_DIR / "knowledge")
    # 文书模板目录（.docx）。默认使用项目随附的劳动法模板；可由 .env 覆盖。
    DOC_TEMPLATE_DIR: str = str(DATA_DIR / "raw" / "劳动法" / "文书模板")
    # 模板解析结果缓存（扫描 35 个 docx 要解压 + 解析表格，不必每次请求都做）
    DOC_TEMPLATE_INDEX: str = str(DATA_DIR / "templates" / "index.json")
    MAX_UPLOAD_MB: int = 200
    # ``/contract/parse`` 的单文件上限。**比 ``MAX_UPLOAD_MB`` 严得多，是有意的**：
    # 那个端点把整份文件 ``await f.read()`` 进内存再落临时盘，图片还要走多模态整页
    # 转写，成本与体积成正比且没有上限；而它面向的是"律师随手拖一份合同进来"，
    # 50M 已经远超任何真实合同（含扫描件）。
    # ⚠️ 前端 index.html 的合同上传框文案写死了「50M 」，改这里要一并改文案。
    MAX_PARSE_MB: int = 50
    # EXE 本地不落盘案件原文：客户端侧开关
    CLIENT_PERSIST_LOCAL: bool = False

    # ---------------------------------------------------------------- 管理员种子
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin123"
    DEMO_LAWYER_USERNAME: str = "lawyer"
    DEMO_LAWYER_PASSWORD: str = "lawyer123"

    # ------------------------------------------------------------ 微信机器人接入
    # 律师在微信里上传证据材料、生成证据链。
    #
    # 分工：**所有法律逻辑留在本服务**，微信机器人（WeChat-AI，Node 侧）只做传输 ——
    # 它把「谁发的 + 说了什么 + 带了什么附件」转成本服务的 ``POST /api/v1/wechat/events``，
    # 再把本服务回的几句话原样发给对方。这样"证据链只对诉讼案件开放""案件归属隔离"
    # 这些规则**一处实现、一处生效**，不会在机器人侧长出第二套判据。
    #
    # 认证：机器人不是某个律师，它是服务端之间的调用方，所以走共享密钥
    # （``X-Bridge-Token``）而不是 JWT；**真正以谁的名义办事由绑定关系决定**
    # （peer_id → la_user.id，见 wechat_service）。
    WECHAT_ENABLED: bool = False
    # 桥接共享密钥。**留空 = 接口关闭**（宁可 503 也不要一个空密钥就全放行的接口）。
    # 生成：python -c "import secrets;print(secrets.token_urlsafe(32))"
    WECHAT_BRIDGE_TOKEN: str = ""
    # 绑定码有效期（分钟）。绑定是"律师在 Web 端取码 → 在微信里发 /绑定 <码>"
    # 的一次性动作，窗口给足 15 分钟即可；太长等于给了猜测的时间。
    WECHAT_BIND_CODE_TTL_MINUTES: int = 15
    # 微信单条消息能带的附件上限。微信 CDN 单文件本就有限制（本项目默认 12MB），
    # 这里再设一道：桥接是 base64 走 JSON，膨胀 4/3，太大会把两端内存一起顶穿。
    WECHAT_MAX_ATTACHMENT_MB: int = 20
    # 一次事件最多处理几个附件（连发多张图很常见，但不能无限）。
    WECHAT_MAX_ATTACHMENTS: int = 10

    # ---------------------------------------------------------------- 校验
    @field_validator("CORS_ORIGINS")
    @classmethod
    def _strip_origins(cls, v: str) -> str:
        return v.strip()

    @property
    def wechat_ready(self) -> bool:
        """微信接入是否真的可用。**三项都要满足**，少一项都是"配了不生效"。"""
        return bool(
            self.WECHAT_ENABLED
            and (self.WECHAT_BRIDGE_TOKEN or "").strip()
        )

    # ---------------------------------------------------------------- 工具方法
    @property
    def cors_origin_list(self) -> list[str]:
        raw = (self.CORS_ORIGINS or "").strip()
        if not raw or raw == "*":
            return ["*"]
        return [x.strip() for x in raw.split(",") if x.strip()]

    @property
    def milvus_enabled(self) -> bool:
        return self.MILVUS_ENABLED

    def ensure_dirs(self) -> None:
        for p in (
            self.UPLOAD_DIR,
            self.EXPORT_DIR,
            self.PROCESSED_DIR,
            self.KNOWLEDGE_DIR,
            self.BM25_INDEX_DIR,
            self.AUDIT_LOG_DIR,
        ):
            Path(p).mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- 采集工具方法
    @property
    def crawl_delay_range(self) -> tuple[float, float]:
        """站点接口的随机延迟区间。上下限写反时**自动摆正**而不是抛错。"""
        lo, hi = float(self.CRAWL_DELAY_MIN), float(self.CRAWL_DELAY_MAX)
        if lo > hi:
            lo, hi = hi, lo
        return max(lo, 0.0), max(hi, 0.0)

    @property
    def crawl_download_delay_range(self) -> tuple[float, float]:
        """文件下载的随机延迟区间，同样自动摆正。"""
        lo, hi = float(self.CRAWL_DOWNLOAD_DELAY_MIN), float(self.CRAWL_DOWNLOAD_DELAY_MAX)
        if lo > hi:
            lo, hi = hi, lo
        return max(lo, 0.0), max(hi, 0.0)

    def _anchor(self, value: str) -> Path:
        """把配置里的路径锚定到**项目根**。

        相对路径不能按 cwd 解析。从 ``scripts/`` 目录运行脚本是很常见的
        （IDE 的运行按钮默认就用脚本所在目录当工作目录），此时 ``./data/raw/x``
        会落到 ``scripts/data/raw/x``；而 ``corpus.ingest`` 扫的是项目根的
        ``data/raw``（它按 ``__file__`` 推导）。两者错开的结果是
        **"下载成功、入库 0 条"，全程没有任何报错** —— 极难排查。
        """
        p = Path(value or "").expanduser()
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def crawl_flk_dir(self) -> Path:
        """flk 采集落盘目录（已锚定）。"""
        return self._anchor(self.CRAWL_FLK_DIR)

    @property
    def crawl_state_dir(self) -> Path:
        """采集状态目录（登录态等，已锚定）。"""
        return self._anchor(self.CRAWL_STATE_DIR)

    @property
    def crawl_artifact_dir(self) -> Path:
        """采集工件目录（验证码截图等，已锚定）。"""
        return self._anchor(self.CRAWL_ARTIFACT_DIR)

    @property
    def crawl_captcha_keyword_list(self) -> list[str]:
        return [k.strip() for k in (self.CRAWL_CAPTCHA_KEYWORDS or "").split(",") if k.strip()]

    @property
    def crawl_proxy_list(self) -> list[str]:
        return [p.strip() for p in (self.CRAWL_PROXY_POOL or "").split(",") if p.strip()]

    @staticmethod
    def _split_csv(raw: str) -> list[str]:
        """逗号或换行分隔 → 去空白、去空项。

        同时兼容换行，是因为 .env 里把长名单折成多行更易读。**不用顿号/空格分隔**：
        法规名本身含顿号（"全国人民代表大会常务委员会"不含，但
        "最高人民法院、最高人民检察院…"含顿号），按顿号切会把一条名字劈成两条。
        """
        return [x.strip() for x in (raw or "").replace("\n", ",").split(",") if x.strip()]

    @property
    def crawl_flk_name_list(self) -> list[str]:
        return self._split_csv(self.CRAWL_FLK_NAMES)

    @property
    def crawl_flk_keyword_list(self) -> list[str]:
        return self._split_csv(self.CRAWL_FLK_KEYWORDS)

    @property
    def crawl_flk_category_list(self) -> list[str]:
        return self._split_csv(self.CRAWL_FLK_CATEGORIES)

    @property
    def crawl_flk_auto_category_list(self) -> list[str]:
        return self._split_csv(self.CRAWL_FLK_AUTO_CATEGORIES)

    @property
    def crawl_wenshu_keyword_list(self) -> list[str]:
        return self._split_csv(self.CRAWL_WENSHU_KEYWORDS)

    @property
    def crawl_wenshu_court_list(self) -> list[str]:
        return self._split_csv(self.CRAWL_WENSHU_COURTS)

    @property
    def crawl_time_window(self) -> tuple[int, int] | None:
        """把 ``CRAWL_TIME_WINDOW`` 解析成 ``(start_hour, end_hour)``；留空/非法返回 None。

        接受 ``22-6`` / ``22:00-06:00`` 两种写法。**非法值返回 None 而不是抛错**：
        一个写错的时间窗不该让整轮采集直接崩，退回"不限时间"更安全
        （代价是可能白天跑，但不会一篇都采不到）。
        """
        raw = (self.CRAWL_TIME_WINDOW or "").strip().replace("：", ":")
        if not raw:
            return None
        parts = raw.split("-")
        if len(parts) != 2:
            return None
        try:
            start = int(parts[0].split(":")[0].strip())
            end = int(parts[1].split(":")[0].strip())
        except (TypeError, ValueError):
            return None
        return (start % 24, end % 24)

    # 每组：(组名, 开关字段, 生效值字段, 降级说明)
    def runtime_backends(self) -> dict[str, dict[str, Any]]:
        """返回各基础设施「实际生效」的后端，用于启动自检与 /health 接口。"""
        return {
            "llm": {
                "enabled": True,
                "target": f"{self.LLM_PROVIDER} -> {self.LLM_BASE_URL} ({self.LLM_MODEL})",
                "fallback": "离线抽取式兜底（无外部模型时，仅基于检索证据拼装，不出引用）",
            },
            "vlm": {
                "enabled": self.VLM_ENABLED,
                "target": f"{self.VLM_BASE_URL} ({self.VLM_MODEL})",
                "fallback": "跳过多模态识别，保留占位符与页内文字",
            },
            "embedding": {
                "enabled": True,
                "target": f"{self.EMBEDDING_PROVIDER} -> {self.EMBEDDING_MODEL}",
                "fallback": "确定性 hashing 向量（仅保证链路连通，语义质量有限）",
            },
            "vector_store": {
                "enabled": self.MILVUS_ENABLED,
                "target": f"Milvus {self.MILVUS_URI}",
                "fallback": "本地 numpy 向量索引" if self.VECTOR_FALLBACK_TO_LOCAL else "不可用",
            },
            "keyword_index": {
                "enabled": self.BM25_ENABLED,
                "target": f"BM25/{self.BM25_ENGINE} @ {self.BM25_INDEX_DIR}",
                "fallback": "不可用",
            },
            "relational_db": {
                "enabled": self.DB_ENABLED,
                "target": self.DB_URL,
                "fallback": self.SQLITE_FALLBACK_URL,
            },
            "short_term_memory": {
                "enabled": self.REDIS_ENABLED,
                "target": self.REDIS_URL,
                "fallback": "进程内 TTL 字典（重启即失效）",
            },
            # **队列与 broker 必须分开报。** 队列由数据库支撑（``la_task`` 表），
            # 只要关系库在就可用；RabbitMQ 只是把「worker 多久发现新任务」
            # 从轮询间隔缩短到瞬时。合成一项会让人以为"没装 MQ 就没有队列"，
            # 而实际上任务照样排队、照样执行，只是慢几秒。
            "task_queue": {
                "enabled": self.DB_ENABLED,
                "target": "数据库队列表 la_task",
                "fallback": "不排队，请求内联执行（关系库不可用时的唯一出路）",
            },
            "task_broker": {
                "enabled": self.RABBITMQ_ENABLED,
                "target": self.RABBITMQ_URL,
                "fallback": (f"worker 每 {self.WORKER_POLL_SECONDS:.1f} 秒轮询数据库"
                             f"（只增加延迟，不影响正确性，也不丢任务）"),
            },
            # 采集**不是**基础设施：关掉它不影响任何既有链路（法条/案例检索走的是
            # 本地库与向量索引，不经过浏览器）。把它报成"降级"会误导运维去排查一个
            # 从未启用的能力，所以这里如实写"未启用"，并在 target 里点明依赖。
            "crawler": {
                "enabled": self.CRAWL_ENABLED,
                "target": (f"Playwright {'无头' if self.CRAWL_HEADLESS else '有头'}，"
                           f"单日配额 {self.CRAWL_DAILY_QUOTA or '不限'} 条/源"),
                "fallback": "采集能力不可用（用法条/案例检索等本地能力，不经浏览器）",
            },
            # 微信机器人接入**不是**基础设施：关掉它不影响任何既有链路。
            # 但它是"只能从微信进来"的那条路，所以启动时要能一眼看出开没开、
            # 以及是不是"开了但缺密钥"（那种状态下接口会 503，界面却看着一切正常）。
            "wechat_bot": {
                "enabled": self.WECHAT_ENABLED,
                "target": ("POST /api/v1/wechat/events（X-Bridge-Token 桥接）"
                           if self.wechat_ready
                           else "已开启但缺 WECHAT_BRIDGE_TOKEN —— 接口会返回 503"),
                "fallback": "微信入口不可用（Web 工作台不受影响）",
            },
        }

    @staticmethod
    def unmapped_env_keys() -> dict[str, list[str]]:
        """.env 键 与 配置字段 对拍，返回未映射的键（静默失效的元凶）。"""
        env_file = PROJECT_ROOT / ".env"
        if not env_file.exists():
            return {"file": str(env_file), "unmapped": [], "missing_file": True}
        declared = {name.upper() for name in Settings.model_fields}
        unmapped: list[str] = []
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key = line.split("=", 1)[0].strip().upper()
            if key not in declared:
                unmapped.append(key)
        return {"file": str(env_file), "unmapped": unmapped, "missing_file": False}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


settings = get_settings()


def describe_settings() -> str:
    lines = [f"{settings.APP_NAME} v{settings.APP_VERSION} ({settings.ENV})", "运行后端："]
    for name, info in settings.runtime_backends().items():
        flag = "ON " if info["enabled"] else "OFF"
        lines.append(f"  [{flag}] {name:18} {info['target']}")
    check = settings.unmapped_env_keys()
    if check.get("missing_file"):
        lines.append(f"  [WARN] 未找到 .env：{check['file']}（当前使用代码默认值）")
    elif check["unmapped"]:
        lines.append(f"  [WARN] .env 中存在未映射的配置键：{', '.join(check['unmapped'])}")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - 手工自检入口
    os.environ.setdefault("PYTHONUTF8", "1")
    print(describe_settings())
