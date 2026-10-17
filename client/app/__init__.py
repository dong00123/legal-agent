"""桌面端应用层：HTTP 客户端（``api_client``）、状态（``state``）、入口（``main``）。

**分层的硬要求**：``api_client`` 与 ``state`` 都不 import PySide6。
好处是它们能被单独测试（不需要显示环境），也避免了"网络层里改一个
QMessageBox"这种把业务逻辑焊死在界面上的写法。
"""

from .api_client import ApiClient, ApiError
from .state import AppState, Settings

__all__ = ["ApiClient", "ApiError", "AppState", "Settings"]
