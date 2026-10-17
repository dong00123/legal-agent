"""面板共用的一点点东西。

只有这些才该放这里：**错误怎么显示**、**等级怎么翻译**。
业务布局一律留在各自的面板里 —— 一个"通用面板基类"很快就会长成
什么都管的怪物，然后每次改一个面板都要先读一遍基类。
"""

from __future__ import annotations

LEVEL_LABEL = {"high": "高", "medium": "中", "low": "低"}
LEVEL_COLOR = {"high": "#c0392b", "medium": "#b9770e", "low": "#5d6d7e"}


def err_msg(exc: BaseException) -> str:
    """把异常变成**能显示给律师看**的一句话。

    优先级：服务端 detail > ApiError.user_message > 异常文本。
    服务端 detail 排第一是因为它说得最准（"这个文书类型不支持"），
    而客户端只能猜（"请求失败"）。只显示类名的话，
    "口令错了"和"服务器没起"在界面上长得一模一样。
    """
    detail = getattr(exc, "detail", "")
    if detail:
        return str(detail)[:200]
    message = getattr(exc, "user_message", "")
    if message:
        return str(message)[:200]
    return f"{type(exc).__name__}: {exc}"[:200]
