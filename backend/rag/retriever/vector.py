"""向量检索（Milvus）。

- 生产：Milvus standalone / 集群；
- 兜底：本地 numpy 索引（`data/index/local_vectors/`），保证没有 Milvus 也能跑通与演示；
- 维度漂移防护：embedding 模型更换会导致维度不一致，这里会检测并重建集合，
  否则 Milvus 会直接报维度错误而让人误以为是数据问题。

元数据统一以 JSON 字段 ``payload`` 存储，避免字段爆炸，也让新增元数据不需要改 schema。
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

import numpy as np

from backend.app.config import PROJECT_ROOT, settings

logger = logging.getLogger("legal_agent.rag.vector")

COLLECTIONS = ("statute", "case", "knowledge", "material", "memory")
_LOCAL_DIR = PROJECT_ROOT / "data" / "index" / "local_vectors"


class VectorStoreBase:
    name = "base"

    def upsert(self, collection: str, items: list[dict[str, Any]]) -> int:
        raise NotImplementedError

    def search(
        self, collection: str, vector: list[float], top_k: int = 10, expr: str | None = None
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def count(self, collection: str) -> int:
        raise NotImplementedError

    def drop(self, collection: str) -> None:
        raise NotImplementedError

    def status(self) -> dict[str, Any]:
        return {"backend": self.name}


# --------------------------------------------------------------------------- Milvus
class MilvusStore(VectorStoreBase):
    name = "milvus"

    def __init__(self, uri: str, token: str = "", db: str = "") -> None:
        from pymilvus import MilvusClient

        self.uri = uri
        # 先建库再连接：MilvusClient(db_name=...) 在库不存在时会直接抛
        # MilvusException(code=800, database not found)，如果直接连就会一路降级到本地索引。
        if db:
            bootstrap = MilvusClient(uri=uri, token=token or None)
            try:
                dbs = set(bootstrap.list_databases())
                if db not in dbs:
                    bootstrap.create_database(db)
                    logger.info("Milvus database 已创建：%s", db)
            finally:
                try:
                    bootstrap.close()
                except Exception:  # noqa: BLE001
                    pass

        self.client = MilvusClient(uri=uri, token=token or None, db_name=db or None)
        self._dims: dict[str, int] = {}
        self._lock = threading.Lock()
        self._loaded: set[str] = set()

    # ---------------------------------------------------------------- schema
    def _collection_name(self, collection: str) -> str:
        return f"la_{collection}"

    def ensure_collection(self, collection: str, dim: int) -> None:
        from pymilvus import DataType

        name = self._collection_name(collection)
        with self._lock:
            if self.client.has_collection(name):
                desc = self.client.describe_collection(name)
                for f in desc.get("fields", []):
                    if f.get("name") == "vector":
                        params = f.get("params") or {}
                        existing = int(params.get("dim", 0))
                        if existing and existing != dim:
                            logger.warning(
                                "集合 %s 维度不一致（现有 %d，当前 embedding %d），重建集合",
                                name, existing, dim,
                            )
                            self.client.drop_collection(name)
                            break
                else:
                    self._dims[collection] = dim
                    self._ensure_loaded(collection)
                    return

            schema = self.client.create_schema(auto_id=False, enable_dynamic_field=True)
            schema.add_field("chunk_id", DataType.VARCHAR, max_length=128, is_primary=True)
            schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dim)
            schema.add_field("text", DataType.VARCHAR, max_length=16384)
            schema.add_field("corpus", DataType.VARCHAR, max_length=32)
            schema.add_field("payload", DataType.JSON)

            index_params = self.client.prepare_index_params()
            index_params.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")

            self.client.create_collection(
                collection_name=name, schema=schema, index_params=index_params
            )
            self._dims[collection] = dim
            logger.info("Milvus 集合已创建：%s (dim=%d)", name, dim)
            self._ensure_loaded(collection)

    def _ensure_loaded(self, collection: str) -> None:
        name = self._collection_name(collection)
        if collection in self._loaded:
            return
        try:
            self.client.load_collection(name)
            self._loaded.add(collection)
        except Exception as exc:  # noqa: BLE001 - 已加载时会抛错，忽略
            logger.debug("load_collection(%s)：%s", name, exc)
            self._loaded.add(collection)

    # ---------------------------------------------------------------- 写
    def upsert(self, collection: str, items: list[dict[str, Any]]) -> int:
        if not items:
            return 0
        dim = len(items[0]["vector"])
        # 记录"这次是不是新建的集合"：新建集合写入后必须 flush 才稳定可见，
        # 否则会出现 upsert 返回成功、紧接着检索却查不到（真机上是**间歇性**的，
        # 时好时坏，最容易被误判成"缓存逻辑写错了"）。
        fresh = not self._dims.get(collection)
        self.ensure_collection(collection, dim)
        name = self._collection_name(collection)

        # 去重兜底：Milvus 对"同一批次内出现重复主键"是**整批拒绝**
        # （duplicate primary keys are not allowed in the same batch），
        # 不是跳过重复项。所以哪怕上游只漏出一个重复 chunk_id，
        # 整个 collection 的向量都会写不进去，而 BM25 那边却是成功的 ——
        # 表现出来就是"检索还能跑，但向量通道静默失效"。
        # 这里在写入前显式去重（保留最后一条），把故障面从"整批丢失"缩到"重复项被覆盖"。
        deduped: dict[str, dict[str, Any]] = {}
        duplicates = 0
        for it in items:
            cid = it["chunk_id"]
            if cid in deduped:
                duplicates += 1
            deduped[cid] = it
        if duplicates:
            logger.warning(
                "集合 %s 的写入批次存在 %d 个重复 chunk_id，已按后者覆盖去重"
                "（共 %d 条 → %d 条）",
                collection, duplicates, len(items), len(deduped),
            )

        rows = [
            {
                "chunk_id": it["chunk_id"],
                "vector": it["vector"],
                "text": (it.get("text") or "")[:16000],
                "corpus": it.get("corpus", collection)[:32],
                "payload": it.get("metadata") or {},
            }
            for it in deduped.values()
        ]

        done = 0
        batch = 200
        for i in range(0, len(rows), batch):
            chunk = rows[i : i + batch]
            try:
                self.client.upsert(collection_name=name, data=chunk)
                done += len(chunk)
            except Exception as exc:  # noqa: BLE001
                # 整批失败时退化为逐条写入：牺牲速度换取"部分成功"，
                # 否则一个坏数据点就会让这一批全部丢失。
                logger.warning("集合 %s 第 %d 批（%d 条）整体写入失败：%s；改为逐条重试",
                               collection, i // batch, len(chunk), exc)
                ok = self._upsert_one_by_one(name, chunk)
                done += ok
                if ok == 0:
                    raise
        if done:
            # 写入后**必须重新 load**：新建集合在 upsert 之前的 load 是空集合，
            # 之后写进来的数据在 Milvus 上并不会立即进入可检索状态 ——
            # 表现为"upsert 返回成功、count 却是 0、检索永远命中不了"
            # （真机踩到：语义缓存写了却查不到，且是间歇性的）。
            # 只在新集合上多一次 flush（把数据封存成可查询段）；
            # 已存在的集合不必 flush —— 否则每次批量入库都要等一次刷盘，
            # 大语料入库会被拖慢。
            if fresh:
                try:
                    self.client.flush(collection_name=name)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("flush(%s) 失败（不影响写入）：%s", name, exc)
            self._loaded.discard(collection)
            self._ensure_loaded(collection)
        return done

    def _upsert_one_by_one(self, name: str, rows: list[dict[str, Any]]) -> int:
        """逐条写入，返回成功条数；失败条目单独告警，不中断其余数据。"""
        ok = 0
        for row in rows:
            try:
                self.client.upsert(collection_name=name, data=[row])
                ok += 1
            except Exception as exc:  # noqa: BLE001
                logger.error("集合 %s 单条写入失败 chunk_id=%s：%s",
                             name, row.get("chunk_id"), exc)
        return ok

    def delete_by_source(self, collection: str, source_path: str) -> int:
        name = self._collection_name(collection)
        if not self.client.has_collection(name):
            return 0
        # Milvus 的表达式解析器把反斜杠当转义符，Windows 路径必须转义，
        # 否则报 "token recognition error at: '\' : invalid parameter"。
        literal = source_path.replace("\\", "\\\\").replace('"', '\\"')
        try:
            res = self.client.delete(
                collection_name=name, filter=f'payload["source_path"] == "{literal}"'
            )
            if isinstance(res, dict):
                return int(res.get("delete_count", 0) or 0)
            return int(getattr(res, "delete_count", 0) or 0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("按来源删除失败：%s", exc)
            return 0

    # ---------------------------------------------------------------- 读
    def search(
        self, collection: str, vector: list[float], top_k: int = 10, expr: str | None = None
    ) -> list[dict[str, Any]]:
        name = self._collection_name(collection)
        if not self.client.has_collection(name):
            return []
        self._ensure_loaded(collection)
        try:
            hits = self.client.search(
                collection_name=name,
                data=[vector],
                limit=max(1, top_k),
                output_fields=["chunk_id", "text", "corpus", "payload"],
                search_params={"metric_type": "COSINE"},
                filter=expr or "",
            )
        except Exception as exc:  # noqa: BLE001
            # 维度不匹配（embedding 模型换过）⇒ 集合里的向量是用**旧模型**算的，
            # 与当前查询向量根本不可比，留着只会让每次检索都白等 6 秒后失败
            # （真机踩过：错误每轮打 3 次 RPC，每次 6s，整个 KnowledgeNode 被拖死）。
            # 自愈：删掉旧集合返回空，下次 upsert 会按当前模型维度重建。
            if "dimension mismatch" in str(exc):
                logger.warning(
                    "集合 %s 维度与当前 embedding 模型不一致，已删除旧集合（需重新入库：%s）",
                    name, exc,
                )
                try:
                    self.client.drop_collection(name)
                    self._loaded.discard(collection)
                except Exception:  # noqa: BLE001
                    pass
                return []
            logger.warning("Milvus 检索失败：%s", exc)
            return []
        out: list[dict[str, Any]] = []
        for group in hits or []:
            for h in group:
                entity = h.get("entity") or {}
                out.append(
                    {
                        # chunk_id 必须显式取回：它是融合去重与引用溯源的唯一键。
                        # 只靠 h.get("id") 在不同 pymilvus 版本上可能拿到 None，
                        # 于是"向量通道命中"与"BM25 通道命中"无法判定为同一条，
                        # 同一条文会各占一个 top-k 名额。
                        "chunk_id": entity.get("chunk_id") or h.get("id") or "",
                        "score": float(h.get("distance", 0.0)),
                        "text": entity.get("text", ""),
                        "corpus": entity.get("corpus", collection),
                        "metadata": entity.get("payload") or {},
                        "retriever": "vector",
                    }
                )
        return out

    def count(self, collection: str) -> int:
        name = self._collection_name(collection)
        if not self.client.has_collection(name):
            return 0
        try:
            res = self.client.query(collection_name=name, filter="", output_fields=["count(*)"])
            return int(res[0].get("count(*)", 0)) if res else 0
        except Exception:  # noqa: BLE001
            return 0

    def drop(self, collection: str) -> None:
        name = self._collection_name(collection)
        if self.client.has_collection(name):
            self.client.drop_collection(name)
        self._loaded.discard(collection)

    def status(self) -> dict[str, Any]:
        counts = {c: self.count(c) for c in COLLECTIONS}
        return {"backend": "milvus", "uri": self.uri, "collections": counts}


# --------------------------------------------------------------------------- 本地
class LocalVectorStore(VectorStoreBase):
    """numpy 兜底向量库：内存矩阵 + 磁盘持久化（npz + jsonl）。

    适用于没有 Milvus 的开发机与离线演示。数据量到十万级时性能会下降，
    所以它会明确把自己标记为 ``fallback``，在 /health 里可见。
    """

    name = "local"

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = Path(base_dir or _LOCAL_DIR)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _paths(self, collection: str) -> tuple[Path, Path]:
        return self.base_dir / f"{collection}.npz", self.base_dir / f"{collection}.jsonl"

    def _load(self, collection: str) -> dict[str, Any]:
        if collection in self._data:
            return self._data[collection]
        npz_path, meta_path = self._paths(collection)
        store: dict[str, Any] = {"vectors": np.zeros((0, 0), dtype=np.float32), "ids": [], "rows": []}
        if npz_path.exists() and meta_path.exists():
            try:
                arr = np.load(npz_path)
                store["vectors"] = arr["vectors"].astype(np.float32)
                ids = [line for line in meta_path.read_text(encoding="utf-8").splitlines() if line.strip()]
                store["rows"] = [json.loads(x) for x in ids]
                store["ids"] = [r["chunk_id"] for r in store["rows"]]
            except Exception as exc:  # noqa: BLE001
                logger.warning("本地向量索引加载失败（%s），将重建：%s", collection, exc)
        self._data[collection] = store
        return store

    def _persist(self, collection: str) -> None:
        store = self._data[collection]
        npz_path, meta_path = self._paths(collection)
        np.savez_compressed(npz_path, vectors=store["vectors"])
        with open(meta_path, "w", encoding="utf-8") as f:
            for row in store["rows"]:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def upsert(self, collection: str, items: list[dict[str, Any]]) -> int:
        if not items:
            return 0
        with self._lock:
            store = self._load(collection)
            dim = len(items[0]["vector"])
            if store["vectors"].size == 0:
                store["vectors"] = np.zeros((0, dim), dtype=np.float32)
            elif store["vectors"].shape[1] != dim:
                logger.warning(
                    "本地向量维度不一致（现有 %d，当前 %d），重建索引",
                    store["vectors"].shape[1], dim,
                )
                store["vectors"] = np.zeros((0, dim), dtype=np.float32)
                store["ids"], store["rows"] = [], []

            index_map = {cid: i for i, cid in enumerate(store["ids"])}
            new_vecs: list[list[float]] = []
            new_rows: list[dict[str, Any]] = []
            for it in items:
                row = {
                    "chunk_id": it["chunk_id"],
                    "text": it.get("text", ""),
                    "corpus": it.get("corpus", collection),
                    "metadata": it.get("metadata") or {},
                }
                if it["chunk_id"] in index_map:
                    idx = index_map[it["chunk_id"]]
                    store["vectors"][idx] = np.asarray(it["vector"], dtype=np.float32)
                    store["rows"][idx] = row
                else:
                    new_vecs.append(it["vector"])
                    new_rows.append(row)

            if new_vecs:
                store["vectors"] = np.vstack([store["vectors"], np.asarray(new_vecs, dtype=np.float32)])
                store["rows"].extend(new_rows)
                store["ids"].extend([r["chunk_id"] for r in new_rows])
            self._persist(collection)
            return len(items)

    def search(
        self, collection: str, vector: list[float], top_k: int = 10, expr: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            store = self._load(collection)
            vecs: np.ndarray = store["vectors"]
            if vecs.size == 0:
                return []
            q = np.asarray(vector, dtype=np.float32)
            if q.shape[0] != vecs.shape[1]:
                logger.warning("查询向量维度 %d 与索引维度 %d 不一致", q.shape[0], vecs.shape[1])
                return []
            # 向量已 L2 归一化时点积即余弦相似度
            sims = vecs @ q
            order = np.argsort(-sims)[: max(1, top_k)]
            out: list[dict[str, Any]] = []
            for i in order:
                row = store["rows"][int(i)]
                out.append(
                    {
                        "chunk_id": row["chunk_id"],
                        "score": float(sims[int(i)]),
                        "text": row["text"],
                        "corpus": row["corpus"],
                        "metadata": row.get("metadata") or {},
                        "retriever": "vector",
                    }
                )
            return out

    def count(self, collection: str) -> int:
        with self._lock:
            return len(self._load(collection)["ids"])

    def drop(self, collection: str) -> None:
        with self._lock:
            self._data.pop(collection, None)
            npz_path, meta_path = self._paths(collection)
            for p in (npz_path, meta_path):
                if p.exists():
                    p.unlink()

    def status(self) -> dict[str, Any]:
        return {
            "backend": "local-numpy",
            "dir": str(self.base_dir),
            "collections": {c: self.count(c) for c in COLLECTIONS},
        }


# --------------------------------------------------------------------------- 工厂
_store: VectorStoreBase | None = None
_store_lock = threading.Lock()
_store_info: dict[str, Any] = {}


def get_vector_store() -> VectorStoreBase:
    """获取向量库（Milvus 优先，失败降级本地）。"""
    global _store
    if _store is not None:
        return _store
    with _store_lock:
        if _store is not None:
            return _store
        if settings.MILVUS_ENABLED:
            try:
                store = MilvusStore(settings.MILVUS_URI, settings.MILVUS_TOKEN, settings.MILVUS_DB)
                # 真正探活：列集合
                store.client.list_collections()
                _store = store
                _store_info.update({"backend": "milvus", "uri": settings.MILVUS_URI})
                logger.info("向量库已启用：Milvus %s", settings.MILVUS_URI)
                return _store
            except Exception as exc:  # noqa: BLE001
                logger.warning("Milvus 不可用（%s），降级为本地 numpy 向量索引", exc)
                _store_info.update({"backend": "local", "milvus_error": str(exc)})
        if not settings.VECTOR_FALLBACK_TO_LOCAL:
            raise RuntimeError("向量库不可用且未开启本地兜底")
        _store = LocalVectorStore()
        logger.warning("向量库已降级为本地 numpy 索引（语义检索仍可用，但非生产形态）")
        return _store


def vector_status() -> dict[str, Any]:
    try:
        st = get_vector_store().status()
    except Exception as exc:  # noqa: BLE001
        return {"backend": "unavailable", "error": str(exc)}
    st["degraded"] = st.get("backend") != "milvus"
    return st
