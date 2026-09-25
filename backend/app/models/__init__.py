"""ORM 模型集合。

集中导入,保证 Base.metadata 能看到全部表(建表/迁移依赖此)。
"""

from .chat import Conversation, Message, MessageRole
from .knowledge import Chunk, Document, DocumentStatus, KnowledgeBase
from .user import User, UserRole

__all__ = [
    "User",
    "UserRole",
    "KnowledgeBase",
    "Document",
    "DocumentStatus",
    "Chunk",
    "Conversation",
    "Message",
    "MessageRole",
]
