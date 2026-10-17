"""LLM 客户端：多provider 自动探测 + 明确降级。

生产形态：H20 + SGLang（OpenAI 兼容接口）。
兼容形态：vLLM / DeepSeek / 任意 OpenAI 兼容网关 / 本机 Ollama。
降级形态：确定性规则引擎（``rule_engine``）——无外部模型时链路仍完整可跑，
        且因为不生成、只抽取，天然满足「无来源不出引用」。

所有调用都记录 provider / 耗时 / 重试次数，供 /health 与统计接口使用。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from backend.app.config import settings

logger = logging.getLogger("legal_agent.llm")


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    elapsed_ms: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    degraded: bool = False
    error: str = ""


class BaseProvider:
    name = "base"

    def __init__(self, model: str) -> None:
        self.model = model

    async def probe(self) -> bool:  # pragma: no cover - 抽象
        raise NotImplementedError

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> LLMResult:  # pragma: no cover
        raise NotImplementedError


class OpenAICompatProvider(BaseProvider):
    """SGLang / vLLM / DeepSeek / 任意 OpenAI 兼容网关。"""

    name = "openai"

    def __init__(self, base_url: str, api_key: str, model: str, timeout: int, thinking: bool = False) -> None:
        super().__init__(model)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or "EMPTY"
        self.timeout = timeout
        self.thinking = thinking

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def probe(self) -> bool:
        url = f"{self.base_url}/models"
        # ⚠ 4 秒对**远端网关**太紧（原来是硬编码 4.0）：后端刚启动时进程在加载向量模型，
        # 加上网络往返，探针很容易超时 → openai/ollama 全判不可用 → 整个进程退回规则引擎。
        # 现在有 LLM_REPROBE_INTERVAL 兜底自愈，但**首个请求仍会降级**，所以把超时放开一点。
        timeout = float(getattr(settings, "LLM_PROBE_TIMEOUT", 8) or 8)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url, headers=self._headers())
                return resp.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> LLMResult:
        url = f"{self.base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": kwargs.get("temperature", settings.LLM_TEMPERATURE),
            "max_tokens": kwargs.get("max_tokens", settings.LLM_MAX_TOKENS),
            "stream": kwargs.get("stream", getattr(settings, "LLM_STREAM", False)),
        }
        # SGLang / vLLM 的思维链开关（Qwen3 系列），关掉以压低首 token 延迟
        if self.thinking is False:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if kwargs.get("json_mode"):
            payload["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            # 间歇性失败要**当场重试一次**：真机网关对同一 key 时而 403
            # （AccessDenied.Unpurchased）、时而 200，间隔秒级 —— 大概率是
            # 网关侧限流/抖动。不重试的后果是"这次任务整个退回规则引擎"，
            # 报告变成模板、引用全空，而用户只看到结果变差，查不到原因。
            # 只重试 1 次、只重试"值得重试"的状态码，避免拖长必然失败的请求。
            last_exc: Exception | None = None
            for attempt in (1, 2):
                try:
                    resp = await client.post(url, headers=self._headers(), json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                    last_exc = None
                    break
                except httpx.HTTPStatusError as exc:
                    code = exc.response.status_code
                    if code not in (403, 429, 500, 502, 503, 504) or attempt == 2:
                        raise
                    last_exc = exc
                    await asyncio.sleep(1.2)
                except httpx.HTTPError as exc:  # 网络抖动同样救一次
                    if attempt == 2:
                        raise
                    last_exc = exc
                    await asyncio.sleep(1.2)
            if last_exc is not None:  # 理论不可达，保险起见
                raise last_exc
        elapsed = int((time.perf_counter() - started) * 1000)

        choices = data.get("choices") or []
        text = choices[0]["message"]["content"] if choices else ""
        return LLMResult(
            text=text,
            provider=self.name,
            model=self.model,
            elapsed_ms=elapsed,
            usage=data.get("usage") or {},
        )


class OllamaProvider(BaseProvider):
    """本机 Ollama，开发期常用。"""

    name = "ollama"

    def __init__(self, base_url: str, model: str, timeout: int) -> None:
        super().__init__(model)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def probe(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                resp = await client.get(f"{self.base_url}/api/tags")
                if resp.status_code != 200:
                    return False
                models = resp.json().get("models") or []
                # 没有任何已拉取模型时视为不可用（否则会在调用时报 404）
                return len(models) > 0
        except Exception:  # noqa: BLE001
            return False

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> LLMResult:
        url = f"{self.base_url}/api/chat"
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": kwargs.get("temperature", settings.LLM_TEMPERATURE),
                "num_predict": kwargs.get("max_tokens", settings.LLM_MAX_TOKENS),
            },
        }
        if kwargs.get("json_mode"):
            payload["format"] = "json"
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        elapsed = int((time.perf_counter() - started) * 1000)
        return LLMResult(
            text=(data.get("message") or {}).get("content", ""),
            provider=self.name,
            model=self.model,
            elapsed_ms=elapsed,
            usage={
                "prompt_tokens": data.get("prompt_eval_count"),
                "completion_tokens": data.get("eval_count"),
            },
        )


class RuleProvider(BaseProvider):
    """确定性规则引擎兜底。

    明确说明：这不是"假模型"，而是降级路径——不生成、只从检索证据中抽取与拼装，
    因此不会编造条文号或案号。用于：无外部推理服务的开发机、CI、断网演示。
    生产环境应配置 SGLang，此时本 provider 不会被启用。
    """

    name = "rule"

    def __init__(self) -> None:
        super().__init__("rule-based-legal-engine")

    async def probe(self) -> bool:
        return True

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> LLMResult:
        from backend.llm import rule_engine

        task = kwargs.get("task", "general")
        payload = kwargs.get("payload") or {}
        started = time.perf_counter()
        text = await asyncio.to_thread(rule_engine.run_task, task, messages, payload)
        return LLMResult(
            text=text,
            provider=self.name,
            model=self.model,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            degraded=True,
        )


class LLMClient:
    """统一入口。首次调用时按序探测可用 provider，并缓存结果。"""

    def __init__(self) -> None:
        self._provider: BaseProvider | None = None
        self._selected = ""
        self._lock = asyncio.Lock()
        self.calls = 0
        self.total_ms = 0
        self.degraded_calls = 0
        self.last_error = ""
        self._last_probe_at = 0.0

    # ------------------------------------------------------------- provider
    def _candidates(self) -> list[BaseProvider]:
        out: list[BaseProvider] = []
        mode = settings.LLM_PROVIDER
        if mode in ("auto", "openai"):
            out.append(
                OpenAICompatProvider(
                    settings.LLM_BASE_URL,
                    settings.LLM_API_KEY,
                    settings.LLM_MODEL,
                    settings.LLM_TIMEOUT,
                    settings.LLM_ENABLE_THINKING,
                )
            )
        if mode in ("auto", "ollama"):
            out.append(OllamaProvider(settings.OLLAMA_BASE_URL, settings.OLLAMA_MODEL, settings.LLM_TIMEOUT))
        if mode == "mock":
            out = []
        out.append(RuleProvider())
        return out

    async def ensure_provider(self) -> BaseProvider:
        if self._provider is not None:
            return self._provider
        async with self._lock:
            if self._provider is not None:
                return self._provider
            self._last_probe_at = time.monotonic()
            for cand in self._candidates():
                ok = False
                try:
                    ok = await cand.probe()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("provider %s 探测异常：%s", cand.name, exc)
                if ok:
                    self._provider = cand
                    self._selected = cand.name
                    logger.info("LLM provider 已选定：%s (%s)", cand.name, cand.model)
                    break
                logger.info("LLM provider 不可用，尝试下一个：%s", cand.name)
            if self._provider is None:
                self._provider = RuleProvider()
                self._selected = "rule"
                logger.warning("未探测到可用模型服务，LLM 降级为确定性规则引擎")
        return self._provider

    async def reset(self) -> None:
        """配置变更后重新探测。"""
        self._provider = None
        self._selected = ""
        await self.ensure_provider()

    async def reprobe(self) -> None:
        """公共入口：若当前钉在规则引擎兜底上，按间隔重探真模型。

        供"调用前先判断模型是否可用"的路径使用（如直答）——这类路径
        不一定真的会发起 chat，若不主动重探，规则引擎兜底永远不会翻身。
        """
        await self._reprobe_if_stuck_on_rule()

    async def _reprobe_if_stuck_on_rule(self) -> None:
        """被钉在"规则引擎"上时，隔一段时间重新探一次真模型。

        为什么必须做（2026-09-21 真机踩到）：provider 只在第一次调用时探测一次并缓存。
        后端**刚启动**那几秒里进程正忙着加载向量/嵌入模型，探针的 4 秒超时很容易不够，
        于是 openai 与 ollama 都判为"不可用"，直接把 RuleProvider 缓存成整个进程的默认 ——
        之后每个请求都静默走规则引擎（日志只有一句
        ``LLM provider 已选定：rule``），而且**再也不会自己恢复**。
        现象极难查：接口 200、流程正常、结果全是模板味/空值。

        规则引擎是"兜底"，不是"结论"，所以这里给它一次翻身机会：
        间隔 ``LLM_REPROBE_INTERVAL`` 秒（默认 20）重新探，探通了自动切回真模型。
        """
        if self._provider is None or self._provider.name != "rule":
            return
        interval = float(getattr(settings, "LLM_REPROBE_INTERVAL", 20) or 20)
        if time.monotonic() - self._last_probe_at < interval:
            return
        async with self._lock:
            if self._provider is None or self._provider.name != "rule":
                return
            if time.monotonic() - self._last_probe_at < interval:
                return
            self._provider = None
            self._selected = ""
        logger.info("当前停留在规则引擎兜底上，重新探测模型服务…")
        await self.ensure_provider()

    # ------------------------------------------------------------- 调用
    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        task: str = "general",
        payload: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> LLMResult:
        await self._reprobe_if_stuck_on_rule()
        provider = await self.ensure_provider()
        started = time.perf_counter()
        try:
            result = await provider.chat(messages, task=task, payload=payload or {}, **kwargs)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("provider %s 调用失败，本次降级到规则引擎：%s", provider.name, exc)
            fallback = RuleProvider()
            result = await fallback.chat(messages, task=task, payload=payload or {}, **kwargs)
            self.degraded_calls += 1
        self.calls += 1
        self.total_ms += int((time.perf_counter() - started) * 1000)
        return result

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        task: str = "general",
        payload: dict[str, Any] | None = None,
        default: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], LLMResult]:
        """要求 JSON 输出，并对模型输出做健壮解析（LLM 常带 markdown 围栏或前后废话）。"""
        result = await self.chat(messages, task=task, payload=payload, json_mode=True)
        parsed = extract_json(result.text)
        if parsed is None:
            # 模型输出了非 JSON：退化为让规则引擎给出同构结果，保证下游不崩。
            # 日志带上长度/用量/头部 —— 最常见的两种原因是「被 max_tokens 截断」
            # 和「模型带了围栏外的额外说明」，只看一句"非 JSON"根本分不清。
            logger.warning(
                "模型返回非 JSON（task=%s, len=%d, usage=%s），头部：%s",
                task, len(result.text or ""), result.usage or {}, (result.text or "")[:400],
            )
            from backend.llm import rule_engine

            text = await asyncio.to_thread(rule_engine.run_task, task, messages, payload or {})
            parsed = extract_json(text)
            result = LLMResult(
                text=text,
                provider="rule",
                model="rule-based-legal-engine",
                elapsed_ms=result.elapsed_ms,
                degraded=True,
                error="模型输出非 JSON，已降级",
            )
        return (parsed if parsed is not None else (default or {})), result

    # ------------------------------------------------------------- 状态
    def status(self) -> dict[str, Any]:
        return {
            "provider": self._selected or "未探测",
            "model": self._provider.model if self._provider else "",
            "degraded": (self._selected == "rule") or (self._provider is None),
            "calls": self.calls,
            "degraded_calls": self.degraded_calls,
            "avg_ms": int(self.total_ms / self.calls) if self.calls else 0,
            "last_error": self.last_error,
            "configured_base_url": settings.LLM_BASE_URL,
        }


# --------------------------------------------------------------------------- 工具
_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出中抠出第一个合法 JSON 对象。"""
    if not text:
        return None
    candidates: list[str] = []
    m = _JSON_FENCE.search(text)
    if m:
        candidates.append(m.group(1))
    candidates.append(text)

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for cand in candidates:
        cand = cand.strip()
        if not cand:
            continue
        try:
            data = json.loads(cand)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    return None


