"""ORM 模型聚合导出，确保 ``Base.metadata`` 能收集到全部表。"""

from backend.app.db.models.audit_log import AuditLog
from backend.app.db.models.case import Case, CaseMaterial
from backend.app.db.models.chat_turn import ChatTurn
from backend.app.db.models.citation import Citation
from backend.app.db.models.crawl import CrawlCursor, CrawlDoc, CrawlQuota
from backend.app.db.models.knowledge import (
    DraftRecord,
    DraftVersion,
    KnowledgeDoc,
    StatuteDoc,
    SystemConfig,
    UpdatePack,
)
from backend.app.db.models.task import TaskRecord
from backend.app.db.models.usage_log import UsageLog
from backend.app.db.models.user import Team, User
from backend.app.db.models.wechat import WechatBindCode, WechatBinding

__all__ = [
    "AuditLog",
    "Case",
    "CaseMaterial",
    "ChatTurn",
    "Citation",
    "CrawlCursor",
    "CrawlDoc",
    "CrawlQuota",
    "DraftRecord",
    "DraftVersion",
    "KnowledgeDoc",
    "StatuteDoc",
    "SystemConfig",
    "UpdatePack",
    "TaskRecord",
    "UsageLog",
    "Team",
    "User",
    "WechatBindCode",
    "WechatBinding",
]
