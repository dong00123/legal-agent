"""BM25 关键词检索。

为什么法律场景必须要 BM25：
- 语义型：「员工长期未打卡但被默许继续工作，能否以旷工解除？」→ 向量检索；
- 精确型：「《民法典》第585条」「（2024）京01民终1234号」「LPR一年期」→ BM25。

实现要点（踩坑记录）：
1. 中文分词用 jieba，但**条文号/法规名/案号必须作为整体 token 保留**，
   否则「第八十七条」会被切成「第八/十/七条」，精确检索直接失效；
2. **默认引擎是 Whoosh**（``BM25_ENGINE=whoosh``），索引落在 ``<dir>/whoosh/<语料>``；
3. 内置实现（jsonl.gz）保留为**兜底**：Whoosh 不可用时自动回落，
   且首次启用 Whoosh、索引为空时会把内置索引的语料**迁移**过去（见 ``get_bm25``）
   —— 否则切完引擎检索结果直接变空，是最难排查的一类"静默退化"。
"""

from __future__ import annotations

import gzip
import json
import logging
import math
import re
import threading
from collections import Counter
from pathlib import Path
from typing import Any

from backend.app.config import settings

logger = logging.getLogger("legal_agent.rag.bm25")

try:
    import jieba

    jieba.setLogLevel(logging.WARNING)
    _JIEBA_OK = True
except Exception:  # noqa: BLE001
    _JIEBA_OK = False

# whoosh 是可选依赖（未装则整体回退内置实现，见 get_bm25）。
# ⚠ 但分析器必须在模块级拿到基类：谁定义成 __init__ 里的局部类谁就会被
# whoosh 的 schema pickle 拒收（TOC 落盘时 Can't get local object）。
try:
    from whoosh.analysis import Analyzer, Token

    _WHOOSH_OK = True
except Exception:  # noqa: BLE001
    _WHOOSH_OK = False

    class Analyzer:  # type: ignore[no-redef]  # 仅占位，保证模块可导入
        """未安装 whoosh 时的占位基类（此时 WhooshBM25Index 根本不会被构造）。"""

    Token = None  # type: ignore[assignment]

# 必须整体保留的强 token：条文号 / 法规名 / 案号 / 年份文号 / 英文缩写 / 数字
STRONG_TOKEN_RE = re.compile(
    r"第\s*[一二三四五六七八九十百零两\d]+\s*条(?:\s*之[一二三四五六七八九十]+)?"
    r"|《[^》]{2,40}》"
    r"|（\s*\d{4}\s*）[\u4e00-\u9fa5A-Za-z0-9]{2,30}号"
    r"|\(\s*\d{4}\s*\)[\u4e00-\u9fa5A-Za-z0-9]{2,30}号"
    r"|[A-Za-z][A-Za-z0-9._-]{1,20}"
    r"|\d+(?:\.\d+)?%?"
)

STOPWORDS = set(
    "的 了 和 与 及 或 在 是 为 对 从 到 就 都 而 之 其 此 该 等 有 无 不 也 被 把 向 由 于 以 并 且 但 则 若 如 按 依 应当 可以 予以 本 甲乙 双方".split()
)


def tokenize(text: str) -> list[str]:
    """分词：强 token 整体保留 + jieba 细粒度词。

    两套 token 同时入索引，等价于「精确匹配额外加权」——条文号与案号既不会被切碎，
    中文语义词也能参与匹配。
    """
    if not text:
        return []
    strong = [m.group(0).replace(" ", "") for m in STRONG_TOKEN_RE.finditer(text)]
    words: list[str] = []
    if _JIEBA_OK:
        words = [t.strip() for t in jieba.lcut(text) if len(t.strip()) > 1 and t.strip() not in STOPWORDS]
    else:
        words = re.findall(r"[\u4e00-\u9fa5]{2,}", text)
    return strong + words


