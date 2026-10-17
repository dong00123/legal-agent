"""微调数据工程。

**这里是整个项目里最该保守的一层。** 前面所有环节产出的是一次回答，
错了改一次就行；这里产出的是**模型的习惯**，错了会跟着模型走很久，
而且很难追溯是哪条样本教坏了它。

所以本包的三条硬规则：

1. **没有引用的样本一律不要。** 它教会模型的是"可以不出依据"，
   而引用可核验正是这个系统对律师唯一的价值。
2. **脱敏有残留的一律剔除，不做"部分脱敏"。** 漏一个真名进训练集，
   就是把它写进了模型参数 —— 事后没法从模型里删掉。
3. **每条样本都能追到原始 task_id。** 律师审核时看到的必须是"这条来自哪次任务"，
   而不是一段没有出处的文本。

训练本身不在这里做（见 ``llama_factory/``），这里只管把数据造干净。

**为什么不在 ``__init__`` 里 import 子模块。** 一旦 import，
``python -m backend.finetune.dataset`` 会先把包初始化一遍、子模块进
``sys.modules``，然后解释器又要以 ``__main__`` 再执行一次 ——
于是每个模块有两份，改了配置不生效这类怪事就是这么来的。
所以用 :func:`__getattr__` 做惰性导出，命令行走 ``backend.finetune.__main__``。
"""

from __future__ import annotations

from typing import Any

__all__ = ["MIN_REPORT_CHARS", "Sample", "build", "load_tasks", "write_jsonl"]

_LAZY = {
    "MIN_REPORT_CHARS": "dataset",
    "Sample": "dataset",
    "build": "dataset",
    "load_tasks": "dataset",
    "write_jsonl": "dataset",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        import importlib

        mod = importlib.import_module(f"{__name__}.{_LAZY[name]}")
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
