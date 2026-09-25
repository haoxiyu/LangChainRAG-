"""会话与消息相关的请求/响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ConversationCreate(BaseModel):
    title: str = Field(default="新对话", max_length=128)


class ConversationUpdate(BaseModel):
    """会话局部更新:只改请求里显式传了的字段。

    因此接口侧必须用 model_dump(exclude_unset=True) 来区分「没传」和「传了 null」。
    """

    title: str | None = Field(default=None, min_length=1, max_length=128)


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    # 检索已全局化,新会话恒为 None。字段保留只为兼容历史数据,前端不再使用。
    knowledge_base_id: int | None = None
    created_at: datetime
    updated_at: datetime
    message_count: int = 0


class ConversationPage(BaseModel):
    """会话列表分页结果。带 total 前端才能显示正确的页码数。"""

    items: list[ConversationOut]
    total: int


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: str
    content: str
    references: list[dict[str, Any]] | None = None
    model: str = ""
    latency_ms: int = 0
    created_at: datetime

    @field_validator("role", mode="before")
    @classmethod
    def role_to_str(cls, v):
        return v.value if hasattr(v, "value") else v


class ChatRequest(BaseModel):
    conversation_id: int | None = Field(
        default=None, description="会话 id;为空则新建会话"
    )
    question: str = Field(min_length=1, max_length=2000)

    @field_validator("question")
    @classmethod
    def not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("问题不能为空")
        return v


class ConversationDetail(BaseModel):
    conversation: ConversationOut
    messages: list[MessageOut]
