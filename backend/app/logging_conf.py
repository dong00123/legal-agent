"""统一日志配置。

要点（踩坑记录）：Python 默认 ``logging.lastResort`` 只输出 WARNING 以上，
用 ``logger.info()`` 打的启动状态在默认 uvicorn 下完全看不见，等于白写。
所以这里显式挂 handler，并把业务 logger 的级别设为 INFO。
"""

from __future__ import annotations

import logging
import sys


class _ColorFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[36m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[41m",
    }
    RESET = "\033[0m"

    def __init__(self, use_color: bool = True) -> None:
        super().__init__("%(asctime)s | %(levelname)-7s | %(name)-28s | %(message)s", "%H:%M:%S")
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        if not self.use_color:
            return msg
        color = self.COLORS.get(record.levelname, "")
        return f"{color}{msg}{self.RESET}" if color else msg


_CONFIGURED = False


def setup_logging(level: str = "INFO") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_ColorFormatter(use_color=sys.stdout.isatty()))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 业务包统一 INFO，第三方库收敛到 WARNING，避免刷屏
    logging.getLogger("legal_agent").setLevel(logging.INFO)
    for noisy in ("httpx", "httpcore", "urllib3", "pymilvus", "milvus_lite", "sqlalchemy.engine", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    for name in ("backend", "backend.app", "backend.agents", "backend.rag", "backend.parser"):
        logging.getLogger(name).setLevel(logging.INFO)

    _CONFIGURED = True
