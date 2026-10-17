# 律师专用多 Agent 法律咨询助手

一个跑通了的法律智能体系统：**同一套 Agent 编排**同时服务 Web 端（律师工作台 + 管理后台）
  
与桌面端（EXE 瘦客户端）。核心链路是「案件/合同材料 → 解析 → 混合检索（向量 + BM25）
  
→ 多节点编排 → 引用终审 → 报告/文书」，并且**每一条降级都显式声明**，不做静默回落。

> **本 README 的写法**：凡是标「已实测」的都在本机跑过并给出命令与预期输出；
>   
> 标「未验证」的会集中列在第 9 节，不混在正文里。
>   
> 法律场景下"看起来正常的错误结果"比报错更危险，所以这里对没跑过的东西一律写明。

---

## 1. 当前状态

| 项      | 状态                                               |
| ------ | ------------------------------------------------ |
| 后端接口   | 91 个端点，全部可用（见第 6 节）                              |
| 自动化测试  | **268 passed**（`pytest -q`，约 60 秒）               |
| Web 前端 | 13 个页面全部渲染成功，未捕获异常 0 条（`scripts/web_smoke.mjs`）  |
| 任务队列   | 入队 → worker 认领 → 执行落库，端到端跑通（`probe_e2e.py`）      |
| 法规检索   | 「未签订书面劳动合同二倍工资」→ 6 条条文精准命中                       |
| 运行依赖   | MySQL ✅ / Milvus ✅ / 本地 BGE Embedding ✅ / BM25 ✅ |
| 未接线    | Redis、RabbitMQ、VLM、远程 LLM —— 见第 8 节的降级表          |

本机实测的运行后端：

```
[ON ] llm            auto -> rule (qwen3-8b-legal-lora 不可达，回落确定性规则引擎)
[ON ] embedding      local  BAAI/bge-small-zh-v1.5 (dim=512)
[ON ] vector_store   Milvus http://127.0.0.1:19530
[ON ] keyword_index  BM25/builtin @ data/index/bm25
[ON ] relational_db  mysql+pymysql://root:***@127.0.0.1:3306/legal_agent
[OFF] short_term_memory  redis://127.0.0.1:6379/0
[OFF] task_queue         amqp://guest:guest@127.0.0.1:5672/
[OFF] vlm                qwen2.5-vl-7b
```

`llm` 那行是**实测结论**：本机没有起 SGLang，所以 LLM 逐级回落
  
（openai → ollama → rule），最终由确定性规则引擎产出分析与报告。
  
报告仍然带引用、带缺口声明，只是分析维度比 LLM 窄 —— 这一点会写进 `evidence_gap`，
  
在界面和接口里都看得到。

---

## 2. 快速开始

前置：Python 3.13、MySQL 8（本机 `127.0.0.1:3306`，root/123456）、
  
Milvus standalone（`127.0.0.1:19530`，可选，不可用会回落本地向量索引）。

```bash
cd legal-agent

# 1) 虚拟环境（--system-site-packages 是为了复用已装好的 torch/sentence-transformers）
python -m venv .venv --system-site-packages
./.venv/Scripts/python.exe -m pip install -r requirements.txt     # Windows
# source .venv/bin/activate && pip install -r requirements.txt    # Linux/macOS

# 2) 配置：复制模板后按需改
cp .env.example .env        # 开箱可用；默认口令 admin/admin123、lawyer/lawyer123

# 3) 起后端（会自动建表 + 修复指向 legacy 备份表的外键）
./.venv/Scripts/python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8001
#    启动自检约 0.2 秒，之后**预热检索链路约 17 秒**（载入 BGE 权重占 16.8 秒），
#    日志出现「检索链路预热完成」才说明可以接客了。不想等就设 WARMUP_ENABLED=false，
#    代价是这 17 秒会落到第一个用户头上。

# 4)（可选）起任务 worker —— 只有要用「排队执行」时才需要
./.venv/Scripts/python.exe -m backend.worker.app

# 5) 打开前端
#    http://127.0.0.1:8001/ui/
```

前端是**零构建**的（原生 ES Module + 手写 CSS 变量主题），没有 npm install、没有打包步骤，
  
改完刷新即可。`/ui` 由同一进程托管。

### 账号

