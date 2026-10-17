"""评测模块：从**跑过的真实任务**里算质量指标。

用法::

    python -m backend.evals --days 7                    # 出报告（同时写 JSON）
    python -m backend.evals --days 30 --limit 2000      # 拉长窗口
    python -m backend.evals --json-only                 # 只出 JSON（给流水线）
    python -m backend.evals --no-out                    # 只打印，不落盘

覆盖的指标（详见 :mod:`backend.evals.metrics`）：

- **引用可采信率 / 拦截率 / 终审执行率** —— 报告里的法条经没经过终审核实。
  这三条要一起看：拦截率变 0 有两种原因，终审执行率能把它们分开。
- **证据缺口比例（按类型）** —— 缺口不是越低越好，它是缺什么的诚实声明。
- **降级完成占比 / 失败占比** —— 分开算，降级是可用交付物。
- **各节点耗时 p50 / p95** —— 按单次排序，不按总耗时。
- **工具失败率（按工具）** —— 混在一起算会被高频工具稀释。
- **语料体检** —— 特别是"登记为有效但零条文"的法规。

**这一层不做判断，只出数字。** 要不要因为某个指标变差而重新调检索、
要不要换模型，是人和流程的决定；评测给出的是可复算的依据。
"""

from __future__ import annotations

import argparse
import sys

from . import metrics, runner

__all__ = ["metrics", "runner"]