llm_client = LLMClient()


def review_provider() -> OpenAICompatProvider | None:
    """合同审查复审用的**第二个模型**（独立配置，与初审模型不同侧重）。

    「初审广撒网、复审严核查」的分工：初审走 ``llm_client``（``LLM_*``），
    复审走这里（``LLM_REVIEW_*``）—— 两个模型可以来自不同厂商、不同 checkpoint。

    ⚠ 返回 ``None`` 表示未配置/已关闭，调用方据此**跳过复审**而非失败：
    复审是增强项，不是硬依赖。也不复用 ``llm_client`` 的探测/规则兜底链 ——
    复审模型不可用时宁可跳过并在缺口里声明，不要把它拖进规则引擎兜底
    （那会让"有复审"变成一个假象）。
    """
    if not getattr(settings, "LLM_REVIEW_ENABLED", False):
        return None
    base = (getattr(settings, "LLM_REVIEW_BASE_URL", "") or "").strip()
    if not base:
        return None
    model = getattr(settings, "LLM_REVIEW_MODEL", "") or settings.LLM_MODEL
    return OpenAICompatProvider(
        base,
        getattr(settings, "LLM_REVIEW_API_KEY", "EMPTY") or "EMPTY",
        model,
        settings.LLM_TIMEOUT,
        settings.LLM_ENABLE_THINKING,
    )


def describe_llm() -> str:
    st = llm_client.status()
    return f"provider={st['provider']} model={st['model']} degraded={st['degraded']}"