| 账号       | 口令          | 角色  | 用途                           |
| -------- | ----------- | --- | ---------------------------- |
| `admin`  | `admin123`  | 管理员 | 管理后台（团队/用户/配置/审计/统计/知识库/法规库） |
| `lawyer` | `lawyer123` | 律师  | 演示办案流程                       |

首个管理员由 `.env` 的 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 在启动自检时种下。
  
**上生产前必须改掉**（还有 `SECRET_KEY`：当前 31 字节，低于 RFC 7518 对 HMAC-SHA256 的
  
32 字节建议下限，启动时会打 warning）。

---

## 3. 一次任务是怎么跑的

```
                 ┌──────────────────────────────────────────────┐
  Web (/ui) ────►│ POST /chat/stream            SSE 逐节点进度   │
  EXE 客户端 ───►│ POST /chat?enqueue=true      入队，立刻返回    │
                 └───────────────┬──────────────────────────────┘
                                 │ 同一个 orchestrator.execute(RunRequest)
                                 ▼
        ┌────────────────────────────────────────────────────────┐
        │ Harness（运行时）：Trace / 配额 / 降级 / 证据缺口        │
        │ Planner → Router → Nodes → Validator → Report          │
        └───────────────┬────────────────────────────────────────┘
                        │
        ┌───────────────┴───────────────┐
        ▼                               ▼
  la_task（任务快照+Trace）      la_citation / la_draft（沉淀到独立表）
```

**两条执行路径，都是同一个 `orchestrator.execute`：**

|          | 同步 / SSE               | 排队（`enqueue=true`）   |
| -------- | ---------------------- | -------------------- |
| 谁执行      | API 进程                 | worker 进程            |
| 适用       | 问答、检索等短任务              | 合同审查这类几分钟的长任务        |
| 连接断了     | **SSE 断开不取消任务**，后台跑完落库 | 与连接无关                |
| API 进程重启 | 在跑的任务消失（停在 `running`）  | 任务在库里，worker 重启后继续   |
| 客户端      | Web 前端（默认）             | EXE 客户端 / 批处理 / cron |

**认领是一次带条件的 UPDATE**（`UPDATE ... WHERE status='pending'` 的 `rowcount`
  
是唯一裁决依据）。多 worker 各自 `peek` 到同一条是常态，"先查后改"会让同一任务被跑两遍，
  
而表现只是"慢"。

**数据库是唯一真源，broker 只是快通道。** RabbitMQ 启用时它只负责让 worker 立刻醒来；
  
消息丢了、worker 重启期间消息过期了，轮询都会在后一个间隔把任务捞回来。

---

## 4. 目录结构

```
legal-agent/
├── backend/
│   ├── app/                    FastAPI 应用层
│   │   ├── api/v1/             91 个端点（auth/cases/chat/docs/tasks/admin/...）
│   │   ├── services/           业务服务：user / case / knowledge / citation /
│   │   │                       draft / audit / stats / config / task /
│   │   │                       task_queue / orchestrator
│   │   ├── db/                 模型 + 引擎（MySQL，SQLite 自动回落）+ 结构对账
│   │   ├── config.py           全部配置项（pydantic-settings，从 .env 读）
│   │   └── main.py             应用装配 + 启动自检
│   ├── agents/                 多 Agent 编排
│   │   ├── graph.py            LangGraph 编排（含 Validator 回环）
│   │   ├── state.py            LegalState（唯一状态定义）
│   │   ├── harness/            运行时：Trace / Planner / Router / 配额 /
│   │   │                       Validator / 错误处理 / 审计
│   │   └── nodes/              11 个节点：Intent / Intake / Statute / Case /
│   │                           Knowledge / Analysis / Review / Draft /
│   │                           Validator / Report / Memory
│   ├── rag/                    混合检索：向量 + BM25 + 领域精排 + 材料检索
│   ├── parser/                 文档解析：PDF / DOCX / 图片 OCR
│   ├── tools/                  工具注册表（配额、审计、降级声明）
│   ├── skills/                 Skill 封装 + MCP server
│   ├── memory/                 分层记忆 + 脱敏 + 语义/档案提取
│   ├── llm/                    可插拔 LLM 客户端（remote / ollama / rule）
│   ├── worker/                 任务 worker 进程（队列消费者）
│   ├── corpus/                 法规语料与版本管理
│   ├── evals/                  离线评测
│   └── finetune/               LoRA 微调数据工程
├── web/                        前端（零构建，原生 ESM）
│   ├── index.html, css/app.css
│   └── js/  api/store/router/sse/ui/md/icons + views/ 11 个视图
├── client/                     EXE 瘦客户端（PyInstaller）
│   ├── app/                    theme(配色) / icons(矢量图标) / widgets(公用控件) + 客户端与状态
│   └── panels/                 home(工作台) + chat / review / tasks / drafts
├── deploy/                     Docker / Nginx 部署
├── scripts/
│   ├── smoke_services.py       服务层冒烟（真实库，验证能写能读）
│   └── web_smoke.mjs           前端冒烟（jsdom 真跑每个页面）
├── tests/                      268 条回归测试
└── data/                       运行时数据
    ├── raw/ processed/         语料原文与解析产物
    ├── index/bm25/             BM25 索引
    ├── models/                 本地 embedding 模型
    ├── uploads/ exports/       案件材料与导出文书
    ├── knowledge/              知识库附件
    ├── audit/                  审计 JSONL 落盘（文件真源）
    └── logs/
```

