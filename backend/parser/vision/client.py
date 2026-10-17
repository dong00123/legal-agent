"""VLM 客户端（Qwen-VL 等 OpenAI 兼容多模态服务）。

生产：SGLang 上部署 Qwen-VL，与文本模型同网关。
降级：未配置或不可达时返回结构化占位说明，并在结果里标注 ``degraded=True``，
     由流水线写入 ``evidence_gap``——不让调用方以为识别成功了。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from backend.app.config import settings
from backend.parser.vision.prompts import PROMPTS, pick_prompt

logger = logging.getLogger("legal_agent.parser.vision")

# 中文文字 + 中文标点 + 全角符号。用途见 normalize_scan_text：
# 夹在这类字符之间的空格是噪声（中文不用空格分词），可以安全删掉。
# 区间：CJK 符号与标点(3000-303F)、扩展A(3400-4DBF)、基本区(4E00-9FFF)、
#       兼容汉字(F900-FAFF)、全角形式(FF01-FF65)。
_CJK_ISH = "\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff01-\uff65"


@dataclass
class VisionResult:
    kind: str = "generic"
    data: dict = field(default_factory=dict)
    semantic: str = ""
    degraded: bool = False
    error: str = ""
    elapsed_ms: int = 0


class VisionClient:
    # 同一张图的识别结果缓存：key = (图片 sha256, prompt 类别)。
    # ⛔ 为什么要缓存：temperature=0 + 归一化之后，同一张图多次识别**仍可能**差一两个字
    #    （VLM 对模糊字形/占位符个数的"估读"不是完全确定的）。用户上传同一份合同两次，
    #    看到的字数不一样，会直接怀疑系统不可靠 —— 而同一份文件的"正确答案"只有一份。
    #    按内容哈希缓存后：同一张图（无论改什么文件名、传多少次）永远返回同一份转写，
    #    字数自然严格一致；省掉的还是重复的 8K-token 转写开销。
    #    只有**成功**结果进缓存；失败/降级不缓存，下次还有机会成功。
    _CACHE_MAX = 128

    def __init__(self) -> None:
        self._available: bool | None = None
        self._cache: OrderedDict[tuple[str, str], VisionResult] = OrderedDict()

    async def probe(self) -> bool:
        if not settings.VLM_ENABLED:
            self._available = False
            return False
        if self._available is not None:
            return self._available
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                resp = await client.get(
                    f"{settings.VLM_BASE_URL.rstrip('/')}/models",
                    headers={"Authorization": f"Bearer {settings.VLM_API_KEY or 'EMPTY'}"},
                )
                self._available = resp.status_code == 200
        except Exception:  # noqa: BLE001
            self._available = False
        if not self._available:
            logger.info("VLM 不可用（%s），多模态识别将走降级路径", settings.VLM_BASE_URL)
        return self._available

    async def describe_image(self, image_path: str, hint: str = "", kind_override: str = "") -> VisionResult:
        """识别单张图片，返回语义描述（用于占位符回插）。

        ``kind_override``：强制指定 prompt 类别（如 ``document_scan`` 整页转写），
        用于「独立上传的合同扫描件」这类 hint（文件名 1.1.jpg）匹配不到关键词的场景。
        """
        kind, prompt = pick_prompt(hint)
        if kind_override and kind_override in PROMPTS:
            kind, prompt = kind_override, PROMPTS[kind_override]
        path = Path(image_path)
        if not path.exists():
            return VisionResult(kind=kind, degraded=True, error="图片文件不存在")

        # 内容哈希缓存：同一张图（字节级相同）直接复用上次的识别结果，
        # 保证「同一份文件每次上传得到的正文与字数完全一致」。
        try:
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            sha = ""
        cache_key = (sha, kind) if sha else None
        if cache_key is not None and cache_key in self._cache:
            self._cache.move_to_end(cache_key)
            return self._cache[cache_key]

        if not await self.probe():
            return VisionResult(
                kind=kind,
                degraded=True,
                error="VLM 未启用或不可达",
                semantic=f"图像内容未识别（{kind}，多模态服务不可用）",
            )

        try:
            # data URL 的 MIME 要跟真实后缀一致：整页扫描件多为 jpg，
            # 一律标成 image/png 有些网关会直接 400。
            suffix = path.suffix.lower()
            mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                    ".bmp": "image/bmp", ".tiff": "image/tiff", ".webp": "image/webp"}.get(suffix, "image/png")
            b64 = base64.b64encode(path.read_bytes()).decode()
            # 整页转写要的是完整正文，1024 tokens 会**截断**（截断的合同比没识别更危险）；
            # 其余「小图元素识别」保持 1024。
            max_tokens = 8192 if kind == "document_scan" else 1024
            payload = {
                "model": settings.VLM_MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{b64}"},
                            },
                        ],
                    }
                ],
                # 转写用贪心解码（VLM_TEMPERATURE 默认 0）：转写要可复现，
                # 采样随机这一层没必要留。
                # ⚠️ 但**别指望它解决"字数每次不一样"** —— 实测过，temperature=0 一样会漂，
                #    真正的来源是填空线长度，靠 normalize_scan_text 归一化（见其 docstring），
                #    以及 describe_image 的内容哈希缓存（同一张图只识别一次）。
                "temperature": settings.VLM_TEMPERATURE,
                # 固定随机种子：SGLang/vLLM 等 OpenAI 兼容网关支持该字段，
                # 即使贪心解码下某些实现仍有数值噪声，固定 seed 也能压住；
                # 不支持该字段的网关会忽略它，无副作用。
                "seed": 42,
                "max_tokens": max_tokens,
            }
            async with httpx.AsyncClient(timeout=settings.VLM_TIMEOUT) as client:
                resp = await client.post(
                    f"{settings.VLM_BASE_URL.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.VLM_API_KEY or 'EMPTY'}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]

            data = _safe_json(content)
            result = VisionResult(
                kind=data.get("doc_kind") or kind,
                data=data,
                semantic=summarize(data, kind),
            )
            if cache_key is not None:
                self._cache[cache_key] = result
                self._cache.move_to_end(cache_key)
                while len(self._cache) > self._CACHE_MAX:
                    self._cache.popitem(last=False)
            return result
        except Exception as exc:  # noqa: BLE001
            logger.warning("VLM 识别失败 %s：%s", path.name, exc)
            return VisionResult(kind=kind, degraded=True, error=str(exc),
                                semantic=f"图像内容未识别（{kind}，识别失败）")


def normalize_scan_text(text: str) -> str:
    """把整页转写结果里的「非正文噪声」归一化，让同一张图的转写稳定下来。

    **为什么要做**（都是实测，不是推测）：同一张图连跑多次、且 ``temperature=0``
    （已排除采样随机），字数依然会漂。逐字比对下来，差异只有两类，且都**不承载正文信息**：

    1. **填空线的长度**：合同里的填空线是排版出来的一条连续线条，模型只能"估"它多长
       ⇒ 某处 ``_`` 多两根或少两根（实测 4 次：800 / 798 / 798 / 798，唯一差异就是这一处）；
    2. **中文字之间被插进空格**：如 ``劳动合 同 书``（实测两次跑出了不同的结果）。
       中文排版本来就没有词间空格，这些空格是噪声。

    前端显示的「（xxx 字）」现在是后端 ``count_chars()`` 算的**不计空白**字数
    （见 ``api/v1/chat.py``），所以噪声已经不再影响那个数字；
    这里归一化是为了让**送去审查的正文本身**也不夹带噪声 —— 两件事都做，才既稳又不脏。

    **规矩：只动不含正文的字符，正文字一个字不动。**

    - 下划线串（半角 ``_`` / 全角 ``＿``）→ 固定 4 根（"此处待填"这个含义不变）；
    - 连续空格 / 全角空格 / Tab → 一个；
    - **两侧都是中文（含中文标点）的空格 → 直接删掉**（``劳动合 同 书`` → ``劳动合同书``）；
      中文与数字/英文之间的空格**保留**（``月 5000 元``、表格的 `` | `` 那种分隔有意义）；
    - 连续 3 个以上换行 → 2 个。**换行整体保留**（prompt 要求保留段落顺序，
      条款编号与段落切分都靠它）。

    ⛔ 故意**不**压 ``□``（prompt 约定的"看不清"占位）：它的个数虽然也是估的，
    但「这一页有多少字看不清」对律师是有用信号，压掉等于把风险信息抹平。
    """
    if not text:
        return ""
    out = re.sub(r"[_＿]{2,}", "____", text)
    out = re.sub(r"[ \t\u3000]{2,}", " ", out)
    # 中文（含中文标点、全角符号）之间的空格是噪声：中文不用空格分词
    out = re.sub(rf"(?<=[{_CJK_ISH}])[ \t]+(?=[{_CJK_ISH}])", "", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _safe_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[-1] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {}
    return {}


def summarize(data: dict, kind: str) -> str:
    """把 VLM 的结构化输出压成一行语义描述，用于回插到原文占位符处。"""
    if not data:
        return ""
    # 整页扫描件走的是「全文转写」：直接回原始正文，不做任何概括压缩 ——
    # 但要过一遍 normalize_scan_text（见其 docstring：填空线的长度是**猜**出来的，
    # 不归一化就会出现「同一张图每次字数都不一样」）。
    if kind == "document_scan" or data.get("raw_text"):
        return normalize_scan_text(data.get("raw_text") or "")
    if kind == "business_license" or data.get("company_name"):
        bits = [data.get("company_name", "")]
        for k, label in (
            ("company_type", ""),
            ("legal_rep", "法定代表人"),
            ("registered_capital", "注册资本"),
            ("credit_code", "统一社会信用代码"),
        ):
            if data.get(k):
                bits.append(f"{label}{data[k]}" if label else str(data[k]))
        return "营业执照扫描件，" + "，".join(b for b in bits if b)

    if kind == "seal_or_signature" or data.get("seal_text"):
        bits = []
        if data.get("has_seal"):
            bits.append(f"有印章：{data.get('seal_text', '文字无法辨认')}")
        if data.get("is_cross_page"):
            bits.append("骑缝章")
        if data.get("has_signature"):
            bits.append(f"有签字：{data.get('signature_name') or '姓名无法辨认'}")
        return "、".join(bits) or "未见明显印章或签字"

    if kind == "handwriting" or data.get("handwriting"):
        return f"手写批注：{data.get('handwriting', '')}"

    if kind == "structure_diagram" or data.get("nodes"):
        return data.get("summary") or f"结构图，含 {len(data.get('nodes') or [])} 个主体"

    if kind == "screenshot":
        return data.get("summary") or "证据截图"

    return data.get("summary") or "图像已识别"


vision_client = VisionClient()