class BM25Index:
    """一个语料库对应一个索引文件。"""

    def __init__(self, name: str, base_dir: str | Path | None = None, k1: float | None = None, b: float | None = None) -> None:
        self.name = name
        self.base_dir = Path(base_dir or settings.BM25_INDEX_DIR)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.k1 = settings.BM25_K1 if k1 is None else k1
        self.b = settings.BM25_B if b is None else b

        self._rows: list[dict[str, Any]] = []
        self._id_index: dict[str, int] = {}
        self._tf: list[Counter] = []
        self._len: list[int] = []
        self._df: Counter = Counter()
        self._avgdl = 1.0
        self._lock = threading.RLock()
        self._dirty = False

    # ---------------------------------------------------------------- 持久化
    @property
    def _path(self) -> Path:
        return self.base_dir / f"{self.name}.jsonl.gz"

    def load(self) -> "BM25Index":
        if not self._path.exists():
            return self
        try:
            with gzip.open(self._path, "rt", encoding="utf-8") as f:
                self._rows = [json.loads(line) for line in f if line.strip()]
            self._rebuild_stats()
            logger.info("BM25 索引已加载：%s（%d 条）", self.name, len(self._rows))
        except Exception as exc:  # noqa: BLE001
            logger.warning("BM25 索引加载失败（%s），将重建：%s", self.name, exc)
            self._rows = []
        return self

    def save(self) -> None:
        with self._lock:
            tmp = self._path.with_suffix(".tmp")
            with gzip.open(tmp, "wt", encoding="utf-8") as f:
                for row in self._rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            tmp.replace(self._path)
            self._dirty = False

    def _rebuild_stats(self) -> None:
        self._id_index = {}
        self._tf = []
        self._df = Counter()
        self._len = []
        for i, row in enumerate(self._rows):
            tokens = row.get("tokens") or tokenize(row.get("text", ""))
            row["tokens"] = tokens
            tf = Counter(tokens)
            self._tf.append(tf)
            self._len.append(max(1, len(tokens)))
            self._df.update(set(tokens))
            self._id_index[row["chunk_id"]] = i
        self._avgdl = (sum(self._len) / len(self._len)) if self._len else 1.0

    # ---------------------------------------------------------------- 写
    def add(self, items: list[dict[str, Any]], *, save: bool = True) -> int:
        """items: [{chunk_id, text, corpus, metadata}]"""
        if not items:
            return 0
        with self._lock:
            for it in items:
                cid = it["chunk_id"]
                tokens = tokenize(it.get("text", ""))
                row = {
                    "chunk_id": cid,
                    "text": it.get("text", ""),
                    "corpus": it.get("corpus", self.name),
                    "metadata": it.get("metadata") or {},
                    "tokens": tokens,
                }
                if cid in self._id_index:
                    idx = self._id_index[cid]
                    old_tokens = set(self._rows[idx].get("tokens") or [])
                    self._rows[idx] = row
                    self._tf[idx] = Counter(tokens)
                    self._len[idx] = max(1, len(tokens))
                    self._df.subtract(old_tokens)
                    self._df.update(set(tokens))
                else:
                    self._rows.append(row)
                    self._id_index[cid] = len(self._rows) - 1
                    self._tf.append(Counter(tokens))
                    self._len.append(max(1, len(tokens)))
                    self._df.update(set(tokens))
            self._df = Counter({k: v for k, v in self._df.items() if v > 0})
            self._avgdl = (sum(self._len) / len(self._len)) if self._len else 1.0
            self._dirty = True
            if save:
                self.save()
        return len(items)

    def remove_by_source(self, source_path: str, *, save: bool = True) -> int:
        with self._lock:
            keep = [r for r in self._rows if (r.get("metadata") or {}).get("source_path") != source_path]
            removed = len(self._rows) - len(keep)
            if removed:
                self._rows = keep
                self._rebuild_stats()
                self._dirty = True
                if save:
                    self.save()
            return removed

    def clear(self) -> None:
        with self._lock:
            self._rows, self._tf, self._len, self._id_index = [], [], [], {}
            self._df, self._avgdl = Counter(), 1.0
            if self._path.exists():
                self._path.unlink()
            self._dirty = False

    # ---------------------------------------------------------------- 读
    def __len__(self) -> int:
        return len(self._rows)

    def _idf(self, term: str) -> float:
        n = len(self._rows)
        df = self._df.get(term, 0)
        if df == 0:
            return 0.0
        return math.log(1.0 + (n - df + 0.5) / (df + 0.5))

    def search(
        self,
        query: str,
        top_k: int = 10,
        *,
        expr_filter: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Okapi BM25 检索。"""
        qtokens = tokenize(query)
        if not qtokens or not self._rows:
            return []
        scores: dict[int, float] = {}
        for term in set(qtokens):
            idf = self._idf(term)
            if idf <= 0:
                continue
            for i, tf in enumerate(self._tf):
                f = tf.get(term, 0)
                if not f:
                    continue
                dl = self._len[i]
                denom = f + self.k1 * (1 - self.b + self.b * dl / self._avgdl)
                scores[i] = scores.get(i, 0.0) + idf * (f * (self.k1 + 1)) / denom

        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        out: list[dict[str, Any]] = []
        for idx, score in ranked:
            row = self._rows[idx]
            if expr_filter and not _match_filter(row.get("metadata") or {}, expr_filter):
                continue
            out.append(
                {
                    "chunk_id": row["chunk_id"],
                    "score": float(score),
                    "text": row["text"],
                    "corpus": row.get("corpus", self.name),
                    "metadata": row.get("metadata") or {},
                    "retriever": "bm25",
                }
            )
            if len(out) >= top_k:
                break
        return out


def _match_filter(metadata: dict[str, Any], expr: dict[str, Any]) -> bool:
    for k, v in expr.items():
        if metadata.get(k) != v:
            return False
    return True


# --------------------------------------------------------------------------- Whoosh
class JiebaAnalyzer(Analyzer):  # type: ignore[misc]  # 基类在下方按需导入，见 _whoosh_imports
    """whoosh 没有中文分析器，必须自己接 jieba，否则整句中文会被当成单个 token。

    ⚠ 必须定义在**模块级**：schema 里挂了分析器实例，whoosh 建索引时会把
    schema 连分析器一起 pickle 进 TOC。写成 ``__init__`` 里的局部类，
    落盘那一步就炸 ``Can't get local object '<locals>.JiebaAnalyzer'``，
    同样被上层当成"Whoosh 不可用"静默回退内置实现。
    """

    def __call__(
        self,
        value,
        positions: bool = False,
        chars: bool = False,
        keeporiginal: bool = False,
        removestops: bool = True,
        start_pos: int = 0,
        start_char: int = 0,
        mode: str = "",
        **kwargs,
    ):
        text = value if isinstance(value, str) else value.decode("utf-8", "ignore")
        pos = start_pos
        char_pos = start_char
        for tok in tokenize(text):
            t = Token(positions, chars, removestops=removestops, mode=mode, **kwargs)
            t.text = tok.lower()
            t.boost = 1.0
            if keeporiginal:
                t.original = tok
            if positions:
                t.pos = pos
                pos += 1
            if chars:
                t.startchar = char_pos
                t.endchar = char_pos + len(tok)
                char_pos = t.endchar
            yield t


class WhooshBM25Index:
    """Whoosh 实现（可选引擎，``BM25_ENGINE=whoosh``）。

    自定义 jieba 分析器：whoosh 没有中文分析器，直接用 StandardAnalyzer 会把
    整句中文当成一个 token，检索必然失效。
    """

    def __init__(self, name: str, base_dir: str | Path | None = None) -> None:
        from whoosh import index as whoosh_index
        from whoosh.fields import ID, STORED, TEXT, Schema

        self.name = name
        self.base_dir = Path(base_dir or settings.BM25_INDEX_DIR) / "whoosh"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._dir = self.base_dir / name
        # ⚠ 语料子目录也要建：whoosh 的 create_in 只保证写文件，不建目录，
        #   于是首次建索引时在写 TOC 临时文件那一步炸
        #   ``FileNotFoundError: .../<语料>/_MAIN_0.toc.<ts>`` ——
        #   被 get_bm25 当成"Whoosh 不可用"静默回退内置实现，
        #   表现为配置写 whoosh、实际一直跑 builtin。
        self._dir.mkdir(parents=True, exist_ok=True)

        schema = Schema(
            chunk_id=ID(stored=True, unique=True),
            text=TEXT(stored=True, analyzer=JiebaAnalyzer()),
            corpus=ID(stored=True),
            meta=STORED,
        )
        if whoosh_index.exists_in(str(self._dir)):
            self.ix = whoosh_index.open_dir(str(self._dir))
        else:
            self.ix = whoosh_index.create_in(str(self._dir), schema)

    def add(self, items: list[dict[str, Any]], *, save: bool = True) -> int:
        writer = self.ix.writer()
        for it in items:
            writer.update_document(
                chunk_id=it["chunk_id"],
                text=it.get("text", ""),
                corpus=it.get("corpus", self.name),
                meta=json.dumps(it.get("metadata") or {}, ensure_ascii=False),
            )
        writer.commit()
        return len(items)

    def remove_by_source(self, source_path: str, *, save: bool = True) -> int:
        removed = 0
        writer = self.ix.writer()
        with self.ix.searcher() as searcher:
            for hit in searcher.all_stored_fields():
                meta = json.loads(hit.get("meta") or "{}")
                if meta.get("source_path") == source_path:
                    writer.delete_by_term("chunk_id", hit["chunk_id"])
                    removed += 1
        writer.commit()
        return removed

    def clear(self) -> None:
        from whoosh.writing import CLEAR

        writer = self.ix.writer()
        writer.commit(mergetype=CLEAR)

    def __len__(self) -> int:
        return self.ix.doc_count()

    def _parse(self, query: str):
        """把用户提问解析成 Whoosh 查询对象。

        两处必须和内置实现对齐，否则切完引擎会发现"检索突然变差"：
        1. **组合方式**：默认的 Group 是 AND —— 查询里任何一个词不在语料里
           就整条零命中（实测"未签书面劳动合同 二倍工资"：内置 5 条、
           Whoosh 0 条）。内置 BM25 是按词累加得分（OR 语义），这里用
           ``OrGroup`` 才能召回一致，保留 0.9 的惩罚让多词命中仍排前面。
        2. **特殊字符**：案号「（2023）京01民终123号」这类含括号/冒号的串
           会直接让 QueryParser 抛错。解析失败时先剥掉语法字符重试，
           再失败才返回 None（由调用方按空结果处理）。
        """
        from whoosh.qparser import MultifieldParser, OrGroup, QueryParserError

        parser = MultifieldParser(["text"], schema=self.ix.schema,
                                  group=OrGroup.factory(0.9))
        try:
            return parser.parse(query)
        except (QueryParserError, Exception):  # noqa: BLE001 - 畸形查询不应中断检索
            pass
        safe = re.sub(r"[(){}\[\]^~*?:\"'/\\]", " ", query).strip()
        if not safe:
            return None
        try:
            return parser.parse(safe)
        except Exception:  # noqa: BLE001
            logger.warning("BM25 查询解析失败，本次跳过关键词检索：%r", query[:80])
            return None

    def search(self, query: str, top_k: int = 10, *, expr_filter: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        q = self._parse(query)
        if q is None:
            return out
        with self.ix.searcher() as searcher:
            results = searcher.search(q, limit=top_k * 3)
            for hit in results:
                meta = json.loads(hit.get("meta") or "{}")
                if expr_filter and not _match_filter(meta, expr_filter):
                    continue
                out.append(
                    {
                        "chunk_id": hit["chunk_id"],
                        "score": float(hit.score),
                        "text": hit.get("text", ""),
                        "corpus": hit.get("corpus", self.name),
                        "metadata": meta,
                        "retriever": "bm25",
                    }
                )
                if len(out) >= top_k:
                    break
        return out


# --------------------------------------------------------------------------- 管理
_indexes: dict[str, Any] = {}
_lock = threading.Lock()


def _migrate_builtin_to_whoosh(name: str, target: "WhooshBM25Index") -> bool:
    """把内置索引（jsonl.gz）里的语料迁进 Whoosh，返回是否迁了。

    ⛔ 为什么必须有这一步：把默认引擎切成 Whoosh 之后，目录是新的
    （``<dir>/whoosh/<语料>``），既有语料全在内置索引里 ——
    不迁移的话检索会**返回空而没有任何报错**，表现为"知识库突然检不到了"，
    排查方向极易跑偏（会去查向量库、查 embedding，而不是查索引目录）。
    """
    try:
        src = BM25Index(name).load()
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取内置 BM25 索引失败（%s），跳过迁移：%s", name, exc)
        return False
    rows = [
        {
            "chunk_id": r.get("chunk_id"),
            "text": r.get("text", ""),
            "corpus": r.get("corpus", name),
            "metadata": r.get("metadata") or {},
        }
        for r in (getattr(src, "_rows", None) or [])
        if r.get("chunk_id")
    ]
    if not rows:
        return False
    try:
        target.add(rows)
    except Exception as exc:  # noqa: BLE001
        logger.warning("迁移内置索引到 Whoosh 失败（%s）：%s", name, exc)
        return False
    logger.info("BM25 引擎切换：已从内置索引迁移 %d 条语料到 Whoosh（%s）", len(rows), name)
    return True


def get_bm25(name: str) -> Any:
    """按语料库取 BM25 索引（懒加载）。默认 Whoosh，失败回落内置实现。"""
    if name in _indexes:
        return _indexes[name]
    with _lock:
        if name in _indexes:
            return _indexes[name]
        if settings.BM25_ENGINE == "whoosh":
            try:
                idx = WhooshBM25Index(name)
                # 新索引为空 + 内置索引有货 ⇒ 迁过去，避免"切引擎后检索变空"
                if len(idx) == 0:
                    _migrate_builtin_to_whoosh(name, idx)
                _indexes[name] = idx
                logger.info("BM25 引擎：Whoosh（%s，%d 条）", name, len(idx))
                return idx
            except Exception as exc:  # noqa: BLE001
                logger.warning("Whoosh 初始化失败（%s），回退内置实现", exc)
        idx = BM25Index(name).load()
        _indexes[name] = idx
        return idx


def _engine_of(idx: Any) -> str:
    """索引实例实际用的引擎名（配置值 ≠ 实际值：可能已回落内置实现）。"""
    return "whoosh" if isinstance(idx, WhooshBM25Index) else "builtin"


def bm25_status() -> dict[str, Any]:
    """检索面板/健康检查用。**engine 报实际生效的引擎**，不是配置值 ——
    "配置写 whoosh、实际跑 builtin"这种静默回落必须能被看出来。"""
    engines = {name: _engine_of(idx) for name, idx in _indexes.items()}
    return {
        "engine": next(iter(engines.values()), settings.BM25_ENGINE),
        "configured": settings.BM25_ENGINE,
        "engines": engines,
        "index_dir": settings.BM25_INDEX_DIR,
        "corpora": {name: len(idx) for name, idx in _indexes.items()},
    }
