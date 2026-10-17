# 部署

容器编排 + 离线语料导入 + 服务健康检查。

## 目录

```
deploy/
  docker-compose.yml    服务编排（mysql / redis / rabbitmq / api / worker / web + 可选 milvus / sglang）
  Dockerfile            API 与 worker 共用的镜像
  nginx/nginx.conf      静态托管 + 反代（含 SSE 与 WebSocket 的关键配置）
  mysql/init.sql        库级字符集兜底
  sglang_launch.sh      **无 Docker 的算力云**（AutoDL 等）上起两个 SGLang（文本 :30000 / 多模态 :30001）
  README.md             本文件
```

## 快速开始

```bash
# 1) 部署配置（**注意是 deploy 目录下这份，不是项目根那份 .env**）
cd deploy
cp .env.example .env        # 然后**必须**填 SECRET_KEY 与 ADMIN_PASSWORD

#    生成随机串：python -c "import secrets;print(secrets.token_urlsafe(48))"
#    这两个值留空的话 docker compose 会**拒绝启动**并提示你 ——
#    不是矫情：出厂示例密钥是公开的，用它签发 JWT 等于任何人都能伪造律师身份。

# 2) 起核心服务
docker compose up -d

# 3) 等服务就绪（冷启动要加载 embedding 模型，别用固定的 sleep）
python ../scripts/healthcheck.py --url http://127.0.0.1:8080 --wait 180

# 4) 打开 http://127.0.0.1:8080/ui/
```

**为什么部署用的是 `deploy/.env` 而不是项目根的 `.env`。** 根目录那份是本机
开发配置：`DEBUG=true`、`SECRET_KEY=change-me-in-production-legal-agent`、
`ADMIN_PASSWORD=admin123`、`REDIS_ENABLED=false`。早先的编排确实直接挂它，
结果是"照着文档部署完，容器里跑的是一套公开示例的安全配置" —— 更麻烦的是
那份文件里写死了 `ENV=dev`，正好绕开了代码里出厂密钥的告警条件
（现在这条校验改为看**进程是否在容器里**，不再依赖 ENV，见
`backend/app/main.py::_exposed_environment`）。

`.gitignore` 已把所有层级的 `.env` 排除、`*.example` 保留 —— `deploy/.env`
里有真实密钥，它一旦进版本库，事后删除也抹不掉历史记录。

可选组合：

```bash
docker compose --profile vector up -d   # 加 Milvus 向量库（需把 MILVUS_ENABLED=true）
docker compose --profile gpu up -d      # 加 SGLang 推理（需要 NVIDIA 显卡 + nvidia-container-toolkit）
```

## 镜像构建

`compose up` 会自动构建，也可以单独构建：

```bash
# 在项目根目录执行（构建上下文是整个仓库，deploy/Dockerfile 里 COPY 的是相对路径）
docker build -f deploy/Dockerfile -t legal-agent/api:latest .
```

**首次构建慢是正常的，但慢多少取决于 torch 从哪儿装** —— 这是唯一的变量：

| 装法 | torch 体积 | 实测耗时 | 什么时候用 |
| --- | --- | --- | --- |
| CPU 索引（**默认**） | 196 MB | 下载约 50 秒 | 绝大多数情况 |
| PyPI（CUDA 轮子 + `nvidia-*`） | 3 GB+ | 实测 **42 分钟仍未结束** | 要给容器里的 embedding 也上显卡时才需要 |

PyPI 上的 torch 轮子是 CUDA 版，会连带拉入 cublas / cudnn / nccl 等一堆依赖；
而本项目在容器里用 torch **只做 BGE-small 的 CPU 推理**（单条几十毫秒），
GPU 是给 `gpu` profile 里那个独立 SGLang 容器跑大模型用的。所以 Dockerfile
默认从 CPU 索引预装 torch：

```bash
docker build --build-arg TORCH_INDEX_URL=https://pypi.org/simple .  # 退回 CUDA 轮子
docker build --build-arg TORCH_INDEX_URL= .                          # 完全跳过预装
```

构建日志如果不加 `--progress=plain` 是折叠的，看起来像"卡住了"——
判断是不是真卡死，看有没有在下载某个轮子。

## 端口

| 服务 | 容器内 | 对外 | 说明 |
|---|---|---|---|
| web (nginx) | 80 | `${WEB_PORT:-8080}` | **唯一对外入口** |
| api | 8001 | 不暴露 | 只允许 nginx 转发，避免绕过网关直连后端 |
| worker | — | 不暴露 | 只连数据库与 broker |
| mysql | 3306 | 不暴露 | 需要排查时可临时加 `ports: ["3306:3306"]` |
| rabbitmq 管理台 | 15672 | 不暴露 | 同上 |

