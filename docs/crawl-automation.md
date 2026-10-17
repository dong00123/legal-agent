# 法条 + 案例自动采集

把「法条」和「案例」的采集交给 **Windows 任务计划程序**，按天自动跑一轮：
断点续跑、单日配额、验证码转人工都复用既有能力，不需要常驻进程。

> 相关代码：`backend/crawler/runner.py`（执行器）、`backend/crawler/sites/flk.py`（法条源）、
> `backend/crawler/sites/wenshu.py`（案例源）、`scripts/run_crawl_once.bat`、
> `scripts/register_crawl_task.ps1`。

---

## 一、一条命令 = 一轮采集

```powershell
# 预演（不下载）：法条只检索列清单、案例只打印目标 URL
.venv\Scripts\python.exe -m backend.crawler.runner --once --dry-run

# 正式跑一轮（法条 + 案例）
.venv\Scripts\python.exe -m backend.crawler.runner --once

# 只跑其中一路
.venv\Scripts\python.exe -m backend.crawler.runner --once --statutes-only
.venv\Scripts\python.exe -m backend.crawler.runner --once --cases-only

# 常驻（不想用计划任务时）
.venv\Scripts\python.exe -m backend.crawler.runner --serve --interval 720
```

一轮的顺序：启动**一个**浏览器 → 法条（`FlkCollector`）→ 可选自动建索引 →
案例（`CrawlPipeline`）→ 输出汇总。法条与案例共用同一浏览器，省掉重复启动与暖场。

**总开关是 `CRAWL_ENABLED=true`**（没有第二个"定时开关"，避免配了不生效）。
未启用时 `--once` 直接返回"跳过"、退出码 0。

---

## 二、法条：两种模式，二选一或并用

```ini
CRAWL_ENABLED=true
CRAWL_RUN_STATUTES=true
CRAWL_INGEST_STATUTES=true            # 采完自动调 backend.corpus.ingest --statutes
```

### 2.1 自动目录模式（**不用手填清单**，推荐）

打开后按**分类**从新到旧翻页增量采集，靠"文件幂等 + 整页已存在即停"实现增量，
不需要维护任何名单：

```ini
CRAWL_FLK_AUTO=true
CRAWL_FLK_AUTO_CATEGORIES=宪法,法律,行政法规,监察法规,司法解释
CRAWL_FLK_AUTO_MAX_PAGES=0        # 0 = 翻到"整页已存在"为止
CRAWL_FLK_AUTO_STOP_PAGES=1       # 连续 N 页整页已存在即断定该分类已追平
```

每一轮的判定逻辑：

1. 命中**非现行有效** → 过滤（不给库塞废止条文）；
2. 本地**已有同名文件** → 跳过（`docx/pdf/doc` 三种后缀都认）；
3. 其余 → 下载 + 入库；
4. 某分类**连续 N 页全部已存在** → 认为更旧的都采过了，停止该分类。

为什么这么设计（而不是"按发布日期过滤"）：站点的日期过滤参数格式随版本变动，
写死会**静默失效**（表现为每轮重采全站）。"结果按公布日期倒序 + 文件幂等"这条
路径不依赖任何私有参数，代价只是首次要多翻几页。

**建议首次流程**：先 `--dry-run` 看要补多少，再正式跑（可能跨天才追平，配额耗尽
会挂起，次日自动续）：

```powershell
# 临时改 .env：CRAWL_FLK_AUTO=true
.venv\Scripts\python.exe -m backend.crawler.runner --once --statutes-only --dry-run
.venv\Scripts\python.exe -m backend.crawler.runner --once --statutes-only
```

⚠ 默认**不含"地方法规"**：它数万条且与多数业务相关性低，全量自动跑会常年吃满
单日配额。需要时加进 `CRAWL_FLK_AUTO_CATEGORIES`，并用 `--limit` / `MAX_PAGES` 限量。

### 2.2 从 `data/raw/` 推导模式（"我在 raw 里怎么分，就怎么采"）

按你已有的目录组织去采，不必另维护清单：

```ini
CRAWL_FLK_FROM_RAW=true
CRAWL_FLK_RAW_INCLUDE_FILES=false   # true = 连文件名也当检索词
CRAWL_FLK_RAW_MAX_TERMS=0           # 0 = 不限
CRAWL_FLK_RAW_EXCLUDE=
```

规则：

| raw/ 里的东西 | 变成 | 检索方式 |
| --- | --- | --- |
| **目录名**（`劳动法/`） | 业务主题 | 模糊（标题包含） |
| **文件名**（`中华人民共和国劳动法.pdf`） | 具体法规 | 模糊；去扩展名与 `_日期` 后缀 |

