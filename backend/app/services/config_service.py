"""运行时配置白名单（管理后台「系统配置」页）。

**为什么要有白名单，而不是"什么都能改"。**

``la_system_config`` 是 ``Settings`` 的覆盖层。如果放开任意键，管理员可以：

- 把 ``DB_URL`` 改成另一个库 —— 下一次重启数据就"消失"了；
- 把 ``SECRET_KEY`` 改掉 —— 所有已签发令牌立刻失效（看起来像"全所掉线"）；
- 把 ``LLM_BASE_URL`` 指到一个不存在的地址 —— 整条链路静默降级，
  而界面显示"已保存成功"。

这些不是"高级用法"，是事故。所以本模块**显式列出可运行时修改的键**，
其余键即使写进数据库也不生效（``knowledge_service.set_config`` 会拒绝）。

判定标准很简单：**改错了只会导致输出质量下降、不会导致数据或鉴权出问题的键，
才可热改。**

另外 ``EDITABLE`` 与 ``Settings`` 的字段必须一一对得上 —— ``health()`` 做这项自检。
写得对不上（例如某天改了 ``Settings`` 的字段名）会让管理后台出现一个
"能填、能存、但不生效"的假开关，这是最难发现的一类缺陷。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("legal_agent.services.config")


def _spec(group: str, value_type: str, description: str) -> dict[str, str]:
    return {"group": group, "type": value_type, "description": description}


# --------------------------------------------------------------------------- 白名单
EDITABLE: dict[str, dict[str, str]] = {
    # ---- Agent 行为参数
    "MAX_STEPS": _spec("agent", "int", "单次任务的节点步数上限（含一次 Validator 回环）"),
    "TOOL_TIMEOUT": _spec("agent", "int", "单个工具调用超时（秒）"),
    "TOOL_MAX_RETRY": _spec("agent", "int", "幂等工具的最大重试次数"),
    "TOOL_RETRY_BACKOFF": _spec("agent", "float", "重试退避基数（秒）"),
    "STATUTE_SEARCH_MAX_CALLS": _spec("agent", "int", "单次任务内法规检索配额"),
    "CASE_SEARCH_MAX_CALLS": _spec("agent", "int", "单次任务内类案检索配额"),
    "CITATION_MIN_CONFIDENCE": _spec("agent", "float", "引用纳入输出的最低可信度（0~1）"),
    "EVIDENCE_GAP_ALERT_RATIO": _spec("agent", "float", "效力状态未知的法条占比告警阈值"),
    # ---- 自主循环（2026-10-07）。可热改是刻意的：演示/答辩要"同问同答"时，
    # 关掉 CONTROLLER_ENABLED 就退回完全确定性的静态路线，不用改代码重启。
    "CONTROLLER_ENABLED": _spec("agent", "bool", "关闭后 4 个入口退回静态路线（同问同答，不做自主补证据）"),
    "CONTROLLER_MAX_ROUNDS": _spec("agent", "int", "单个任务最多自主思考几轮"),
    "CONTROLLER_LLM_MAX_CALLS": _spec("agent", "int", "单个任务内控制器自身的模型调用上限"),
    "CONTROLLER_LLM_TIMEOUT": _spec("agent", "float", "单次自主裁决的模型调用超时（秒）"),
    "CONTROLLER_MAX_TOOL_CALLS": _spec("agent", "int", "控制器直调工具的条数上限（无配额工具的兜底闸）"),
    "MAX_LOOP": _spec("agent", "int", "引用终审回环上限；调大时须同步调大 MAX_STEPS"),
    # A2A 委派上限（2026-10-07 从 delegation.py 的模块常量提上来，默认值不变）。
    # 放到这里是为了能**演示拦截**：把档一调到 0，一次委派都发不出去，
    # 而这条路径原本只能改代码重启才动得了。
    "DELEGATION_MAX_IN_ROUTE": _spec("agent", "int", "单任务内「叫回本次该跑的节点」的委派次数上限"),
    "DELEGATION_MAX_EXPANSION": _spec("agent", "int", "单任务内「拉一个起手范围外的 Agent」的委派次数上限"),
    "DELEGATION_MAX_DEPTH": _spec("agent", "int", "委派链深度上限（挡 A→B→A 互相委派）"),

    # ---- 检索参数
    "RETRIEVE_TOP_K": _spec("rag", "int", "混合检索召回条数"),
    "RERANK_TOP_K": _spec("rag", "int", "重排后保留条数"),
    "RRF_K": _spec("rag", "int", "RRF 融合常数（越大越平滑）"),
    "BM25_K1": _spec("rag", "float", "BM25 词频饱和参数"),
    "BM25_B": _spec("rag", "float", "BM25 长度归一化参数"),

    # ---- 模型参数
    "LLM_MODEL": _spec("model", "str", "推理服务上的模型名"),
    "LLM_MAX_TOKENS": _spec("model", "int", "单次生成的最大 token 数"),
    "LLM_TEMPERATURE": _spec("model", "float", "采样温度（法律场景建议 0.1~0.3）"),
    "LLM_TIMEOUT": _spec("model", "int", "模型调用超时（秒）"),
    "LLM_ENABLE_THINKING": _spec("model", "bool", "是否启用思考模式（法律场景建议关闭）"),
    "VLM_MODEL": _spec("model", "str", "多模态识别模型名"),

    # ---- 降级开关（方案第十三章例外处理的落地）
    "VLM_ENABLED": _spec("degrade", "bool", "关闭后跳过多模态识别，保留页内文字与占位符"),
    "MILVUS_ENABLED": _spec("degrade", "bool", "关闭后向量检索走本地兜底索引"),
    "BM25_ENABLED": _spec("degrade", "bool", "关闭后仅走向量通道（不建议）"),
    "REDIS_ENABLED": _spec("degrade", "bool", "关闭后短期记忆回落进程内字典"),

    # ---- 合规
    "DISCLAIMER": _spec("system", "str", "输出末尾的免责声明文案（合规要求，不建议清空）"),
    "DESENSITIZE_ENABLED": _spec("system", "bool", "长期记忆与向量库写脱敏"),
    "AUDIT_ENABLED": _spec("system", "bool", "审计留痕（关掉将无法通过合规审查）"),
    "MAX_UPLOAD_MB": _spec("system", "int", "单文件上传大小上限（MB）"),
}

# 明确**不**允许热改的键，以及原因。写在代码里而不是文档里 ——
# 这样"为什么不能改"永远和"不能改"待在一起。
FORBIDDEN: dict[str, str] = {
    "DB_URL": "热改会让下一次重启连到另一个库，表现为数据凭空消失",
    "SQLITE_FALLBACK_URL": "同上",
    "SECRET_KEY": "热改会让所有已签发令牌立刻失效，表现为全所同时掉线",
    "ACCESS_TOKEN_EXPIRE_MINUTES": "改小会让在线用户被立即踢出；请改配置后重启",
    "LLM_BASE_URL": "指向不存在的地址会静默降级，界面却显示保存成功",
    "VLM_BASE_URL": "同上",
    "EMBEDDING_BASE_URL": "同上",
    "MILVUS_URI": "同上",
    "REDIS_URL": "同上",
    "RABBITMQ_URL": "同上",
    "ENV": "运行环境标识，改它会让「生产禁用弱口令」等安全约束悄悄失效",
    "TRUST_PROXY_HEADERS": "打开后任何人伪造 X-Forwarded-For 就能改写审计里的来源 IP",
    "ADMIN_USERNAME": "账号标识不作为配置热改",
    "ADMIN_PASSWORD": "口令走用户管理，不走配置表",
}


def groups() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key, meta in EDITABLE.items():
        out.setdefault(meta["group"], []).append(key)
    return {k: sorted(v) for k, v in sorted(out.items())}


def describe() -> list[dict[str, Any]]:
    """白名单清单（管理后台展示「哪些能改」用）。"""
    return [
        {"key": k, "group": m["group"], "type": m["type"], "description": m["description"]}
        for k, m in sorted(EDITABLE.items())
    ]


def forbidden() -> list[dict[str, str]]:
    """不可热改的键及原因。"""
    return [{"key": k, "reason": v} for k, v in FORBIDDEN.items()]


def health() -> dict[str, Any]:
    """自检：白名单键必须都是 ``Settings`` 的真实字段，且不得与禁改清单重叠。

    这两项任一不成立，管理后台就会出现「能填能存但不生效」的假开关。
    """
    from backend.app.config import Settings

    fields = set(Settings.model_fields)
    unknown = sorted(k for k in EDITABLE if k not in fields)
    overlap = sorted(set(EDITABLE) & set(FORBIDDEN))
    return {
        "editable": len(EDITABLE),
        "groups": {k: len(v) for k, v in groups().items()},
        "unknown_keys": unknown,      # 必须是空列表
        "overlap_with_forbidden": overlap,  # 必须是空列表
        "ok": not unknown and not overlap,
    }


if __name__ == "__main__":  # pragma: no cover - 手工自检入口
    print(health())
    for g, keys in groups().items():
        print(f"[{g}] {', '.join(keys)}")


__all__ = ["EDITABLE", "FORBIDDEN", "describe", "forbidden", "groups", "health"]