## 分 profile 的原因

默认组合在一台 8C16G 的机器上能跑通全流程：向量检索回落到本地 numpy 索引
（`MILVUS_ENABLED=false`），LLM 走你配的 OpenAI 兼容网关。Milvus 与 GPU 推理
都只是"更好"，不是"否则不能跑"；把它们塞进默认组合，会让"先起来看一眼"
变成一次两小时的排障。

## 数据卷

一律用**命名卷**，不用 bind mount。镜像里跑 uvicorn 的是非 root 用户
（`appuser`），bind mount 会用宿主机目录的属主覆盖镜像内的属主，写文件时会报
`readonly database` —— 只在"写"的时候出现，且看起来像磁盘满了。

- `app-data` → `/app/data`：上传材料、导出文书、BM25 索引、审计日志
- `corpus-packs` → `/app/corpus`（只读）：离线语料包挂载点
- `hf-cache` → SGLang 的模型缓存

## 离线语料导入

内网环境不能联网拉法规，把 PDF 打成包带进来：

```bash
# 先看清单，确认分类对不对（法规 / 类案 / 文书模板）
python scripts/import_corpus_pack.py /app/corpus/法规更新包_2026Q3.zip --dry-run

# 导入
python scripts/import_corpus_pack.py /app/corpus/法规更新包_2026Q3.zip

# 法规换新版时必须加 --force，否则同名文件被判为"已入库"而跳过，
# 库里还是旧的 —— 而导入过程不会报任何错
python scripts/import_corpus_pack.py /app/corpus/法规更新包_2026Q3.zip --force
```

导入后会回查库里的法规部数与条文数并做前后对拍；**条数没变化会明确告警**，
不要只看"过程没报错"就认为成功了。

## 健康检查

```bash
python scripts/healthcheck.py --url http://127.0.0.1:8080            # 人读
python scripts/healthcheck.py --json                                  # 流水线消费
python scripts/healthcheck.py --strict                                # 有降级也判失败（上线前用）
```

退出码：`0` 正常或仅降级；`1` 不可用；`2` `--strict` 且存在降级。

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 一切正常但重启后数据没了 | 应用静默回落到 SQLite | `healthcheck.py` 会提示；检查 `DB_URL` 主机名是不是写成了 `localhost`（容器里应为服务名 `mysql`） |
| 中文存成问号 | 库或连接字符集不是 utf8mb4 | 服务器已加 `--character-set-server=utf8mb4`，各表也都声明了；仍出现时检查 `DB_URL` 有没有带 `?charset=utf8mb4` |
| 上传大 PDF 没反应 | nginx `client_max_body_size` 默认 1m | 已设为 200m；自建网关时要同步改 |
| "边审边看"变成一次性出结果 | 反代缓冲没关 | `nginx.conf` 里 `/api/` 段已设 `proxy_buffering off`；换网关时要同步 |
| WebSocket 连上就断 | 反代没转发 Upgrade 头 | `/ws/` 段已设；换网关时要同步 |
| Milvus 容器反复 `Exited(134)` | 2.6.x standalone 的 etcd 启动竞态 | 已钉 `v2.4-latest` 并让 milvus 等 etcd healthy；升级 Mirvus 大版本时要留意 |
| 写导出文件报 `readonly database` | 用了 bind mount 覆盖了属主 | 改回命名卷（见上文） |
| 任务一直不执行 | 没有 worker 在跑 | `docker compose ps` 看 worker；`healthcheck.py` 的队列段能看到积压与卡死数 |
| 第一个请求要等十几秒，或首次工具调用报「超时（30s）」 | 预热没跑/没成功 | 启动时会自动预热检索链路（实测 17.3 秒，其中 16.8 秒是载入 BGE）。看日志有没有「检索链路预热完成」，或读 `GET /health` 的 `warmup` 字段（**`full=false` 也有**，探针正是打这一档）。`start_period: 90s` 就是给它留的余量 |
| api 容器刚起来就被判 unhealthy | 预热拖长了启动 | 别把 `start_period` 调小；也别把探针改成 `full=true`（那会让它自己也去加载模型） |
| **重启某个服务后全站 502，但后端是 healthy** | nginx 在加载配置时把 `api:8001` 解析成了旧 IP 并永久缓存 | 已改成变量 + `resolver`（ rationale 见 `nginx.conf` 文件头）。**症状极具误导性**：后端自己访问 8001 是 200、`docker compose ps` 也全绿，只有经网关访问才是 502。换网关/自己写反代时务必注意 |
| worker 永远显示 `unhealthy`，自身却在工作 | 容器继承了镜像里那条探 8001 的 HEALTHCHECK，而 worker 不监听端口 | 已在 compose 的 worker 段显式覆盖。若给 worker 换镜像或去掉这段，它会回到假 unhealthy —— 有些编排平台会据此**反复杀掉 worker**，队列里的活就没人干了 |
| worker 启动时报 `la_task doesn't exist`，随后自愈 | worker 比 api 早约 1 秒起来，抢在 `create_all` 之前查了表 | 无害：worker 的容错会继续轮询，api 建完表后一切正常。属多进程同时初始化数据库的**竞态**，知情即可 |
| `http://127.0.0.1:8080` 打开的是另一个项目 | 宿主机已有别的进程占了这个端口 | Docker 转发的是 `0.0.0.0:8080`，而宿主机上某个进程绑了**精确的** `127.0.0.1:8080`，后者按更精确匹配优先命中。用 `http://127.0.0.2:8080` 或在 `deploy/.env` 里换 `WEB_PORT=8081` |

