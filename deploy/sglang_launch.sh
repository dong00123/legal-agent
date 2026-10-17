#!/usr/bin/env bash
# 在算力云（AutoDL 等）实例上启动两个 SGLang 服务：文本模型 + 多模态模型。
#
# 别和「检索服务」混：bge 向量/精排那套是 **CPU** 服务、端口 6006，脚本叫 autodl_setup.sh ——
# 但那份只存在于**分发态副本**（Desktop\legal-agent\deploy\），本副本（legal-agent2）的
# deploy/ 里并没有它（实测 2026-09-23）。本脚本只管 GPU 上的大模型（30000 / 30001）。
#
# 用法（在算力云实例的终端里跑，不是在 Windows 本机）：
#   bash sglang_launch.sh gpu            # 只看有几张卡、多少显存（只读，先跑这个）
#   bash sglang_launch.sh llm            # 起文本模型
#   bash sglang_launch.sh vl             # 起多模态模型
#   bash sglang_launch.sh both           # 两张卡及以上时一次起两个
#   bash sglang_launch.sh status         # 看端口 + 拉 /v1/models（只读）
#   bash sglang_launch.sh diag           # 版本体检 + 排查 duplicate template name（只读，起不来先跑它）
#   bash sglang_launch.sh logs llm|vl    # 跟着看加载日志（只读，Ctrl+C 退出不影响服务）
#   bash sglang_launch.sh attach llm|vl  # 回到服务所在的 screen 会话
#   bash sglang_launch.sh stop [llm|vl|all]
#
# ⚠️ 为什么用 screen：AutoDL 终端一关页面/SSH 一断，挂在该会话下的进程组会被整体杀掉。
#    `cmd &` 挡不住，`nohup` 只挡 SIGHUP 也不保险——平台自己就红字要求“用 screen 等工具”。
#    所以本脚本优先用 screen 起服务；实例里没有 screen 才回退 nohup 并明确告警。
#
# 可覆盖的环境变量（都有默认值，一般不用动）：
#   LLM_DIR VL_DIR LLM_PORT VL_PORT LLM_NAME VL_NAME API_KEY CTX MEM_FRACTION
#   LOG_DIR（默认数据盘 /root/autodl-tmp，系统盘小）PY EXTRA_ARGS SCREEN

set -uo pipefail

LLM_DIR="${LLM_DIR:-/root/autodl-tmp/Qwen2.5-7B-Instruct}"
VL_DIR="${VL_DIR:-/root/autodl-tmp/Qwen3-VL-8B-Instruct}"
LLM_PORT="${LLM_PORT:-30000}"
VL_PORT="${VL_PORT:-30001}"
LLM_NAME="${LLM_NAME:-Qwen2.5-7B-Instruct}"
VL_NAME="${VL_NAME:-Qwen3-VL-8B-Instruct}"
API_KEY="${API_KEY:-sk-legal-local-2026}"
CTX="${CTX:-32768}"
MEM_FRACTION="${MEM_FRACTION:-0.85}"
LOG_DIR="${LOG_DIR:-$([ -d /root/autodl-tmp ] && echo /root/autodl-tmp || echo /root)}"
PY="${PY:-python3}"
EXTRA_ARGS="${EXTRA_ARGS:-}"
SCREEN="${SCREEN:-auto}"   # auto | off（off 则强制用 nohup）

# 三个模型的显存账（bf16 权重，粗算）：7B≈15GB、8B-VL≈17GB。
# 24G 单卡装不下两个 ⇒ 单卡必须二选一，这不是参数能绕开的事。
WEIGHT_LLM=15
WEIGHT_VL=17

say() { printf '%s\n' "$*"; }
die() { printf '✗ %s\n' "$*" >&2; exit 1; }

have_screen() { [ "$SCREEN" != "off" ] && command -v screen >/dev/null 2>&1; }
sess_name() { echo "sg-$1"; }

# 端口是否有人在听。
# ⛔ AutoDL 容器常常**连 ss 都没有**（iproute2 没装，`ss: command not found`），
#    net-tools 也不一定有 ⇒ 最终一律回退到 python 的 connect 探测（python3 必然存在）。
port_in_use() {
    local port="$1"
    if command -v ss >/dev/null 2>&1; then
        ss -lnt 2>/dev/null | grep -q ":${port} " && return 0
        return 1
    fi
    if command -v netstat >/dev/null 2>&1; then
        netstat -lnt 2>/dev/null | grep -q ":${port} " && return 0
        return 1
    fi
    "$PY" -c 'import socket,sys
s=socket.socket(); s.settimeout(1)
try:
    s.connect(("127.0.0.1", int(sys.argv[1]))); print("OPEN")
except Exception:
    pass
finally:
    s.close()' "$port" 2>/dev/null | grep -q OPEN
}