---

## 5. 测试与验证

```bash
# 全套回归（268 条）
./.venv/Scripts/python.exe -m pytest -q

# 端到端全链路：**用一份真实的劳动法 PDF** 跑通解析 → 检索 → 编排 → 校验 → 文书
./.venv/Scripts/python.exe scripts/e2e_chain.py

# 桌面端界面冒烟（离屏）：需要后端在跑、且装了 PySide6
./.venv/Scripts/python.exe scripts/qt_smoke.py

# 服务层冒烟：真实数据库，验证每个服务能写能读
./.venv/Scripts/python.exe scripts/smoke_services.py

# 前端冒烟：需要后端已在 8001 运行；13 个页面逐页断言 + 统计未捕获异常
node scripts/web_smoke.mjs

# 任务队列端到端：入队 → 起 worker → 回查（需要后端在跑）
./.venv/Scripts/python.exe probe_e2e.py
```

`scripts/web_smoke.mjs` 是这一轮**最有价值的一条防线**：静态检查查不出
  
`tabBar` 回调里的 TDZ、查不出列表接口根本没返回 `content`、也查不出字段名对不上 ——
  
这三类都是本项目实际踩过而且**不报错**的坑，只有真的 import + 真的渲染 + 真的发请求才会暴露。
  
它不校验样式（jsdom 不做布局）。**要验视觉就用真实浏览器**：

```bash
# 登录页截图（含暗色主题与窄屏）
agent-browser open http://127.0.0.1:8001/ui/
agent-browser screenshot docs/login-preview-web.png
agent-browser close

# 容器部署那条路要单独验：静态资源由 nginx 用 alias 直接 serve，绕过 FastAPI，
# 后端设的缓存头在这条路上根本不生效
agent-browser open http://127.0.0.2:8080/ui/
agent-browser screenshot docs/login-preview-deployed.png
agent-browser close
```

**逐页验收**（本轮补上的那一层）。登录之后走 hash 路由，SPA 不会整页刷新，
所以错误收集器装一次就能覆盖后面所有页面：

```bash
# 装收集器：错误与未处理的 Promise 拒绝都记下来
agent-browser eval "window.__errs=[];addEventListener('error',e=>__errs.push(e.message));
  addEventListener('unhandledrejection',e=>__errs.push(String(e.reason)));"

# 每页：导航 → 等渲染 → 量布局 → 截图。断言 overflow 为 0 和 errs 为空
agent-browser eval "(async()=>{location.hash='#/tasks';await new Promise(r=>setTimeout(r,2400));
  const de=document.documentElement;
  return JSON.stringify({overflow:de.scrollHeight-de.clientHeight,errs:__errs.length});})()"
agent-browser screenshot docs/page-tasks.png
```

实测 10 条路由 `overflow` 全为 0、未捕获异常全为 0。**注意 `screenshot` 不带
`--full`**：带 `--full` 走的是 CDP 的 `captureBeyondViewport`，会临时改动视口
触发重排，固定定位元素可能错位；本项目的页面本来就正好一屏，直接截即可。