## 本机验证状态

2026-09-18 实测（Docker Desktop 29.7.2 / `desktop-linux` builder）：

- ✅ `docker compose config`（不需要 daemon，只校验编排文件语法）
- ✅ `nginx -t`（一次性容器）
- ✅ `docker build` **构建成功**：镜像 2.48 GB
- ✅ **容器真跑起来**：`docker run -p 18001:8001 legal-agent/api:latest`
  → 状态 `Up (healthy)`，容器自己的 HEALTHCHECK 通过；
  `/health?full=false` 200（含 `warmup` 明细）、`/ui/` 返回前端 HTML、
  `/docs` 200、登录可拿到 `access_token`
- ✅ worker 入口可用：`docker run --rm --entrypoint python ... -m backend.worker.app`
  → 无 MySQL/Redis 时正确降级到 SQLite 并常驻轮询
  （顺带验证了非 root 用户对 `/app/data` 可写 —— 这就是必须上命名卷的原因）
- ✅ `scripts/healthcheck.py` 对**本机直启**的服务跑通
- ✅ `scripts/import_corpus_pack.py --dry-run` 用真实语料目录跑通
- ✅ **`docker compose up -d` 整体编排已实际拉起**（mysql / redis / api / worker /
  web 五个服务全部 `Up (healthy)`），并走了真实业务请求：
  经 nginx（`http://<宿主>:8080`）登录 200 → `/health` 200 → `POST /api/v1/chat`
  200（5.0s）。`/meta/config` 显示 `backend: mysql` —— **证明容器里连的是 MySQL
  容器，没有静默回落到 SQLite**；`task_queue` 不在降级列表里，队列用的就是库里的
  `la_task` 表。
- ❌ `broker` profile（RabbitMQ）与 `gpu`（SGLang）、`vector`（Milvus）
  三个 profile **仍未实际拉起**。前两者的前置依赖没装/没有显卡，第三个本机已有
  独立部署的 Milvus 在占 19530。
- ❌ `docker compose down` 后的**数据保留**没验（命名卷按设计应保留，未实测删除重建）。

### 一个只在容器里才会踩的坑

```python
pool.add(f"case|{re.sub(r'[\s（）()]', '', c['case_no'])}")   # ❌ Python 3.11 会 SyntaxError
```

**f-string 的表达式里不能有反斜杠**（PEP 701 才放宽，要 3.12+）。本机开发是
Python 3.13，跑得好好的、测试全绿；镜像是 3.11（torch 轮子最全），
于是容器在 **import 阶段**就崩了 —— 连一行应用日志都来不及打，现象只是"起来又退出"。

已经修掉，并加了 `tests/test_py311_compat.py` 拦住它：
这条守卫用 `ast` 扫全仓库（**不能用 `tokenize` 的 `STRING` 按 3.11 的口径过滤** ——
3.12 之后 f-string 被拆成 `FSTRING_START/MIDDLE/END`，一个都匹配不到），
并且自带"检波器自检"，避免"没扫到东西"被当成"没有违规"。

改兼容性相关问题后，最快的全量校验是让 3.11 直接编译整个仓库：

```bash
docker run --rm -v "$PWD:/src" python:3.11-slim python -m compileall -q /src/backend /src/scripts /src/client
```