# 版本体检 + 新老布局共存排查。
# 「duplicate template name」这个报错几乎只由两件事引起：torch 升级后**新老两套
# _inductor/kernel 布局共存**（flex_attention.py 与 flex/ 同时存在 ⇒ 同名模板注册两次），
# 或 torch/triton/sglang 三者的 ABI 版本不匹配（sglang 的内核是照着特定 torch 编的）。
cmd_diag() {
    say "== 版本三件套（sglang 的内核按特定 torch ABI 编译，三者必须匹配）=="
    "$PY" - <<'PYEOF'
import importlib
for name in ("torch", "triton", "sglang", "flashinfer", "sgl_kernel"):
    try:
        m = importlib.import_module(name)
        print(f"  {name:11s} {getattr(m, '__version__', '(无 __version__)')}")
    except Exception as e:
        print(f"  {name:11s} 导入失败: {type(e).__name__}: {e}")
PYEOF
    say ""
    local kdir
    kdir="$("$PY" -c 'import torch, os; print(os.path.join(os.path.dirname(torch.__file__), "_inductor", "kernel"))' 2>/dev/null)"
    say "== ${kdir} 下 flex 相关条目 =="
    ls -d "${kdir}"/flex* 2>/dev/null || say "  （列不出来）"
    say "  ⇒ 同时出现 flex_attention.py**和** flex/ 目录 = 升级残留，会报 duplicate template name"
    say ""
    say "== 单独导入 torch._inductor（复现/排除上面那个报错）=="
    "$PY" -c "import torch._inductor.lowering; print('  OK：torch._inductor 导入正常')" 2>&1 | tail -n 6
    say ""
    say "== 端口 =="
    for port in "$LLM_PORT" "$VL_PORT"; do
        if port_in_use "$port"; then say "  :${port} 有人在听"; else say "  :${port} 空的"; fi
    done
}

# 跟着看日志。⚠️ 用 tail 而不是 screen attach：Ctrl+C 退出 tail 不会碰到服务进程。
cmd_logs() {
    local which="${1:-llm}"
    case "$which" in llm|vl) ;; *) die "logs 只认 llm / vl" ;; esac
    say "== ${LOG_DIR}/sglang-${which}.log（Ctrl+C 退出，不影响服务）=="
    if [ -f "${LOG_DIR}/sglang-${which}.log" ]; then
        tail -n 40 -f "${LOG_DIR}/sglang-${which}.log"
    else
        die "日志还不存在：${LOG_DIR}/sglang-${which}.log —— 服务可能没起来，先跑 status"
    fi
}

cmd_attach() {
    local which="${1:-llm}" port
    case "$which" in
        llm) port="$LLM_PORT" ;;
        vl)  port="$VL_PORT" ;;
        *) die "attach 只认 llm / vl" ;;
    esac
    have_screen || die "这台机器没有 screen（或 SCREEN=off），服务是用 nohup 起的，直接看日志即可"
    say "== 回到 $(sess_name "$port")；看完按 Ctrl+A 再按 D 脱离（⛔ 别按 Ctrl+C，那会杀掉服务）=="
    screen -r "$(sess_name "$port")"
}

gpu_count() {
    command -v nvidia-smi >/dev/null 2>&1 || { echo 0; return; }
    nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | wc -l | tr -d ' '
}

show_gpu() {
    say "== 显卡 =="
    if ! command -v nvidia-smi >/dev/null 2>&1; then
        die "没有 nvidia-smi —— 这个实例不是 GPU 实例（或驱动没起来）。SGLang 跑不了。"
    fi
    nvidia-smi --query-gpu=index,name,memory.total,memory.used --format=csv
    local n; n="$(gpu_count)"
    say ""
    say "卡数 = ${n}；两个模型权重合计约 $((WEIGHT_LLM + WEIGHT_VL)) GB（不含 KV cache）"
    if [ "$n" -ge 2 ]; then
        say "⇒ 可以 two-GPU 各占一张，用 both 起两个。"
    else
        local total
        total="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')"
        if [ "${total:-0}" -ge 60000 ]; then
            say "⇒ 单卡 ${total}MiB，够两个一起跑（跑 both 会自动各让一半显存）。"
        else
            say "⇒ 单卡 ${total}MiB，**装不下两个**。先起 llm（主链路靠它），"
            say "   要识别扫描件时再 stop llm 后起 vl。"
        fi
    fi
}