这一步不是可选项。改登录页时就靠它抓到三个 jsdom 无论如何都发现不了的问题：
`[hidden]` 被 `display:grid` 盖掉导致登录页下方多出一屏空白可滚动区；本机这条路
的静态资源缺 `Cache-Control`；以及容器这条路 **nginx 的 `expires 1h` 让上面那个
修复形同虚设**（前端文件名不带 hash，长缓存会让新版 HTML 配上旧 CSS）。
三者都配了守卫（`tests/test_ui_static.py`）。

改侧栏图标时又是靠它抓到的两个：**字符图标**（`⌂ ⚖ § ⌕ ❐ ◷ ✎ ◈ ⚙` 里既有
emoji 码段的、也有字体覆盖率极低的，换台机器就可能变方块，已全部换成内联 SVG，
见 `js/icons.js`）；以及**侧栏最后一项被挤出可视区**——「管理后台」底部算出来
501px、而底部区块顶在 435px，整项落在滚动区里且没有任何提示，用户只会以为
没有这个入口。后者**截图看不出来**（截图上那一项就是"不存在"的样子），
必须去量 `scrollHeight vs clientHeight` 才能发现。

`scripts/e2e_chain.py` 补的是另外两个冒烟**都不管**的那一层：它们只关心
  
"服务活着"与"代码逻辑对"，不关心"检索出来的法条和报告里引用的法条是不是同一批"。
  
所以前两个全绿、这个红是完全可能的 —— 它的五步里最有价值的正是
  
**「命中内容与问题相关」** 和 **「引用校验通过」** 两项。
  
跑完会给出前端直达链接，因为链路跑通的判据不是"脚本没报错"，而是"律师能在界面上看到它"。

`scripts/qt_smoke.py` 验的是桌面端。它存在的理由是**打包成功 ≠ EXE 能启动**：
  
缺 Qt 插件、缺 DLL、入口导入错误，都只在真的构造出 QWidget 那一刻暴露，
  
而 PyInstaller 那边一律显示 build completed successfully。脚本走离屏 QPA，
  
真实登录后依次验：矢量图标全部可渲染（名字写错必须抛错而不是静默画空白）、侧栏三组九项与 Web 端逐项一致、
工作台首页的卡片与意图下拉同源、点卡片只把模板填进输入框而**不直接发送**、侧栏最近任务回填、
各功能面板拿到真实数据、切页，最后验证退出时凭证与内存案件一起清空。

实测输出（节选）：

```
OK   工作台       #/          (view 755 字)
OK   智能问答     #/chat      (view 201 字)
OK   法规检索     #/search    (view 165 字)
     检索「未签订书面劳动合同二倍工资」→ 6 条命中
OK   任务详情     #/tasks/task_0454722c7ef3  (view 1708 字)
OK   文书草稿     #/drafts    (view 291 字)
     打开草稿 draft_27ba0be2324c，正文字数 77
OK   能力与降级   #/capabilities (view 8709 字)  tab 5 个全通
OK   管理后台     #/admin     (view 347 字)  tab 7 个全通（审计 tab 64093 字）
================ 未捕获异常 ================
（无）
```

---

## 6. 接口地图

完整清单见 `http://127.0.0.1:8001/docs`。分组摘要：

| 分组  | 关键端点                                                                                     | 说明                                |
| --- | ---------------------------------------------------------------------------------------- | --------------------------------- |
| 认证  | `POST /auth/login`、`GET /auth/me`、`GET /auth/permissions`                                | JWT；`/auth/me` 与登录响应都带 `is_admin` |
| 案件  | `GET,POST /cases`、`/cases/{id}/materials`、`/cases/{id}/progress`                         | `material_ids` 而非路径传材料            |
| 对话  | `POST /chat`、`POST /chat/stream`                                                         | `/chat?enqueue=true` 走队列          |
| 合同  | `POST /contract/review[/stream]`                                                         | **必须有材料**，否则拒绝执行                  |
| 文书  | `POST /doc/draft`、`GET /doc/types`                                                       | `doc_type` 决定文书类型                 |
| 检索  | `POST /search/{statute,case,knowledge,material}`                                         | 只检索不出报告，走工具通道                     |
| 任务  | `GET /tasks`、`/tasks/{id}`、`/tasks/{id}/stream`、`/tasks/{id}/citations`、`/gaps`、`/trace` | `/stream` 是 WebSocket 进度          |
| 草稿  | `GET,POST /drafts`、`POST /drafts/{id}/export`、`GET /drafts/{id}/download`                | 导出走 `/download`，不由客户端读服务器文件       |
| 知识库 | `/knowledge/docs`、`POST /knowledge/reingest`                                             |                                   |
| 法规库 | `/statutes/lib`、`PUT /statutes/lib/{doc}/status`                                         | **改效力状态是最危险的操作**                  |
| 管理  | `/admin/{teams,users,config,audit,stats}`                                                | 全组需要管理员                           |
| 元信息 | `/health`、`/health/ready`、`/meta/*`                                                      | `/health` 不需要登录                   |
| 升级包 | `GET,POST /update-packs`、`POST /update-packs/{id}/rollback`                              |                                   |

