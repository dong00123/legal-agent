"""失败聚类的语义向量适配层（可降级）。

图 L3 写的是「**embedding** 相似失败归并」。本模块把项目的 embedding 能力
包成**同步可调用**的形式，供 ``cluster_failures`` 使用；任何一步不可用
（依赖缺失、模型加载失败、当前就在事件循环里）都返回 None，调用方据此
回退到字符集相似度。

**为什么默认不启用。** 加载 BGE 权重实测 16.8 秒（见 README 的预热说明），
而失败聚类的调用点挂在在线接口 ``GET /evolution/clusters/{skill}`` 上。
把它设成默认，等于让首次巡检请求干等 17 秒 —— 而巡检恰恰是律师在
工作台里随手点开就看的东西。所以走**显式开关**，并且聚类结果里如实标注
本次用的是哪一种相似度：降级必须是看得见的，不能让人以为看到的是语义归并。

**为什么在事件循环里直接放弃。** 本模块是同步的，而 ``embedding_client``
是异步的。在已有运行中的事件循环里（异步路由内部）强行 ``asyncio.run``
会抛 ``RuntimeError: This event loop is already running``，且这个异常若被
吞掉，表现会是"聚类结果莫名其妙变粗糙了"。直接返回 None 让降级路径显式
发生，比在半路炸掉好判断。
"""

from __future__ import annotations

import asyncio
import math
from typing import Callable, Sequence

# 语义相似度阈值与字符集相似度阈值不可通用：
# Jaccard 在同一族失败上通常落在 0.5~0.7，余弦相似度普遍更高（0.8+）。
# 用同一个阈值会让"语义归并"把所有失败并成一坨，等于不聚类。
DEFAULT_EMBEDDING_THRESHOLD = 0.85

Embedder = Callable[[Sequence[str]], "list[list[float]] | None"]


def embed_texts(texts: Sequence[str]) -> list[list[float]] | None:
    """同步取 embedding 向量；不可用返回 None。"""
    items = [str(text) for text in texts]
    if not items:
        return None
    try:
        from backend.rag.embedding import embedding_client
    except Exception:  # noqa: BLE001 - 依赖缺失即降级，不影响聚类的字符集路径
        return None

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        return None  # 已有运行中的循环，见模块 docstring

    try:
        vectors = asyncio.run(embedding_client.encode(items))
    except Exception:  # noqa: BLE001 - 模型加载失败/网关不可达一律降级
        return None
    if not vectors or len(vectors) != len(items):
        return None
    return vectors


def get_embedder(enabled: bool = True) -> Embedder | None:
    """按开关返回 embedder；``enabled=False`` 时返回 None（调用方走字符集路径）。"""
    return embed_texts if enabled else None


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """余弦相似度。空向量返回 0，不抛异常 —— 聚类不该因为一条坏向量整体失败。"""
    if not left or not right:
        return 0.0
    size = min(len(left), len(right))
    dot = sum(float(left[i]) * float(right[i]) for i in range(size))
    left_norm = math.sqrt(sum(float(left[i]) ** 2 for i in range(size)))
    right_norm = math.sqrt(sum(float(right[i]) ** 2 for i in range(size)))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


def probe() -> dict[str, object]:
    """自检：报告当前 embedding 是否可用（供 ``/evolution/meta`` 如实展示）。"""
    try:
        from backend.rag.embedding import embedding_client  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"embedding 依赖不可用：{exc}"}
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return {"available": True, "reason": "同步上下文可用（离线脚本/线程池）"}
    return {"available": False, "reason": "当前在事件循环内，语义归并需离线执行"}