need_dir() {
    [ -d "$1" ] || die "模型目录不存在：$1
   先确认真实路径（你截图里那两个文件夹）：
     ls -d /root/*/Qwen2.5-7B-Instruct /root/*/Qwen3-VL-8B-Instruct 2>/dev/null
   然后 LLM_DIR=<真实路径> bash sglang_launch.sh llm"
}

# start_one <模型目录> <对外模型名> <端口> <GPU编号或空> <显存占比> <日志文件>
start_one() {
    local dir="$1" name="$2" port="$3" gpu="$4" frac="$5" log="$6"
    need_dir "$dir"

    if port_in_use "$port"; then
        say "• 端口 ${port} 已在监听，跳过（要重起先 stop）"
        return 0
    fi

    local -a cmd=("$PY" -m sglang.launch_server
        --model-path "$dir"
        --host 0.0.0.0
        --port "$port"
        --served-model-name "$name"
        --context-length "$CTX"
        --mem-fraction-static "$frac"
        --api-key "$API_KEY")
    # shellcheck disable=SC2206
    [ -n "$EXTRA_ARGS" ] && cmd+=($EXTRA_ARGS)

    say "• 启动 ${name} → :${port}${gpu:+ (GPU ${gpu})}"

    local sess cmdstr
    sess="$(sess_name "$port")"
    cmdstr="$(printf '%q ' "${cmd[@]}")"

    if have_screen; then
        # screen 里的进程自成会话，关掉浏览器/SSH 也不会被带走。
        # exports 和 exec 都写在里面：exec 让 python 直接取代 bash，不留中间壳进程。
        local inner="exec $cmdstr > $(printf '%q' "$log") 2>&1"
        [ -n "$gpu" ] && inner="export CUDA_VISIBLE_DEVICES=$gpu; $inner"
        if screen -dmS "$sess" bash -c "$inner"; then
            say "  会话=$(sess_name "$port")  日志=$log"
            say "  看日志：bash $0 logs $( [ "$port" = "$LLM_PORT" ] && echo llm || echo vl )"
            say "  取回前台：bash $0 attach $( [ "$port" = "$LLM_PORT" ] && echo llm || echo vl )（看完 Ctrl+A 再 D 脱离）"
            say "  （加载权重要 1~3 分钟）"
            return 0
        fi
        say "  ⚠️ screen 启动失败，回退 nohup"
    fi

    # 回退路径：实例里没装 screen。这种形态**关掉终端页就会被杀**，只适合临时试跑。
    if [ -n "$gpu" ]; then
        CUDA_VISIBLE_DEVICES="$gpu" nohup "${cmd[@]}" > "$log" 2>&1 &
    else
        nohup "${cmd[@]}" > "$log" 2>&1 &
    fi
    say "  pid=$!  日志=$log  （加载权重要 1~3 分钟）"
    say "  ⚠️ 没装 screen，进程挂在当前终端下：关掉终端页 / SSH 断线就会被杀"
    say "     想让长跑稳当，装一个：apt-get install -y screen  （或 pip install screen 不行，得系统包）"
}

wait_ready() {
    local port="$1" tries="${2:-60}" i=0
    # ⛔ curl 会走 http_proxy 环境变量 ⇒ 探本机必须 --noproxy '*'
    while [ "$i" -lt "$tries" ]; do
        if curl -s --noproxy '*' -m 3 "http://127.0.0.1:${port}/v1/models" | grep -q '"id"'; then
            return 0
        fi
        sleep 3; i=$((i + 1))
    done
    return 1
}

start_llm() { start_one "$LLM_DIR" "$LLM_NAME" "$LLM_PORT" "${1:-}" "$MEM_FRACTION" "${LOG_DIR}/sglang-llm.log"; }
start_vl()  { start_one "$VL_DIR"  "$VL_NAME"  "$VL_PORT"  "${1:-}" "$MEM_FRACTION" "${LOG_DIR}/sglang-vl.log"; }