### 几条接口纪律

| 纪律                               | 原因                                                           |
| -------------------------------- | ------------------------------------------------------------ |
| 列表接口**不带正文**（只给 `content_chars`） | 详情页编辑前必须回查一次；直接拿列表行填 textarea 再保存 = **静默清空正文**（保存只校验标题非空）    |
| 响应体**不给磁盘路径**                    | `stored_path` / `exported_path` 会把部署目录结构带出去；要文件走 `/download` |
| `task_id` 由服务端生成并沿用              | 排队任务必须复用入队时那一行，否则认领的行永远停在 `running`                          |
| 降级一律显式声明                         | `degraded` + `fallback_note` 必须透出，静默降级在法律场景不可接受              |

---

## 7. 关键配置项（`.env`）

完整项见 `.env.example`（84 项；`.env` 里实际生效 43 项，其余用代码默认值）。
  
最需要知道的：

| 键                                             | 默认                                                                       | 说明                                        |
| --------------------------------------------- | ------------------------------------------------------------------------ | ----------------------------------------- |
| `DB_URL`                                      | `mysql+pymysql://root:123456@127.0.0.1:3306/legal_agent?charset=utf8mb4` | MySQL 不可用时自动回落 `SQLITE_FALLBACK_URL`      |
| `MILVUS_ENABLED` / `VECTOR_FALLBACK_TO_LOCAL` | `true`                                                                   | 关闭或连不上时回落本地向量索引                           |
| `EMBEDDING_PROVIDER`                          | `auto`                                                                   | `local` 优先走 `EMBEDDING_LOCAL_MODEL_PATH`，未设置时走 `data/models/bge-small-zh-v1.5` |
| `RERANKER_LOCAL_MODEL_PATH`                   | 空                                                                       | 本地 bge-reranker CrossEncoder 模型目录；为空时使用项目 `data/models` 中的候选目录 |
| `LLM_PROVIDER` / `LLM_BASE_URL`               | `auto` / `127.0.0.1:30000/v1`                                            | 不可达时逐级回落，最终用规则引擎                          |
| `REDIS_ENABLED`                               | `false`                                                                  | 关闭则短期记忆用进程内 TTL 字典（**不跨进程**）              |
| `RABBITMQ_ENABLED`                            | `false`                                                                  | 关闭则 worker 靠轮询数据库                         |
| `WORKER_POLL_SECONDS`                         | `5.0`                                                                    | 入队到开始跑的延迟上限                               |
| `WORKER_BATCH`                                | `4`                                                                      | 一次认领条数 = 单 worker 并发度                     |
| `WORKER_TASK_TIMEOUT`                         | `1800`                                                                   | 单任务硬超时，`0` 不限                             |
| `WORKER_STALE_SECONDS`                        | `0`（= 自动取超时 2 倍）                                                         | 判定"卡死在 running"，**不要小于任务超时**              |
| `MAX_STEPS`                                   | `16`                                                                     | 步数上限（方案原文 6~8 与"回环"要求冲突，落地必须上调）           |
| `DESENSITIZE_ENABLED` / `AUDIT_ENABLED`       | `true`                                                                   | 脱敏与审计                                     |

---

## 8. 降级矩阵（这是 `#/capabilities` 页面读的数据）

| 子系统         | 关闭 / 不可达时           | 代价                            |
| ----------- | ------------------- | ----------------------------- |
| Milvus      | 回落本地向量索引            | 向量召回变慢，条数可能变少                 |
| LLM（SGLang） | 逐级回落 → 确定性规则引擎      | 分析维度变窄，**会写进 `evidence_gap`** |
| VLM         | 图片材料不做版面理解，退回 OCR   | 复杂表格/手写体识别质量下降                |
| Redis       | 进程内 TTL 字典          | 短期记忆**不跨进程**，多副本各记各的          |
| RabbitMQ    | worker 轮询数据库（5 秒间隔） | 入队到开始跑最多多等一个间隔                |
| MySQL       | SQLite 文件库          | 并发写变弱；跨方言语法已单独处理（见下）          |

