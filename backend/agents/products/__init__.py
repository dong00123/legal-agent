"""产品级 Agent 运行时。

4 个产品入口（AI 助手 / 类案检索 / 合同审查 / 文书中心）从"共享固定管线的
门面配置"改造为"各自独立的真正 Agent"：每个产品拥有一份独立的
:class:`ProductRuntime` —— 独立的节点池、独立的自助工具池、独立的必需节点、
独立的自主规划提示词与自主裁决提示词。

本包不改变 HTTP 接口路径、请求/响应格式与前端菜单（改造只发生在后端
agent 运行时层，详见 docs/产品Agent运行时改造说明.md）。
"""

from backend.agents.products.planner import product_plan
from backend.agents.products.runtime import (
    PRODUCT_RUNTIMES,
    ProductRuntime,
    infer_product,
    product_agent_of_intent,
    resolve_runtime,
)

__all__ = [
    "ProductRuntime",
    "PRODUCT_RUNTIMES",
    "infer_product",
    "resolve_runtime",
    "product_agent_of_intent",
    "product_plan",
]