print_env() {
    say ""
    say "== 客户端 .env 该填什么（Windows 本机项目根，⛔ 只能改完重启，不能热改）=="
    say "LLM_PROVIDER=openai"
    say "LLM_BASE_URL=http://127.0.0.1:${LLM_PORT}/v1     # 走 SSH 隧道时就是 127.0.0.1"
    say "LLM_API_KEY=${API_KEY}"
    say "LLM_MODEL=${LLM_NAME}"
    say "VLM_ENABLED=true"
    say "VLM_BASE_URL=http://127.0.0.1:${VL_PORT}/v1"
    say "VLM_API_KEY=${API_KEY}"
    say "VLM_MODEL=${VL_NAME}"
}

cmd_status() {
    for pair in "文本:${LLM_PORT}" "多模态:${VL_PORT}"; do
        local label="${pair%%:*}" port="${pair##*:}"
        local out
        out="$(curl -s --noproxy '*' -m 5 "http://127.0.0.1:${port}/v1/models" 2>&1)"
        if printf '%s' "$out" | grep -q '"id"'; then
            say "[ON ] ${label} :${port}  →  $(printf '%s' "$out" | tr -d '\n' | cut -c1-160)"
        else
            say "[OFF] ${label} :${port}  →  $(printf '%s' "$out" | tr -d '\n' | cut -c1-160)"
        fi
    done
    say ""
    say "（本机 Windows 那边看到的 127.0.0.1 是隧道给的，不是这台机器）"
}

cmd_stop() {
    local which="${1:-all}"
    stop_port() {
        local port="$1" pids sess
        sess="$(sess_name "$port")"
        if have_screen && screen -ls 2>/dev/null | grep -q "[.]${sess}[[:space:]]"; then
            screen -S "$sess" -X quit 2>/dev/null && say "• 已退出 screen 会话 ${sess}"
            sleep 1
        fi
        pids="$(pgrep -f "sglang.launch_server.*--port ${port}" || true)"
        if [ -z "$pids" ]; then say "• :${port} 没有进程"; return 0; fi
        # shellcheck disable=SC2086
        kill $pids 2>/dev/null || true
        sleep 2
        # shellcheck disable=SC2086
        kill -9 $pids 2>/dev/null || true
        say "• 已停止 :${port}（pid: $(echo $pids | tr '\n' ' ')）"
    }
    case "$which" in
        llm) stop_port "$LLM_PORT" ;;
        vl)  stop_port "$VL_PORT" ;;
        all) stop_port "$LLM_PORT"; stop_port "$VL_PORT" ;;
        *) die "stop 只认 llm / vl / all" ;;
    esac
}

case "${1:-}" in
    gpu) show_gpu ;;
    llm) start_llm; wait_ready "$LLM_PORT" && say "✓ 文本模型就绪" || say "✗ 等待超时，看 ${LOG_DIR}/sglang-llm.log"; print_env ;;
    vl)  start_vl;  wait_ready "$VL_PORT"  && say "✓ 多模态模型就绪" || say "✗ 等待超时，看 ${LOG_DIR}/sglang-vl.log"; print_env ;;
    both)
        n="$(gpu_count)"
        if [ "$n" -ge 2 ]; then
            start_llm 0; start_vl 1
        elif [ "$n" -eq 1 ]; then
            total="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')"
            if [ "${total:-0}" -lt 60000 ]; then
                die "单卡 ${total}MiB 装不下两个模型（权重合计约 $((WEIGHT_LLM + WEIGHT_VL))GB）。
   先用 llm；要识别扫描件时 stop llm 再起 vl。"
            fi
            # 单卡大显存：各让一半，别都吃 0.85 把对方挤爆
            MEM_FRACTION=0.45 start_llm
            MEM_FRACTION=0.45 start_vl
        else
            die "没检测到 GPU，先跑 'gpu' 子命令看看"
        fi
        wait_ready "$LLM_PORT" 60 && say "✓ 文本就绪" || say "✗ 文本超时"
        wait_ready "$VL_PORT" 60  && say "✓ 多模态就绪" || say "✗ 多模态超时"
        print_env ;;
    status) cmd_status ;;
    diag) cmd_diag ;;
    logs) shift; cmd_logs "${1:-llm}" ;;
    attach) shift; cmd_attach "${1:-llm}" ;;
    stop) shift; cmd_stop "${1:-all}" ;;
    *) sed -n '2,25p' "$0" ;;
esac