**降级不是故障，但必须说出来。** `/health` 把 `problems`（真故障，关键路径不可用）
  
与 `degraded`（降级但可用）分开报 —— 混在一起会让探针要么长期红、要么掩盖真故障。

---

## 9. 未完成 / 未验证清单

诚实列出来，避免把"没跑过"当成"跑通了"。

| 项                                                        | 状态                                                                                                                                                                                                                                                                               |
| -------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `deploy/`（docker-compose、Dockerfile、nginx.conf、init.sql） | 已完成并实测。`docker compose config` / `nginx -t` 通过；**镜像构建成功（2.48 GB）并真跑起来**：容器 `Up (healthy)`，`/ui/` 返回前端 HTML、登录可拿 token、worker 入口在降级到 SQLite 后正常常驻。过程中修掉一个只在容器里才炸的问题：`review_node.py` 的 f-string 表达式带反斜杠，Python 3.11 直接 SyntaxError（本机 3.13 正常），已加 `tests/test_py311_compat.py` 拦住。**整套编排也已实际拉起**：mysql / redis / api / worker / web 五个服务全 `Up (healthy)`，经 nginx 走通登录 → `/health` → `chat`（5.0s），`/meta/config` 显示 `backend: mysql`（证明连的是 MySQL 容器，没有静默回落 SQLite）。这轮又在真运行里挖出并修掉三个坑：worker 继承了镜像里探 8001 的 HEALTHCHECK 导致永远假 unhealthy；`RABBITMQ_ENABLED=true` 但依赖 `aio-pika` 没进镜像，每次启动都假报警；以及**重启 api 后 nginx 全站 502**（upstream 把服务名解析成旧 IP 后永久缓存，已改为变量 + `resolver`） |
| `backend/finetune/llama_factory/`                        | 已完成（数据集配置 + LoRA 训练 YAML + A100 脚本 + `REVIEW_CHECKLIST.md`）                                                                                                                                                                                                                      |
| `client/`（EXE 瘦客户端）                                      | 已完成并实测。**HTTP 层**：登录 / 案件列表 / 流式（4.3s、61 事件）/ 建案件+上传 / 草稿导出 36689 字节+下载 / 登出后 401。**界面**：侧栏 + 内容区，导航三组九项与 Web 端 `NAV` 表逐项对齐（tests 里有契约守卫），工作台首页卡片由 `GET /chat/intents` 真实驱动；新增法规检索 / 案件管理 / 能力与降级 / 管理后台（只读）四页；离屏冒烟 25 项通过。**EXE**：`dist/LegalAgent.exe` 51.2 MB，**已实际启动**（离屏跑满 12 秒未崩）                                                                                  |
| `backend/evals/` 离线评测                                    | 已完成。真实跑过 101 条任务：引用可采信率 100%，降级率 100%（降级项已如实列出）                                                                                                                                                                                                                                  |
| `scripts/import_corpus_pack.py`                          | 已完成。对真实目录 dry-run 通过（55 文件 → case 1 / statute 19 / template 35）                                                                                                                                                                                                                  |
| `docs/`                                                  | 四组真实渲染截图：**登录页**（`login-preview-web.png` / `-web-dark.png` / `-web-narrow.png` / `-desktop.png` / `-deployed.png`，Chromium 与 Qt 各截一遍）、**前端逐页**（`page-*.png`，10 条路由各一张）、**主题与图标**（`icons-dashboard-{light,dark}.png`）、**桌面端主界面**（`client-*.png`：工作台首页、点卡片后的输入框、四个功能页、侧栏搜索、1040×720 窄窗口）。全部由真实 Chromium / Qt 渲染后截取，不是设计稿                                                                                                                                                                        |
| RabbitMQ 消费路径                                            | `RABBITMQ_ENABLED=false`，**那一段代码本机没跑过**（未装 `aio-pika`）                                                                                                                                                                                                                           |
| Redis 路径                                                 | 同上，进程内字典那条路跑过，Redis 那条没跑过                                                                                                                                                                                                                                                        |
| 远程 LLM / SGLang                                          | 本机无服务，`llm` 一直是规则引擎在产出                                                                                                                                                                                                                                                           |
| VLM 图片材料                                                 | 未启用，未测                                                                                                                                                                                                                                                                           |
| Windows 信号处理                                             | `add_signal_handler` 在 Windows 上不支持，worker 优雅退出**未验证**（靠 Ctrl+C）                                                                                                                                                                                                                 |
| 前端视觉                                                     | 已用**真实 Chromium** 逐条验证：登录页三态（浅色/暗色/窄屏）+ 主界面 **10 条路由逐页截图**，每页断言 `scrollHeight === clientHeight`（无多余空白滚动区）与"未捕获异常 0 条"，截图见 `docs/`。还发现并修掉了一个只看截图之外的测量才暴露的问题：侧栏 9 个导航项在 1366×768 上会把「管理后台」挤进滚动区看不见。**唯一未覆盖的是交互后的瞬时状态**（弹窗、流式输出中途）——那些仍靠 `scripts/web_smoke.mjs` 的 jsdom 断言                                                                                                                                                                              |
| 桌面端视觉                                                   | 用**真实 Windows 平台插件**渲染截取（不用 `QT_QPA_PLATFORM=offscreen`：那个平台下 `QFontDatabase` 一个字体族都没有，中文会全渲染成方块，截图看着像程序坏了）。改用 `WA_DontShowOnScreen` —— 窗口不弹到屏幕上，但布局与字体都是真的。截图见 `docs/client-*.png`（工作台首页、点卡片后的输入框、四个功能页、侧栏搜索过滤、1040×720 窄窗口）。**未覆盖**：高 DPI（150% / 200% 缩放）下的实际观感，以及流式输出进行中的界面状态                                                                                                       |
| 本机 `.venv`                                               | **`--system-site-packages`** 建的，`httpx`/`fastapi`/`pymilvus` 等**全部来自系统 Python**。本机跑得通，但依赖隔离是假的；`requirements.txt`（21 个直接依赖）未被污染，Docker 走独立安装，部署不受影响                                                                                                                              |

