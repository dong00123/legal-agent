# 律师专用多 Agent 法律咨询助手 —— API / worker 镜像
#
# 一个镜像两种用法（入口不同）：
#   uvicorn backend.app.main:app --host 0.0.0.0 --port 8001   # API
#   python -m backend.worker.app                              # 任务 worker
#
# 构建（**必须在项目根目录**，因为 COPY 的上下文是整个仓库）：
#   docker build -f deploy/Dockerfile -t legal-agent/api:latest .

# 用 3.11 而不是 3.13：torch / pymilvus / langgraph 在 3.11 上的轮子最全，
# 装的时候不会退化到源码编译（一次编译要十几分钟，且需要 build-essential）。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# ---------------------------------------------------------------- 系统依赖
# **这一步失败不让构建中断**，但会把理由打印在构建日志里。
# 原因：内网/离线环境里 apt 源经常不可达，而这两样其实都不是必需的 ——
#   · libgomp（OpenMP 运行时）：numpy / torch 的 manylinux 轮子**自带**
#     （在 numpy.libs/ 下），不需要系统包；
#   · tzdata：python:3.11-slim 基础镜像已带（实测 `zoneinfo.ZoneInfo("Asia/Shanghai")`
#     可直接用），审计日志的时间戳不受影响。
# 所以装不上就跳过。若某台机器上 `import numpy` 报
# "libgomp.so.1: cannot open shared object file"，再手动装一次即可：
#   apt-get update && apt-get install -y libgomp1
# 不装 build-essential：依赖清单里没有需要源码编译的包。
RUN (apt-get update \
     && apt-get install -y --no-install-recommends libgomp1 tzdata \
     && rm -rf /var/lib/apt/lists/*) \
    || echo "[构建提示] apt 不可达或包缺失，已跳过系统依赖安装。numpy/torch 的轮子自带 libgomp，tzdata 基础镜像已带；若运行时报 libgomp.so.1 缺失，手动装 libgomp1 即可。"

# ---------------------------------------------------------------- 依赖
# 先只复制 requirements.txt 再装依赖：这样改代码不会让 Docker 缓存失效，
# 重新构建只跑最后一层 COPY（几秒），而不是重装 torch（十几分钟）。
#
# **副作用，比听起来贵得多：** 上面这个顺序意味着 **改 requirements.txt 里的
# 一个字（哪怕只是一条注释）都会让 torch 层缓存失效，重建时重新下载那 196MB。**
# 实测就是这样栽过：为了改一句说明性注释，换来几分钟的重新下载。
# 真要调依赖内容别犹豫；只是润色注释的话，考虑攒到下次一起改，或者干脆
# 把说明挪到 docs/ 里。
COPY requirements.txt ./

# **torch 必须单独先装，且默认走 CPU 索引。** 这不是"优化"，是能不能构建完的问题：
#   · PyPI 上的 torch 轮子是 CUDA 版，连带拉入 nvidia-cublas / cudnn / nccl 等
#     一堆依赖，**合计 3GB+**。实测在本机构建 42 分钟仍未结束（卡在下载），
#     而构建日志被流水线缓冲，外面只能看到"没输出"，很难判断是慢还是死。
#   · 本项目在容器里用 torch **只做 BGE-small 的 CPU 推理**（bge-small-zh-v1.5，
#     单条几十毫秒），GPU 是给另一个容器里的 SGLang 跑大模型用的 ——
#     所以这几个 GB 的 CUDA 运行时在 API 镜像里完全用不上。
#   · CPU 轮子约 190MB，构建从 40 分钟量级降到几分钟，镜像也小一大截。
# 需要 GPU 版（比如要给容器里的 embedding 也上卡）时显式指定：
#   docker build --build-arg TORCH_INDEX_URL=https://pypi.org/simple ...
# 置空（``--build-arg TORCH_INDEX_URL=``）则完全跳过这一步，由 requirements.txt
# 里的 sentence-transformers 从 PyPI 自己解 —— 等价于退回旧行为。
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
# **拆成两层 RUN，不要合成一层。** Docker 按 RUN 提交层：合成一层时，
# 只要后半段（装其余依赖）被中断，前面已经下载好的 torch 也一起丢，
# 下次重来还得从头下载。拆开之后 torch 装完立即固化成一个可复用层。
RUN set -eu; \
    python -m pip install --upgrade pip; \
    [ -n "${TORCH_INDEX_URL}" ] || exit 0; \
    python -m pip install --index-url "${TORCH_INDEX_URL}" torch \
    || { echo "[构建失败] 无法从 ${TORCH_INDEX_URL} 安装 torch。换一个源：" \
         "--build-arg TORCH_INDEX_URL=https://pypi.org/simple；" \
         "或置空跳过预装（会退回 PyPI 的 CUDA 轮子，约 3GB）。"; exit 1; }

RUN python -m pip install -r requirements.txt

# ---------------------------------------------------------------- 代码
COPY backend/ ./backend/
COPY scripts/ ./scripts/
# 前端**要进镜像**：API 自己也托管 /ui（``backend/app/main.py`` 的
# ``_mount_frontend``）。用 nginx 托管时它用不到，但少这一行的话
# 直接访问 api:8001/ui/ 会是 404 —— 而这个入口常被用来判断"后端起没起来"。
COPY web/ ./web/

# ---------------------------------------------------------------- 非 root 运行
# **不这么做会在容器里报 `readonly database`。** 以 root 跑没有这个问题，
# 但把 API 进程以 root 暴露在外网是另一类风险。所以建一个用户，
# 并**显式给 /app/data 属主** —— 命名卷会自动继承镜像里目录的属主，
# 这里不给，卷挂上来之后应用就没法写导出文件了。
RUN useradd -m -u 10001 appuser \
    && mkdir -p /app/data /app/corpus \
    && chown -R appuser:appuser /app
USER appuser

# 目录要在运行时也存在（ensure_dirs 会建，但卷挂载点先建好更明确）
RUN mkdir -p /app/data/uploads /app/data/exports /app/data/processed \
             /app/data/index /app/data/audit /app/data/knowledge

EXPOSE 8001

# 探针用 python 而不是 curl：slim 镜像里没有 curl。
# `full=false` 是刻意的 —— 完整检查会加载 embedding 模型，冷启动十几秒，
# 拿它当探针会让容器在启动期被判死、反复重启。
HEALTHCHECK --interval=30s --timeout=40s --start-period=90s --retries=5 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8001/health?full=false',timeout=30)"

CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8001"]