为什么统一走**模糊**而不是精确标题：目录名是主题（"劳动法"）不是法规全称；文件名里也
常见简称（`raw/劳动法/民法典.pdf`），精确标题会一律匹配不上。

**不会自我循环**：以下都会被自动排除——采集输出目录（`CRAWL_FLK_DIR`）、文书模板目录、
名字含"案例/模板"的项、清洗后过短或纯数字的项。这样"采下来的文件"不会变成下一轮的检索词。

⚠ `CRAWL_FLK_RAW_INCLUDE_FILES=true` 会把每个文件名都变成一个检索任务（本机实测 76 个），
请求量很大；**目录名（主题级）通常已够覆盖**，只在需要逐部精确补时才打开。

#### 落盘位置：按主题目录归置

`CRAWL_FLK_GROUP_BY_TOPIC=true`（默认）时，**检索词在 `data/raw/` 下有同名目录，采到的法规就存进那个目录**：

| 检索词 | 落盘位置 |
| --- | --- |
| `婚姻法`（`raw/婚姻法/` 存在） | `data/raw/婚姻法/` |
| `劳动法`（`raw/劳动法/` 存在） | `data/raw/劳动法/` |
| `民法典`（无同名目录） | 回落到 `CRAWL_FLK_DIR`（`data/raw/国家法律法规库/`） |

开关关闭时全部落 `CRAWL_FLK_DIR`（旧行为）。两条相关约定：

- **不重复下载**：`existing_path` 会先查主题目录、再查回落目录；早先落在
  `国家法律法规库/` 里的同一部法规不会被重复下载。
- ⚠ **不要与 `CRAWL_FLK_RAW_INCLUDE_FILES` 同时开**：采下来的文件会落进主题目录，
  下一轮又会被当成检索词（自我循环）。同时开启时启动会打一条明确告警。

#### 业务领域自动识别（按"劳动法 / 知识产权"这类领域归档）

站点只给**效力层级**（法律/行政法规/司法解释…），**没有业务领域字段**，所以业务领域
由**模型自己判断**，并**自己新建**对应目录：

```ini
CRAWL_FLK_AUTO_TOPIC=true
CRAWL_FLK_AUTO_TOPIC_BATCH=20      # 一次交给模型多少个标题
```

工作方式：采集到一批结果后，把**还没归位**的标题交给模型判领域（一批一次调用），
落盘时据此决定目录。**分类结果没有 json 缓存 —— 记录就是"文件在哪个目录"**：法规一旦
落进 `data/raw/<领域>/` 就算分类完成，之后**永不重新判定**（重跑采集与归位都是幂等的，
不会把文件搬到别的目录去）。实测效果（12 个标题）：

```
公司法→商法  个人所得税法→财税  刑法→刑事  环境保护法→环境资源
道路交通安全法→交通法  国家安全法→国家安全  教育法→教育法
体育法→体育法  仲裁法→诉讼仲裁  专利法→知识产权法（复用了已有目录名）
民法典→（空，按预期留空）
```

四条必须先知道的约定：

- **已归位的文件不再问模型**，"分类稳定"由目录本身保证：同一部法规不会这次进 `劳动法`、
  下次进 `劳动关系`。模型输出带随机性也无所谓，因为它只会被问一次。反过来说：
  把文件手工搬进别的领域目录，就等于**改分类**（不会被改回来）。
- **模型不可用时一个字都不写**。LLM 客户端探测不到模型会**静默降级**到规则引擎，
  那种输出不是领域名；当成目录名会污染整棵 `data/raw/`。所以检查 `degraded`，
  降级就整批放弃、按原目录落盘（**宁可不错，也不要造出垃圾目录**）。
- **领域名要净化才能当目录名**：剔除 `/ \ : * ? " < > |`、书名号，限长 20 字，
  拒绝"未知/其他"这类占位词。
- **落盘优先级**：显式/按检索词选定的主题目录 → **模型判定** → 关键词规则表（可选兜底）
  → `CRAWL_FLK_DIR`。落盘是同步的，所以判定必须**先预热**（采集循环里按批做），
  不能在落盘那一刻现场等网络调用。

模型判断不了的（如《民法典》这类综合法典）会留在 `CRAWL_FLK_DIR` —— 这是预期行为。

不想调模型时，可以配关键词规则表当兜底（字段格式 `领域=关键词1,关键词2`，顺序即优先级）。
注意**关键词别写太泛**：实测把 `全国人民代表大会` 当关键词一次吸走 195 个文件，
因为大量条目是"全国人大常委会关于修改《X》的决定"，标题里天然含这四个字。

归位脚本（把已下载的平铺文件按领域搬进目录）**默认只预演**，迁移不可逆：