### 已知问题

~~**`source_path` 外泄**~~ —— **已修（2026-09-18）**，并留有回归守卫
  
`tests/test_no_path_leak.py`（5 条）。

原先 `result.statutes[].source_path` / `result.cases[].source_path` /
  
`result.citations[].source_path` 会返回形如
  
`C:\...\data\raw\劳动法\劳动合同法.pdf` 的服务端绝对路径，
  
`rule_engine` 甚至把它拼进了**生成的文书正文**（律师会直接复制到给客户的版本里）。

修法是"**只在序列化边界上剥**"，和 `state` 剥队列元数据同一套做法：

| 面        | 修在哪                                                                       |
| -------- | ------------------------------------------------------------------------- |
| 引用条目 API | `citation_service.citation_to_dict` 不再输出该键                                |
| 任务结果 API | `task_service._to_dict` 走 `_strip_source_paths` **递归**剥（不按已知路径删，否则结构一变就漏） |
| 生成文书正文   | `rule_engine._h_report` 的引用清单去掉 `｜来源：...`                                 |

落库快照仍保留完整路径供追溯 —— 泄露问题出在"出接口"，不是"存了它"。

验收方式是**打真实请求后遍历响应体找路径**，不是看代码：
  
`/api/v1/chat` 与 `/api/v1/tasks/{id}` 均已确认零命中。

---

## 10. 排错清单

按这个顺序往下走，能省掉大部分弯路（都是本项目实际踩过的）：

