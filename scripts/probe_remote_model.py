"""探测任意 OpenAI 兼容推理端点（SGLang / vLLM / 云网关），并给出可执行的诊断结论。

**为什么需要它。** 后端连模型只认两件事：``{base_url}/models`` 返回 200、
``{base_url}/chat/completions`` 能按 ``model`` 字段找到模型。这两件事任一不成立，
后端的表现都是**静默降级到规则引擎**——接口照样 200、流程照样跑完，只是产出变成
模板味。所以换端点（比如切到算力云上的 SGLang）之后，第一步不该是重启后端看运气，
而是先用本脚本把端点问清楚。

它故意**照抄后端真实发送的报文**（``backend/llm/client.py`` 的 payload），所以
本脚本通过 == 后端也能通过；不一致的地方才是问题。

用法::

    # 1) 只列模型：确认 URL 通、并看清 model id 到底叫什么
    python scripts/probe_remote_model.py --base-url http://127.0.0.1:30000/v1

    # 2) 端到端：跑一次 chat（报文与后端一致）
    python scripts/probe_remote_model.py --base-url http://127.0.0.1:30000/v1 \\
        --model Qwen2.5-7B-Instruct

    # 3) 多模态：验证 VLM 通道（会自动用项目里的 VLM prompt 风格问一句）
    python scripts/probe_remote_model.py --base-url http://127.0.0.1:30001/v1 \\
        --model Qwen3-VL-8B-Instruct --image D:/some/scan.jpg

退出码：0 全部通过；1 有失败（便于在 CI / 启动脚本里串起来）。
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_TIMEOUT = 60.0


def _post(url: str, payload: dict, api_key: str, timeout: float) -> tuple[int, dict | str]:
    """返回 (status, body)。4xx/5xx 也把响应体读回来——SGLang 的报错信息很有用。"""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key or 'EMPTY'}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def _get(url: str, api_key: str, timeout: float) -> tuple[int, dict | str]:
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {api_key or 'EMPTY'}"}, method="GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def step_list_models(base_url: str, api_key: str, timeout: float) -> list[str]:
    """第一步：``GET /models``。后端探测 provider 用的就是这个接口。"""
    print(f"[1/3] GET {base_url}/models")
    try:
        status, body = _get(f"{base_url}/models", api_key, timeout)
    except Exception as exc:  # noqa: BLE001
        print(f"      ✗ 连不上：{type(exc).__name__}: {exc}")
        print("      → 可能原因：隧道没建 / 服务没起 / 端口写错 / 被平台安全组挡了")
        return []
    if status != 200:
        print(f"      ✗ HTTP {status}：{str(body)[:300]}")
        return []

    ids = [m.get("id", "") for m in (body.get("data") or [])]
    print(f"      ✓ 200，可用模型 {len(ids)} 个：")
    for mid in ids:
        print(f"          · {mid}")
    if not ids:
        print("      ⚠ 列表为空：后端会把该端点判为「不可用」，请检查服务是否真在推理态")
    return ids


def step_chat(
    base_url: str, api_key: str, model: str, timeout: float, with_thinking_kwarg: bool
) -> tuple[bool, str]:
    """第二步：``POST /chat/completions``，报文与 ``backend/llm/client.py`` 一致。"""
    flag = "带 chat_template_kwargs" if with_thinking_kwarg else "不带 chat_template_kwargs"
    print(f"[2/3] POST {base_url}/chat/completions  model={model}  ({flag})")
    payload: dict = {
        "model": model,
        "messages": [{"role": "user", "content": "用一句话说明《劳动合同法》第十九条讲的是什么。"}],
        "temperature": 0.2,
        "max_tokens": 256,
        "stream": False,
    }
    if with_thinking_kwarg:
        # 后端在 LLM_ENABLE_THINKING=false 时**无条件**发这个参数（为 Qwen3 关思考）。
        # Qwen2.5 的 chat template 里没有这个变量，多数 SGLang 版本会忽略，
        # 但个别版本会因此 400 —— 所以本脚本把它当变量单独试一次。
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    started = time.perf_counter()
    try:
        status, body = _post(f"{base_url}/chat/completions", payload, api_key, timeout)
    except Exception as exc:  # noqa: BLE001
        print(f"      ✗ 请求失败：{type(exc).__name__}: {exc}")
        return False, ""
    elapsed = int((time.perf_counter() - started) * 1000)

    if status != 200:
        print(f"      ✗ HTTP {status}（{elapsed}ms）：{str(body)[:400]}")
        return False, ""
    choices = (body or {}).get("choices") or []
    text = (choices[0]["message"]["content"] if choices else "") or ""
    usage = (body or {}).get("usage") or {}
    print(f"      ✓ 200（{elapsed}ms，usage={usage}）")
    print(f"      模型答：{text.strip()[:160]}")
    if not text.strip():
        print("      ⚠ 返回空文本：注意 Qwen3 系若开了思考模式，正文会落进 reasoning_content")
    return True, text


def step_vision(base_url: str, api_key: str, model: str, image: str, timeout: float) -> bool:
    """第三步（可选）：图片理解，报文与 ``backend/parser/vision/client.py`` 一致。"""
    path = Path(image)
    print(f"[3/3] POST {base_url}/chat/completions  model={model}  (image={path.name})")
    if not path.exists():
        print("      ✗ 图片文件不存在")
        return False
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if mime not in ("image/jpeg", "image/png", "image/bmp", "image/tiff", "image/webp"):
        # 后端按后缀映射 MIME；后缀不认识时兜底 image/png，这里保持一致
        mime = "image/png"
    b64 = base64.b64encode(path.read_bytes()).decode()
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "只输出一个 JSON，概括这张图的内容。"},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }
        ],
        "temperature": 0.1,
        "max_tokens": 512,
    }
    started = time.perf_counter()
    try:
        status, body = _post(f"{base_url}/chat/completions", payload, api_key, timeout)
    except Exception as exc:  # noqa: BLE001
        print(f"      ✗ 请求失败：{type(exc).__name__}: {exc}")
        return False
    elapsed = int((time.perf_counter() - started) * 1000)
    if status != 200:
        print(f"      ✗ HTTP {status}（{elapsed}ms）：{str(body)[:400]}")
        return False
    choices = (body or {}).get("choices") or []
    text = (choices[0]["message"]["content"] if choices else "") or ""
    print(f"      ✓ 200（{elapsed}ms）")
    print(f"      识别答：{text.strip()[:200]}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="探测 OpenAI 兼容端点（SGLang / vLLM / 网关）")
    ap.add_argument("--base-url", required=True, help="必须以 /v1 结尾，例如 http://127.0.0.1:30000/v1")
    ap.add_argument("--api-key", default="EMPTY", help="SGLang 未设 --api-key 时用 EMPTY")
    ap.add_argument("--model", default="", help="省略则只列模型，不跑对话")
    ap.add_argument("--image", default="", help="给一张图则额外验证多模态通道")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = ap.parse_args()

    base_url = args.base_url.rstrip("/")
    if not base_url.endswith("/v1"):
        print(f"⚠ base_url 没以 /v1 结尾（当前 {base_url}）——后端的 URL 是直接拼接的，")
        print("  少了 /v1 会请求到 /models 而不是 /v1/models。除非你的网关确实不需要，否则请补上。\n")

    ok = True
    ids = step_list_models(base_url, args.api_key, args.timeout)
    if not ids:
        return 1

    if args.model and args.model not in ids:
        # 这是最高频的坑：SGLang 的 model id 是 --served-model-name，没设就是模型路径全名。
        print(f"      ⚠ 你指定的 model「{args.model}」不在上面的列表里。")
        print(f"        后端的 LLM_MODEL / VLM_MODEL 必须**逐字符等于**列表里的 id，")
        print(f"        否则会 400 model not found。建议直接改成：{ids[0]}")
        print()

    if args.model:
        ok, _ = step_chat(base_url, args.api_key, args.model, args.timeout, with_thinking_kwarg=True)
        if not ok:
            print("      ↻ 换一种报文重试（去掉 chat_template_kwargs）…")
            ok, _ = step_chat(base_url, args.api_key, args.model, args.timeout, with_thinking_kwarg=False)
            if ok:
                print("\n结论：该服务不认 chat_template_kwargs → 把 .env 的 LLM_ENABLE_THINKING 设为 true，")
                print("      后端就不会再发这个参数（代价是 Qwen3 会开思考链，正文可能落进 reasoning_content）。")
    else:
        print("[2/3] 跳过对话（未指定 --model）")

    if args.image:
        ok = step_vision(base_url, args.api_key, args.model or (ids[0] if ids else ""),
                         args.image, args.timeout) and ok
    else:
        print("[3/3] 跳过多模态（未指定 --image）")

    print("\n" + ("全部通过 —— 把上面的 base_url / model 填进 .env 即可。" if ok else "有失败项，见上方诊断。"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
