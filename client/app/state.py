"""桌面端状态：**哪些能落盘、哪些绝对不能**。

这里最重要的不是存了什么，而是**没存什么**：

- **不存 token。** 记住用户名可以，记住登录凭证不行 —— 一个躺在
  ``%APPDATA%`` 里的 JWT 等于"任何人拿到这台电脑就能以律师身份操作"，
  而它没有过期提示、也不会被"退出登录"清除（退出后文件还在）。
- **不存案件原文。** 材料正文只在内存里（``_materials``），退出即失。
  这是保密义务的要求：原文落到磁盘上会进回收站、进备份、进杀毒软件的缓存，
  而这些地方都不在律所的控制范围内。要长期留存就存到**服务端**（那是有审计、
  有权限、有留痕的地方），而不是客户端磁盘。

因此本模块**不依赖 Qt**：它是纯数据与文件读写，可以单独测试。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:8001"
CONFIG_FILE = "config.json"


def config_dir() -> Path:
    """配置目录。Windows 走 ``%APPDATA%``，其他走 XDG。"""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / "LegalAgentClient"
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "legal-agent-client"


@dataclass
class Settings:
    """**可以**落盘的东西。新增字段前先问一句：它算不算业务数据？"""

    base_url: str = DEFAULT_BASE_URL
    username: str = ""
    # 上次打开的案件 ID。只是个编号，不含任何正文。
    last_case_id: str = ""
    window_width: int = 1280
    window_height: int = 820

    def save(self) -> Path:
        d = config_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / CONFIG_FILE
        path.write_text(json.dumps(self.__dict__, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        return path

    @classmethod
    def load(cls) -> "Settings":
        path = config_dir() / CONFIG_FILE
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**known)


@dataclass
class AppState:
    """运行时状态。**含敏感数据，整体不落盘。**"""

    settings: Settings = field(default_factory=Settings)
    token: str = ""
    user: dict[str, Any] = field(default_factory=dict)
    # 案件材料正文：**只在内存**。见模块 docstring。
    _materials: dict[str, str] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------ 身份
    @property
    def logged_in(self) -> bool:
        return bool(self.token)

    @property
    def is_admin(self) -> bool:
        """与 Web 端同一口径：以服务端给的 ``is_admin`` 为准。

        兜底判 ``role == "admin"`` 的理由同 Web 端 —— 这一层只控制界面上
        **有没有入口**，真正的校验在服务端。少显示入口的代价（管理员找不到
        功能）比多显示（点进去看到 403）大得多。
        """
        return bool(self.user.get("is_admin") or self.user.get("role") == "admin")

    def set_material(self, case_id: str, text: str) -> None:
        self._materials[case_id] = text

    def get_material(self, case_id: str) -> str:
        return self._materials.get(case_id, "")

    def drop_material(self, case_id: str) -> None:
        self._materials.pop(case_id, None)

    def logout(self) -> None:
        """退出登录：**清空内存里的一切业务数据**。

        只清 token 是不够的 —— 下一个人坐到这台电脑前，界面上的登录框后面
        还挂着一个装满案件正文的字典。
        """
        self.token = ""
        self.user = {}
        self._materials.clear()

    def remember(self) -> None:
        """只把**非敏感**的连接信息写盘。"""
        self.settings.username = str(self.user.get("username") or self.settings.username)
        self.settings.save()
