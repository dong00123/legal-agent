"""PyInstaller 打包入口（launcher）。

**为什么不能把 ``client/app/main.py`` 直接交给 PyInstaller。**
冻结后模块不再从磁盘目录加载，而 ``client/app/main.py`` 里写的是
``from client.app.api_client import ...`` 这种包内绝对导入 ——
当它自己被当成 ``__main__`` 执行时，包上下文不存在，
启动即抛 ``attempted relative import with no known parent package``，
而且**只在打包后的 EXE 上出现**，开发时怎么跑都正常。

加这一层之后，入口是一个**不属于任何包**的脚本，
由它把项目根目录塞进 ``sys.path`` 再调真正的 ``main()``。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 源码运行时需要；冻结后这行无害（root 指向临时目录，导入走 bundle）
_root = str(Path(__file__).resolve().parents[1])
if _root not in sys.path:
    sys.path.insert(0, _root)

from client.app.main import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
