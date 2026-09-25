"""知识库与文档相关的请求/响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=1000)


class KnowledgeBaseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1000)


class KnowledgeBaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    embedding_model: str
    created_at: datetime
    document_count: int = 0
    chunk_count: int = 0


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    knowledge_base_id: int
    filename: str
    file_type: str
    file_size: int
    status: str
    chunk_count: int
    error_message: str
    created_at: datetime

    @field_validator("status", mode="before")
    @classmethod
    def status_to_str(cls, v):
        return v.value if hasattr(v, "value") else v


class ChunkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    document_id: int
    chunk_index: int
    content: str
    token_count: int


class ChunkPage(BaseModel):
    """分块预览的分页结果。"""

    items: list[ChunkOut]
    total: int
    page: int
    page_size: int
