"""桌面端面板。

每个面板拿到的都是同一对东西：``client``（怎么说话）与 ``state``（现在是谁）。
**面板之间不互相引用** —— 需要共享的东西（当前案件、登录状态）放在 state 里，
而不是让审查面板直接去翻草稿面板的列表。
"""

from .admin_panel import AdminPanel
from .base import LEVEL_COLOR, LEVEL_LABEL, err_msg
from .capabilities_panel import CapabilitiesPanel
from .cases_panel import CasesPanel
from .chat_panel import ChatPanel
from .drafts_panel import DraftsPanel
from .home_panel import HomePanel
from .review_panel import ReviewPanel
from .search_panel import SearchPanel
from .tasks_panel import TasksPanel

__all__ = ["LEVEL_COLOR", "LEVEL_LABEL", "AdminPanel", "CapabilitiesPanel",
           "CasesPanel", "ChatPanel", "DraftsPanel", "HomePanel", "ReviewPanel",
           "SearchPanel", "TasksPanel", "err_msg"]
