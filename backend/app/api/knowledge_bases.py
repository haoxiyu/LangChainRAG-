"""知识库管理接口。

权限设计:
- 列表/详情:所有登录用户可见(用于展示已收录的资料范围)
- 增/删/改:仅管理员

注:问答检索全部知识库,不经过本接口选库。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.deps import get_current_user, require_admin
from ..db import get_session
from ..models import Chunk, Document, KnowledgeBase, User
from ..schemas.knowledge import (
    KnowledgeBaseCreate,
    KnowledgeBaseOut,
    KnowledgeBaseUpdate,
)
from ..services.cache import get_cache
from ..services.ingestion import document_file_path
from ..services.retriever import invalidate_bm25

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/knowledge-bases", tags=["知识库"])


async def _stats(session: AsyncSession) -> dict[int, tuple[int, int]]:
    """一次性取出所有知识库的文档数与分块数,避免 N+1 查询。"""
    doc_rows = (
        await session.execute(
            select(Document.knowledge_base_id, func.count(Document.id))
            .group_by(Document.knowledge_base_id)
        )
    ).all()
    chunk_rows = (
        await session.execute(
            select(Chunk.knowledge_base_id, func.count(Chunk.id))
            .group_by(Chunk.knowledge_base_id)
        )
    ).all()
    docs = {kb_id: n for kb_id, n in doc_rows}
    chunks = {kb_id: n for kb_id, n in chunk_rows}
    return {kb_id: (docs.get(kb_id, 0), chunks.get(kb_id, 0)) for kb_id in set(docs) | set(chunks)}


def _to_out(kb: KnowledgeBase, stats: tuple[int, int]) -> KnowledgeBaseOut:
    out = KnowledgeBaseOut.model_validate(kb)
    out.document_count, out.chunk_count = stats
    return out


@router.get("", response_model=list[KnowledgeBaseOut], summary="知识库列表")
async def list_knowledge_bases(
    session: AsyncSession = Depends(get_session),
    _: User = Depends(get_current_user),
) -> list[KnowledgeBaseOut]:
    kbs = (await session.execute(select(KnowledgeBase).order_by(KnowledgeBase.id))).scalars().all()
    stats = await _stats(session)
    return [_to_out(kb, stats.get(kb.id, (0, 0))) for kb in kbs]


@router.get("/{kb_id}", response_model=KnowledgeBaseOut, summary="知识库详情")
async def get_knowledge_base(
    kb_id: int,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(get_current_user),
) -> KnowledgeBaseOut:
    kb = await session.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="知识库不存在")
    stats = await _stats(session)
    return _to_out(kb, stats.get(kb_id, (0, 0)))


@router.post("", response_model=KnowledgeBaseOut, status_code=status.HTTP_201_CREATED,
             summary="创建知识库(仅管理员)")
async def create_knowledge_base(
    payload: KnowledgeBaseCreate,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(require_admin),
) -> KnowledgeBaseOut:
    exists = (
        await session.execute(select(KnowledgeBase).where(KnowledgeBase.name == payload.name))
    ).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="知识库名称已存在")

    kb = KnowledgeBase(
        name=payload.name.strip(),
        description=payload.description.strip(),
        created_by=admin.id,
    )
    session.add(kb)
    await session.commit()
    await session.refresh(kb)
    return _to_out(kb, (0, 0))


@router.patch("/{kb_id}", response_model=KnowledgeBaseOut, summary="修改知识库(仅管理员)")
async def update_knowledge_base(
    kb_id: int,
    payload: KnowledgeBaseUpdate,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_admin),
) -> KnowledgeBaseOut:
    kb = await session.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="知识库不存在")

    if payload.name is not None:
        name = payload.name.strip()
        dup = (
            await session.execute(
                select(KnowledgeBase).where(
                    KnowledgeBase.name == name, KnowledgeBase.id != kb_id
                )
            )
        ).scalar_one_or_none()
        if dup is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="知识库名称已存在")
        kb.name = name
    if payload.description is not None:
        kb.description = payload.description.strip()

    await session.commit()
    await session.refresh(kb)
    stats = await _stats(session)
    return _to_out(kb, stats.get(kb_id, (0, 0)))


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除知识库(仅管理员)")
async def delete_knowledge_base(
    kb_id: int,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_admin),
) -> None:
    kb = await session.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="知识库不存在")

    # 先记下文档的落盘路径:CASCADE 只删数据库行,磁盘上的文件不会自己消失,
    # 不主动删就会在 uploads/ 里越攒越多(文档 id 命名,删库后无从追溯)。
    docs = (
        await session.execute(
            select(Document.id, Document.filename).where(
                Document.knowledge_base_id == kb_id
            )
        )
    ).all()

    await session.delete(kb)
    await session.commit()

    # 关联的文档/分块由外键 CASCADE 清理,但磁盘文件、内存索引与缓存必须手动失效
    for doc_id, doc_name in docs:
        try:
            path = document_file_path(doc_id, doc_name)
            if path.exists():
                path.unlink()
        except OSError as e:
            logger.warning("删除知识库时清理文件失败 %s: %s", doc_id, e)

    invalidate_bm25()
    get_cache().invalidate_semantic()
