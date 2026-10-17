"""``python -m backend.finetune --days 90`` 的入口。

单独放一个 ``__main__``，是为了让 ``python -m backend.finetune.dataset``
这种写法**不再被需要**：它会导致子模块被加载两次（一次作为包内模块、
一次作为 ``__main__``），表现为"改了常量不生效"这种极难查的问题。
"""

from __future__ import annotations

from .dataset import main

if __name__ == "__main__":
    raise SystemExit(main())
