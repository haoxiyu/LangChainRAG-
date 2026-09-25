"""管理仪表盘(仅管理员):系统概览与用户列表。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..core.deps import require_admin
from ..db import get_session
from ..models import Chunk, Conversation, Document, DocumentStatus, KnowledgeBase, Message, User
from ..schemas.auth import UserOut
from ..services.cache import get_cache
from ..services.retriever import bm25_index_stats

router = APIRouter(prefix="/api/admin", tags=["管理"])


@router.get("/stats", summary="系统概览(仅管理员)")
async def stats(
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    async def count(model) -> int:
        return (await session.execute(select(func.count(model.id)))).scalar_one()

    doc_by_status = dict(
        (
            await session.execute(
                select(Document.status, func.count(Document.id)).group_by(Document.status)
            )
        ).all()
    )

    # 每个知识库的分块数,用于概览图
    kb_rows = (
        await session.execute(
            select(KnowledgeBase.id, KnowledgeBase.name, func.count(Chunk.id))
            .outerjoin(Chunk, Chunk.knowledge_base_id == KnowledgeBase.id)
            .group_by(KnowledgeBase.id, KnowledgeBase.name)
            .order_by(KnowledgeBase.id)
        )
    ).all()

    chunk_total = await count(Chunk)
    cache = get_cache()
    return {
        "users": await count(User),
        "knowledge_bases": await count(KnowledgeBase),
        "documents": await count(Document),
        "documents_ready": doc_by_status.get(DocumentStatus.READY, 0),
        "documents_processing": doc_by_status.get(DocumentStatus.PROCESSING, 0),
        "documents_failed": doc_by_status.get(DocumentStatus.FAILED, 0),
        "chunks": chunk_total,
        "conversations": await count(Conversation),
        "messages": await count(Message),
        "knowledge_base_breakdown": [
            {"id": r[0], "name": r[1], "chunks": r[2],
             "percent": round(r[2] / chunk_total * 100, 1) if chunk_total else 0.0}
            for r in kb_rows
        ],
        "cache": cache.stats(),
        # 常驻内存的 BM25 索引,用于展示稀疏检索的内存占用情况
        "bm25_indexes": bm25_index_stats(),
    }


@router.get("/upload-limits", summary="上传限制(仅管理员)")
async def upload_limits(_: User = Depends(require_admin)) -> dict[str, Any]:
    """把上传限制交给服务端下发,前端不再自己抄一份。

    两边各写一份的后果是悄悄跑偏:前端曾写死 20MB 而后端是 50MB,20~50MB 的
    文件在浏览器里就被拦下并提示一个错误的上限。真正的校验始终在
    documents.py 的 upload_document 里,这里只是让前端的预检用的是同一份配置。
    """
    return {
        "allowed_extensions": sorted(settings.allowed_extensions),
        "max_upload_mb": settings.max_upload_mb,
    }


@router.get("/users", response_model=list[UserOut], summary="用户列表(仅管理员)")
async def list_users(
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_admin),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> list[User]:
    rows = (
        await session.execute(
            select(User).order_by(User.id).offset(offset).limit(limit)
        )
    ).scalars().all()
    return list(rows)
