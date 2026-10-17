"""Embedding 客户端：三级降级。

1. **远程服务**（生产）：SGLang / vLLM 部署 BGE / Qwen-Embedding，OpenAI 兼容 ``/embeddings``；
2. **本地 sentence-transformers**（开发 / 单机私有化）：加载本地 BGE 模型目录；
3. **确定性 hashing 向量**（兜底）：字符 n-gram + 分词 的哈希投影，L2 归一化。

第 3 级的作用是保证「链路永远可跑、维度永远一致」，但语义质量有限，
所以 :meth:`EmbeddingClient.status` / ``/health`` 会明确标出当前生效的是哪一级，
避免出现「以为在用 BGE，其实在用哈希」的静默失真。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
from pathlib import Path
from typing import Any

import httpx

from backend.app.config import PROJECT_ROOT, settings

logger = logging.getLogger("legal_agent.rag.embedding")

DEFAULT_LOCAL_MODEL_DIRS = [
    PROJECT_ROOT / "data" / "models" / "bge-small-zh-v1.5",
    PROJECT_ROOT / "data" / "models" / "bge-base-zh-v1.5",
]

# BGE 系列官方推荐：检索类查询需要加指令前缀，文档侧不加
BGE_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


class BaseEmbedder:
    name = "base"
    dim = 0

    def encode(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:  # pragma: no cover
        raise NotImplementedError


class RemoteEmbedder(BaseEmbedder):
    """OpenAI 兼容 /embeddings。"""

    name = "remote"

    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or "EMPTY"
        self.model = model
        self.timeout = timeout
        self.dim = 0

    def probe(self) -> bool:
        """探活：**失败重试一次**。

        为什么不能只探一次：远程网关偶发抖动（真机见过 WinError 10051、
        偶发 5xx）时单次探测失败 → 静默降级成 hash 伪向量。
        hash 的维度被配成与真模型一致（1024），Milvus **不会**报维度不匹配，
        于是"拿垃圾向量去检索语义索引"这件事没有任何报错，
        表现为"检索突然变差且查不出原因"。抖动重试一次就能挡掉绝大多数误判。
        """
        for attempt in (1, 2):
            try:
                resp = httpx.get(
                    f"{self.base_url}/models",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    timeout=4.0 * attempt,
                )
                if resp.status_code != 200:
                    continue
                vec = self.encode(["连通性测试"], is_query=True)
                self.dim = len(vec[0]) if vec else 0
                if self.dim > 0:
                    return True
            except Exception:  # noqa: BLE001
                continue
        logger.warning("远程 embedding 探测失败（已重试一次）：%s %s",
                       self.base_url, self.model)
        return False

    def encode(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), settings.EMBEDDING_BATCH):
            batch = texts[i : i + settings.EMBEDDING_BATCH]
            resp = httpx.post(
                f"{self.base_url}/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={"model": self.model, "input": batch},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = sorted(resp.json()["data"], key=lambda d: d["index"])
            out.extend([d["embedding"] for d in data])
        if out:
            self.dim = len(out[0])
        return out


class LocalEmbedder(BaseEmbedder):
    """本地 sentence-transformers（BGE 中文）。"""

    name = "local"

    def __init__(self, model_path: str, device: str = "cpu") -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_path, device=device)
        self._model_path = model_path
        self.dim = int(self._model.get_sentence_embedding_dimension())
        self.model_name = Path(model_path).name

    def encode(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
        payload = [
            (BGE_QUERY_INSTRUCTION + t) if is_query else t for t in texts
        ]
        vecs = self._model.encode(
            payload, batch_size=settings.EMBEDDING_BATCH, normalize_embeddings=True, show_progress_bar=False
        )
        return [v.tolist() for v in vecs]

    def probe(self) -> bool:
        try:
            return len(self.encode(["测试"], is_query=True)[0]) == self.dim
        except Exception:  # noqa: BLE001
            return False


class HashEmbedder(BaseEmbedder):
    """确定性 hashing 向量兜底。

    用「jieba 词 + 字符 2/3-gram」做特征，按 blake2b 哈希投影到固定维度并做子线性缩放。
    不依赖任何模型与网络，保证链路可跑、维度稳定、可复现。
    语义泛化能力弱于 BGE，但法律场景里专业术语与条文号本身也是强特征，仍可用。
    """

    name = "hash"

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim
        try:
            import jieba  # noqa: F401

            self._jieba_ok = True
        except Exception:  # noqa: BLE001
            self._jieba_ok = False

    def _features(self, text: str) -> list[str]:
        text = (text or "").strip()
        if not text:
            return []
        feats: list[str] = []
        if self._jieba_ok:
            import jieba

            tokens = [t.strip() for t in jieba.lcut(text) if len(t.strip()) > 1]
            feats.extend(tokens[:400])
        # 字符 n-gram 补足（对条文号、案号这类精确串很有效）
        compact = "".join(text.split())
        for n in (2, 3):
            feats.extend(compact[i : i + n] for i in range(0, max(0, len(compact) - n + 1), n))
        return feats[:1200]

    def encode(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
        result: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            for feat in self._features(text):
                h = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
                idx = int.from_bytes(h[:4], "little") % self.dim
                sign = 1.0 if h[4] & 1 else -1.0
                vec[idx] += sign * (1.0 + math.log1p(len(feat)))
            norm = math.sqrt(sum(v * v for v in vec))
            if norm > 0:
                vec = [v / norm for v in vec]
            result.append(vec)
        return result

    def probe(self) -> bool:
        return True


class EmbeddingClient:
    """统一入口，线程安全地懒加载实际生效的 embedder。"""

    def __init__(self) -> None:
        self._impl: BaseEmbedder | None = None
        self._lock = asyncio.Lock()
        self.batches = 0
        self.total_texts = 0

    def _candidates(self) -> list[Any]:
        """返回候选实现；元组 ("local", 路径) 表示需要线程内加载的本地模型。"""
        mode = settings.EMBEDDING_PROVIDER
        out: list[Any] = []
        if mode in ("auto", "openai"):
            out.append(
                RemoteEmbedder(settings.EMBEDDING_BASE_URL, settings.EMBEDDING_API_KEY, settings.EMBEDDING_MODEL)
            )
        if mode in ("auto", "local"):
            configured_path = settings.EMBEDDING_LOCAL_MODEL_PATH.strip()
            model_dirs = (
                [Path(configured_path)]
                if configured_path
                else DEFAULT_LOCAL_MODEL_DIRS
            )
            for d in model_dirs:
                if d.exists():
                    out.append(("local", str(d)))
                    break
            else:
                logger.warning("未找到本地 embedding 模型目录：%s", configured_path or DEFAULT_LOCAL_MODEL_DIRS)
        # hashing 兜底始终最后候选，保证链路永不中断
        out.append(HashEmbedder(settings.EMBEDDING_DIM))
        return out

    def _build_local(self, path: str) -> BaseEmbedder | None:
        try:
            return LocalEmbedder(path)
        except Exception as exc:  # noqa: BLE001
            logger.info("本地 embedding 模型加载失败（%s）：%s", path, exc)
            return None

    async def ensure(self) -> BaseEmbedder:
        if self._impl is not None:
            return self._impl
        async with self._lock:
            if self._impl is not None:
                return self._impl
            for cand in self._candidates():
                if isinstance(cand, tuple):
                    impl = await asyncio.to_thread(self._build_local, cand[1])
                    if impl is None:
                        continue
                else:
                    impl = cand
                    try:
                        ok = await asyncio.to_thread(impl.probe)
                    except Exception:  # noqa: BLE001
                        ok = False
                    if not ok:
                        logger.info("embedding 候选不可用：%s", impl.name)
                        continue
                self._impl = impl
                logger.info("Embedding 已启用：%s (dim=%d)", impl.name, impl.dim)
                break
            if self._impl is None:
                self._impl = HashEmbedder(settings.EMBEDDING_DIM)
                logger.warning("未找到可用 embedding，降级为 hashing 向量（语义质量有限）")
        return self._impl

    async def encode(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
        impl = await self.ensure()
        if not texts:
            return []
        out = await asyncio.to_thread(impl.encode, texts, is_query=is_query)
        self.batches += 1
        self.total_texts += len(texts)
        return out

    async def encode_one(self, text: str, *, is_query: bool = False) -> list[float]:
        vecs = await self.encode([text], is_query=is_query)
        # self.dim 是 async 方法：[0.0] * coroutine 会抛 TypeError，
        # 且该异常发生在 hybrid.run_vector 的 try 内 → 被吞成"向量检索不可用"，
        # 真正的成因（TypeError）反而看不见。
        return vecs[0] if vecs else [0.0] * (await self.dim)

    async def dim(self) -> int:
        return (await self.ensure()).dim

    async def status(self) -> dict[str, Any]:
        impl = await self.ensure()
        return {
            "backend": impl.name,
            "model": getattr(impl, "model_name", settings.EMBEDDING_MODEL),
            "dim": impl.dim,
            "degraded": impl.name == "hash",
            "batches": self.batches,
            "texts": self.total_texts,
        }


embedding_client = EmbeddingClient()


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def ensure_hf_endpoint() -> None:
    """国内环境默认走 hf-mirror，避免 sentence-transformers 直连超时。"""
    if not os.environ.get("HF_ENDPOINT"):
        os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
