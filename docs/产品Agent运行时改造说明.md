---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 084a826ba32fc879edcb905005df1ab9_7deb7888c22a11f1887c525400de85a5
    ReservedCode1: XuNbZ/KI9kC2RqkgvG/Et5EesLwa9PSiALBxQO2p5ethbzSqazVbuH6t8/5Lw7qyWCAo1nNH6vKtoqdPzGx6Z1Q5wp+ZRuZtZ0O/SqwMAJIsNSEvjBx190P+G7iTLSdQ7OpNW9AhRdJLVTiC9bXV7ffVxz9p2GJ4WrWLRkGmmgP546gxTeG74JW/vcw=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 084a826ba32fc879edcb905005df1ab9_7deb7888c22a11f1887c525400de85a5
    ReservedCode2: XuNbZ/KI9kC2RqkgvG/Et5EesLwa9PSiALBxQO2p5ethbzSqazVbuH6t8/5Lw7qyWCAo1nNH6vKtoqdPzGx6Z1Q5wp+ZRuZtZ0O/SqwMAJIsNSEvjBx190P+G7iTLSdQ7OpNW9AhRdJLVTiC9bXV7ffVxz9p2GJ4WrWLRkGmmgP546gxTeG74JW/vcw=
---

# 产品 Agent 运行时改造说明（2026-10-07）

## 一、改造目标

把 `backend/agents/harness/router.py` 中 `PRODUCT_AGENTS` 的 4 个产品入口
（**AI 助手 / 类案检索 / 合同审查 / 文书中心**）从"门面 + 共享固定管线"
改造为**各自独立的真正 Agent**：

- 每个产品拥有**独立的运行时**（`ProductRuntime`）：独立节点池、独立工具池、
  独立必需节点、独立自主规划提示词与自主裁决提示词；
- 每个产品**自主思考**：任务启动时由 LLM 在该产品节点池内**自主规划**本次路线
  （`products/planner.py` 产品级规划），运行中由该产品自己的控制器提示词
  **自主裁决**是否调用工具补证（`harness/controller.py` 候选裁剪 + 产品提示词）；
- 每个产品**自主调用工具**：控制器候选动作集按该产品 `tool_pool` 裁剪。

**硬性约束达成**：HTTP 接口路径、请求/响应格式、前端菜单与交互**零变动**
（见第四节验证方式）。

## 二、改动内容

| 文件 | 改动 |
|---|---|
| `backend/agents/products/__init__.py` | **新增**。产品运行时包入口，导出 `ProductRuntime` / `PRODUCT_RUNTIMES` / `infer_product` / `resolve_runtime` / `product_plan` 等 |
| `backend/agents/products/runtime.py` | **新增**。`ProductRuntime` 数据类（node_pool / tool_pool / essential / base_route / force_report / planner_system / controller_system）与 `PRODUCT_RUNTIMES`（4 产品注册表）、`infer_product` / `resolve_runtime`（按入口意图绑定运行时）、`planner_system` / `controller_system`（产品大脑提示词）、`runtimes_directory`（运行时目录，非前端菜单数据源） |
| `backend/agents/products/planner.py` | **新增**。`product_plan()` 产品级自主规划：LLM 在该产品节点池内决策本次执行路线，必需节点不可删、顺序不可打乱、失败自动回退 `base_route`（= 原静态路由） |
| `backend/agents/harness/__init__.py` | `Harness.__init__` 新增 `self.product` 与 `self.product_plan` 两个运行时字段（挂在对象上，非 State，与 controller/a2a_budget 同理） |
| `backend/agents/harness/controller.py` | `_think` 使用 `products.controller_system` 产品级裁决提示词（无产品时用全局默认）；`_candidates` 按 `harness.product.tool_pool` 裁剪候选动作集 |
| `backend/agents/nodes/intent_node.py` | 钉意图分支：`harness.product_plan` 非空时作为 `sanitize` 的 override 覆盖静态路由；未绑定 / 未启用 / 规划失败时回退原静态路由（行为与改造前逐字一致） |
| `backend/app/services/orchestrator.py` | `Harness.start` 之后、graph 之前：`resolve_runtime(intent)` 绑定 `harness.product`，并预调用 `product_plan` 生成 `harness.product_plan`；新增 trace 日志 `product_bound` |
| `backend/app/config.py` | 新增 `PRODUCT_PLAN_ENABLED: bool = True` 与 `PRODUCT_PLAN_LLM_TIMEOUT: float = 12.0` |
| `backend/agents/harness/router.py` | `ProductAgent` 文档注释更新（运行时已独立化，门面数据键不变） |