```powershell
.venv\Scripts\python.exe scripts\reorganize_statutes.py            # 只判未归位的，列出计划（可反复跑）
.venv\Scripts\python.exe scripts\reorganize_statutes.py --apply    # 确认无误再真移动
.venv\Scripts\python.exe scripts\reorganize_statutes.py --no-llm   # 不用模型，只走规则表
```

### 2.3 手填清单模式

需要精确控制"只采这几部法"时用（与 `scripts/fetch_flk_statutes.py` 同一套配置）：

```ini
CRAWL_FLK_NAMES=中华人民共和国民法典     # 或 CRAWL_FLK_NAMES_FILE=名单.txt
CRAWL_FLK_KEYWORDS=                        # 关键词 × 级别 = 笛卡尔积任务
CRAWL_FLK_CATEGORIES=
```

**两种模式可以并用**：自动模式先跑（相当于增量同步），随后再跑手填清单（相当于
精确补漏），结果合并上报。两者都没配时，法条这路会**明确跳过**（不是静默成功）。

---

## 三、案例：中国裁判文书网（wenshu）

> ⚠ **合规前提**：该站有严格使用条款且部署了登录与验证码校验，**必须已获授权**
> 再自动化。适配器沿用 flk 的边界：不破解验证码（命中即转人工挂起）、随机间隔、
> 单日配额、不伪装身份。

### 3.1 首次：先有头登录一次

登录态会落盘（`CRAWL_STATE_DIR/wenshu.json`），之后无头即可续跑。

```powershell
# 1) .env 里打开开关
#    CRAWL_WENSHU_ENABLED=true
#    CRAWL_WENSHU_KEYWORDS=劳动争议,买卖合同

# 2) 有头跑一次，在弹出的浏览器里人工登录 / 过验证码
.venv\Scripts\python.exe -m backend.crawler.runner --once --cases-only --headed

# 3) 之后正常（无头）跑即可
.venv\Scripts\python.exe -m backend.crawler.runner --once --cases-only
```

### 3.2 案例目标的配置

| 配置 | 说明 |
| --- | --- |
| `CRAWL_WENSHU_ENABLED` | 案例采集开关 |
| `CRAWL_WENSHU_KEYWORDS` | 案由/争议焦点，逗号分隔，**每个关键词一个断点** |
| `CRAWL_WENSHU_MAX_PAGES` | 每个关键词最多翻几页 |
| `CRAWL_WENSHU_LIST_URL` | 列表页地址；留空用默认检索页 |
| `CRAWL_WENSHU_KEYWORD_PARAM` | 关键词拼进 URL 的参数名；**站点不认 URL 参数时留空** |
| `CRAWL_WENSHU_LINK_PATTERN` | 详情链接正则（须含命名组 `u`）；留空用默认 |
| `CRAWL_WENSHU_SELECTORS` | 详情页选择器 `body=#content,title=h1`；留空用默认 |

采集结果写入 **`la_crawl_doc`**（未核验区，与已核验的 `la_knowledge` 物理隔离）。
要进入 `case_search` 可检索的类案库，还需要一步"核验/提升"——这是刻意的隔离，
未核验内容不应直接进检索召回。

### 3.3 选择器失效了怎么发现、怎么修

默认的链接正则与选择器是按站点公开页面的常见结构给出的，**不保证长期有效**。

- **发现**：看日志里 `crawler.pipeline` 抽到的详情链接数；若为 0，说明 `CRAWL_WENSHU_LINK_PATTERN`
  已不匹配。入库后看某行的 `extra.field_sources`：`dom` = 选择器命中，`regex`/空 = 走了兜底或没抽到。
- **修**：直接在 `.env` 覆盖 `CRAWL_WENSHU_LINK_PATTERN` / `CRAWL_WENSHU_SELECTORS`，**不必改代码**。
- **若列表由 JS 触发检索**（URL 参数无效）：把 `CRAWL_WENSHU_KEYWORD_PARAM` 留空，
  改用「固定列表页 URL（如按案由的页面）+ 关键词留空」，每个 URL 配一个关键词条目。

---

## 四、注册为 Windows 计划任务

```powershell
# 默认每天 23:30 跑一轮
powershell -ExecutionPolicy Bypass -File scripts\register_crawl_task.ps1 -Time 23:30

# 立刻试跑一次
Start-ScheduledTask -TaskName LegalAgentCrawl

# 查看/删除
Get-ScheduledTask -TaskName LegalAgentCrawl
powershell -ExecutionPolicy Bypass -File scripts\register_crawl_task.ps1 -Unregister
```

实际执行的是 `scripts/run_crawl_once.bat`，它：

- 切到仓库根目录，用 `.venv` 的解释器跑 `python -m backend.crawler.runner --once --json`；
- 日志追加到 `logs\crawl.log`（强制 UTF-8，中文不会乱码）；
- 记录每轮的开始/结束时间与退出码。