| 症状                              | 先查什么                                                                                                                                                                                                        |
| ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 改了 `.env` 但没生效                  | `.env` 到底有没有被加载？生效值打出来看一眼，别假设                                                                                                                                                                               |
| 某个功能"没生效"                       | 有没有降级分支把错误吞了？本项目大量 `try/except`，"没生效"常常是"被降级了但没声张"                                                                                                                                                          |
| 日志里看不到启动状态                      | Python 默认 `lastResort` **只输出 WARNING 以上**，`logger.info()` 等于白写；本项目的 `logging_conf.setup_logging()` 已挂 handler                                                                                               |
| 测试结论与预期不符                       | **是不是打在旧进程上**？端口没释放时新服务会静默退出，后续全打在旧进程上                                                                                                                                                                      |
| 操作成功但没反应                        | 确认它真的被执行过 —— 有些模块从未接线，里面的 bug 一直存在但从没暴露                                                                                                                                                                     |
| 第一次检索特别慢（十几秒），或首次工具调用报「超时（30s）」 | 看启动日志有没有 **「检索链路预热完成」**。启动时会自动预热（实测 17.3 秒，其中 16.8 秒是 loading BGE）；没预热或预热失败时，这份加载会落在第一个请求上，而 `TOOL_TIMEOUT` 默认 30 秒会把**首次调用掐断**再重试。`GET /health` 的 `warmup` 字段就是干这个用的                                       |
| **容器起来又退出，日志里没有任何应用输出**         | 十有八九是 **Python 版本差异**：镜像是 3.11，本机是 3.13。最典型是 f-string 的表达式里带反斜杠（要 3.12 才合法），import 阶段就 SyntaxError，连 boot 日志都来不及打。排查：`docker run --rm -v "$PWD:/src" python:3.11-slim python -m compileall -q /src/backend` |
| 首次 `docker build` 极慢（几十分钟）      | 不是 torch 太大，是装**错了** torch：PyPI 上是 CUDA 轮子，连带 `nvidia-*` 合计 3GB+。默认已改走 CPU 索引（196MB），详见 `deploy/README.md`「镜像构建」                                                                                            |
| 改了 CSS/JS 刷新页面没变化                | 服务端返回的可能已是新内容，但 **Starlette 的 `StaticFiles` 默认不发 `Cache-Control`**，浏览器走启发式缓存连回源都不做。查响应头：应为 `no-cache`（不是 `no-store`），为空说明 `main.py` 的 `_RevalidatingStatic` 没挂上                                                     |
| 页面下方多出一整屏空白滚动区                   | UA 样式表的 `[hidden]{display:none}` 优先级极低，任何显式 `display` 都能盖掉它 —— 本项目 `.login-screen` / `.app-shell` 都写了 `display: grid`，于是它们**带着 hidden 属性照样参与布局**。查 `app.css` 里那条带 `!important` 的 `[hidden]` 规则还在不在                          |
| 侧栏里某个入口"根本没有"                | 别用眼睛找，去量：`nav.scrollHeight > nav.clientHeight` 就说明有项落在滚动区里了。侧栏项高由**文字行高**决定而不是图标（默认行高把 9 项撑到 457px，1366×768 的可用高只有 375px），「管理后台」整项被裁且无任何滚动提示                                                                          |
| 队列"积压"但不动                       | `GET /health` 看 `subsystems.task_queue`：`queued` 长期不降 = worker 没起或没认领成功；`stale_running` 非空 = 进程被强杀过                                                                                                         |
| 任务一直转圈                          | 查 `la_task.status`：停在 `running` 且 `updated_at` 很旧 → 用 `worker --requeue-stale`（默认启动就会回收）                                                                                                                    |
| 管理后台进不去                         | 登录响应有没有 `is_admin`？缺了的话管理员也看不到入口，而 `role` 字段一切正常                                                                                                                                                            |
| 知识库文档编辑后正文没了                    | 列表接口不带 `content`，编辑前必须 `GET /knowledge/docs/{id}` 回查                                                                                                                                                        |

---

## 11. 上线前必做

1. 改 `SECRET_KEY`（当前 31 字节，低于 32 字节建议下限）：
     
   `python -c "import secrets;print(secrets.token_urlsafe(48))"`
2. 改 `ADMIN_PASSWORD` / `DEMO_LAWYER_PASSWORD`，或删掉演示账号。
3. 确认 `la_*_legacy_*` 备份表可以删（结构对账时自动留下的，删除前先比对数据）。
4. 决定是否启用 Redis（多副本部署时**必须**启用，否则短期记忆不共享）。
5. ~~修掉第 9 节的 `source_path` 外泄~~ **已完成**。
6. ~~补 `deploy/`~~ **已完成**（Dockerfile / compose / nginx / init.sql 齐全；
     
   Dockerfile 里注意：**应用数据用命名卷，不要用 bind mount** ——
     
   宿主机目录属主会覆盖镜像属主，导致写 SQLite 报 `readonly database`）。