## 三、运行机制（执行链路）

```
POST /api/v1/...（4 个入口端点不变）
  → orchestrator.execute
      → Harness.start(state, ...)
      → resolve_runtime(req.intent)          # 绑定该产品独立运行时 harness.product
      → product_plan(product, ...)           # LLM 在该产品节点池内自主规划 → harness.product_plan
  → graph.run（LangGraph 状态机不变）
      → IntentNode 钉意图分支：product_plan 非空则按产品自主规划路线执行
      → Controller.review：按该产品 tool_pool 裁剪候选 + 产品裁决提示词
          → call_tool 自主补证 → _rewind LOOP_RERUN_TAIL 重跑 Validator+Report
```

- **AI 助手**：不钉意图 → 运行时为通用 `assistant`（全量节点池/工具池），
  行为与改造前逐字一致（本就通过 IntentNode 分类 + 动态规划自主思考）。
- **类案检索**：节点池/工具池聚焦检索与核验，`CaseNode` 为必需节点；
- **合同审查**：节点池聚焦材料解析 + 审查 + 引用终审 + 报告，`ReviewNode` 必需；
- **文书中心**：`force_report=False`（交付物由模板渲染，刻意不产报告，
  与既有 `doc_compose` 路径形状一致），`ValidatorNode` 必需（引用终审是
  这条路的全部意义）。

## 四、前端零变动验证方式

1. **产品菜单**：`product_directory()` 输出键与顺序不变（仍是
   `assistant / case_search / contract_review / document` 4 个），
   `PRODUCT_AGENTS` 名字逐字对齐侧边栏 —— 由 `tests/test_product_agents.py`
   `test_sidebar_has_exactly_four_product_agents` 钉住。
2. **接口路径**：4 个入口端点字符串未改 —— 由
   `test_all_product_endpoints_are_really_mounted`（OpenAPI 路径存在性）钉住。
3. **请求/响应格式**：`RunRequest` / `RunOutcome` / 流式 chunk 结构未改；
   `intent / top_k` 通路与缓存指纹行为不变 —— 由 `test_api.py`（端到端，
   不 mock 编排）、`test_top_k_survives_the_queue_payload_round_trip`、
   `test_pin_and_top_k_are_folded_into_the_cache_fingerprint` 钉住。
4. **回退兜底**：产品规划未启用 / LLM 超时 / 规划失败时自动回退 `base_route`
   （= 原静态路由），行为与改造前一致 —— 由 intent_node 的 override 逻辑 +
   `test_pinned_intent_is_not_reclassified`（无产品绑定时走完整静态路由）钉住。
5. **全量回归**：本项目 pytest 全量用例（不含桌面端 `test_client_ui.py`，
   其失败为环境缺 PySide6 运行库、与本次后端改动无关）全绿，
   其中 `test_api.py` 56 用例为真实端到端 HTTP 验证。

## 五、新增文件清单

- `backend/agents/products/__init__.py`
- `backend/agents/products/runtime.py`
- `backend/agents/products/planner.py`
- 本文档 `docs/产品Agent运行时改造说明.md`

## 六、兼容性说明

- 不改动 `graph.py` 的 LangGraph 状态机、`NODES` 注册、`END` 终态与
  `LOOP_RERUN_TAIL` 回环不变式（补证据后必须重跑 Validator+Report）；
- 运行时字段 `harness.product / harness.product_plan` 挂在 Harness 对象上，
  不写入 State，与既有 `controller / a2a_budget` 的挂载方式一致；
- 控制器计数（`max_tool_calls` 等）仍在 Harness 对象上，不写 State；
- 未绑定产品的直接 `Harness.start`（测试/离线）路径行为与改造前逐字一致。
*（内容由AI生成，仅供参考）*