计划任务的两处设置（脚本已内建）：

- `-MultipleInstances IgnoreNew`：**绝不重叠**。一轮可能因等验证码 / 等次日配额而长时间阻塞，
  重叠会两个进程抢同一批配额。
- `-StartWhenAvailable`：错过触发时间（机器关机）后开机补跑一次。

> 任务以**当前用户**、交互式方式运行，因而能复用已登录的浏览器会话；
> 用户注销时不会运行。若需注销后仍跑，请自行用 `-User/-Password` 注册。

---

## 五、退出码与日志

| 退出码 | 含义 | 任务计划的处理 |
| --- | --- | --- |
| `0` | 正常，或未启用/不在时间窗（**跳过不是失败**） | 无需动作 |
| `3` | 有任务**挂起**：验证码转人工、配额触顶 | 需要有人处理（过验证码 / 等次日） |
| `1` | 真失败 | 需要排查 |

**为什么把"挂起"单列**：标成失败会让人反复重跑（每次都撞同一道墙）；标成成功又会
让人以为采完了。这两件事对"要不要人工介入"的含义完全不同。

`--json` 输出结构化汇总，供外部脚本/报警消费。

---

## 六、配置清单

```ini
CRAWL_ENABLED=true                  # 采集总开关（也是自动化的总开关）
CRAWL_RUN_STATUTES=true             # 一轮里是否跑法条
CRAWL_RUN_CASES=true                # 一轮里是否跑案例
CRAWL_INGEST_STATUTES=true          # 法条采完自动建索引
CRAWL_INTERVAL_MINUTES=1440         # 仅 --serve 用
CRAWL_TIME_WINDOW=                  # 如 "22-6"；跨零点已支持；留空=不限

CRAWL_FLK_AUTO=false                # 自动目录模式：不填清单也能增量更新
CRAWL_FLK_AUTO_CATEGORIES=宪法,法律,行政法规,监察法规,司法解释
CRAWL_FLK_AUTO_MAX_PAGES=0          # 0 = 翻到"整页已存在"为止
CRAWL_FLK_AUTO_STOP_PAGES=1

CRAWL_FLK_FROM_RAW=false            # 按 data/raw/ 的目录名(主题)/文件名(法规)推导检索词
CRAWL_FLK_RAW_INCLUDE_FILES=false   # true = 文件名也当检索词（任务数会变大）
CRAWL_FLK_RAW_MAX_TERMS=0
CRAWL_FLK_RAW_EXCLUDE=
CRAWL_FLK_GROUP_BY_TOPIC=true       # 落盘到 raw/<主题>/（无同名目录则落 CRAWL_FLK_DIR）
CRAWL_FLK_TOPIC_RULES_FILE=./data/crawl/topic_rules.txt   # 业务领域分类规则表
CRAWL_FLK_TOPIC_RULES=              # 也可内联：劳动法=劳动,工伤;知识产权=专利,商标

CRAWL_WENSHU_ENABLED=false
CRAWL_WENSHU_KEYWORDS=
CRAWL_WENSHU_COURTS=                # 只记进 extra 供人工核对，不做站点侧过滤
CRAWL_WENSHU_MAX_PAGES=1
CRAWL_WENSHU_PAGE_SIZE=20
CRAWL_WENSHU_LIST_URL=
CRAWL_WENSHU_PAGE_PARAM=page
CRAWL_WENSHU_KEYWORD_PARAM=searchKeyword
CRAWL_WENSHU_LINK_PATTERN=
CRAWL_WENSHU_SELECTORS=
```

---

## 七、故障排查

| 现象 | 原因 / 处理 |
| --- | --- |
| 一轮什么都没采 | `CRAWL_ENABLED=false`；或清单为空（报告里会写明"跳过"原因） |
| 退出码 3 | 命中验证码或配额触顶：`--headed` 跑一次处理验证码，或等次日配额刷新 |
| 案例抽出 0 个链接 | `CRAWL_WENSHU_LINK_PATTERN` 与站点结构不符 → 核对后覆盖 |
| 要求登录 | 未登录态：`--headed` 跑一次完成登录，登录态会自动落盘 |
| 案例要能被检索 | 需先把 `la_crawl_doc` 里已核验的记录提升进类案库（当前为人工/规则流程） |

---

## 八、相关测试

```powershell
.venv\Scripts\python.exe -m pytest tests/test_crawler_runner.py -q
.venv\Scripts\python.exe -m pytest tests/test_crawler_throttle.py tests/test_crawler_fetcher_opts.py `
    tests/test_crawler_scheduler.py tests/test_crawler_pipeline.py -q
```
