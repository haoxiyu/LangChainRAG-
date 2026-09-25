"""文档管理接口(仅管理员):上传、列表、删除、分块预览。

上传后的解析与向量化耗时较长,放在后台任务里执行,
接口立刻返回 processing 状态,前端轮询状态即可看到进度。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..core.deps import require_admin
from ..db import get_session, get_sessionmaker
from ..models import Chunk, Document, DocumentStatus, KnowledgeBase, User
from ..schemas.knowledge import ChunkOut, ChunkPage, DocumentOut
from ..services.cache import get_cache
from ..services.ingestion import document_file_path, ingest_document
from ..services.retriever import invalidate_bm25

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/knowledge-bases/{kb_id}/documents", tags=["文档"])


async def _get_kb_or_404(session: AsyncSession, kb_id: int) -> KnowledgeBase:
    kb = await session.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="知识库不存在")
    return kb


async def _ingest_in_background(document_id: int) -> None:
    """后台摄取任务:自己开会话,避免复用已关闭的请求会话。"""
    async with get_sessionmaker()() as session:
        await ingest_document(session, document_id)
    # 新内容入库后,旧的检索索引与语义缓存必须失效
    invalidate_bm25()
    get_cache().invalidate_semantic()


@router.get("", response_model=list[DocumentOut], summary="文档列表(仅管理员)")
async def list_documents(
    kb_id: int,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_admin),
) -> list[Document]:
    await _get_kb_or_404(session, kb_id)
    rows = (
        await session.execute(
            select(Document)
            .where(Document.knowledge_base_id == kb_id)
            .order_by(Document.id.desc())
        )
    ).scalars().all()
    return list(rows)


@router.post("", response_model=DocumentOut, status_code=status.HTTP_201_CREATED,
             summary="上传文档(仅管理员)")
async def upload_document(
    kb_id: int,
    background: BackgroundTasks,
    file: UploadFile = File(..., description="支持 pdf/docx/xlsx/csv/txt/md"),
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(require_admin),
) -> Document:
    await _get_kb_or_404(session, kb_id)

    filename = Path(file.filename or "").name  # 只取文件名,防止路径穿越
    ext = Path(filename).suffix.lower()
    if ext not in settings.allowed_extensions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的文件格式 {ext or '(无扩展名)'},"
                   f"支持: {', '.join(sorted(settings.allowed_extensions))}",
        )

    content = await file.read()
    size = len(content)
    if size == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="文件内容为空")
    if size > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"文件超过 {settings.max_upload_mb}MB 限制",
        )

    # 先落库拿到 id,再用 id 命名文件,省掉一个路径字段
    doc = Document(
        knowledge_base_id=kb_id,
        filename=filename,
        file_type=ext,
        file_size=size,
        status=DocumentStatus.PROCESSING,
        created_by=admin.id,
    )
    session.add(doc)
    await session.commit()
    await session.refresh(doc)

    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    target = document_file_path(doc.id, filename)
    target.write_bytes(content)
    logger.info("已接收上传 %s (%d 字节) -> %s", filename, size, target.name)

    # 解析 + 向量化耗时较长,放后台执行
    background.add_task(_ingest_in_background, doc.id)
    return doc


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="删除文档(仅管理员)")
async def delete_document(
    kb_id: int,
    document_id: int,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_admin),
) -> None:
    doc = await session.get(Document, document_id)
    if doc is None or doc.knowledge_base_id != kb_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在")

    path = document_file_path(doc.id, doc.filename)
    await session.delete(doc)
    await session.commit()

    # 磁盘文件与内存索引同步清理
    try:
        if path.exists():
            path.unlink()
    except OSError as e:
        logger.warning("删除文件失败 %s: %s", path, e)

    invalidate_bm25()
    get_cache().invalidate_semantic()


@router.get("/{document_id}/chunks", response_model=ChunkPage, summary="分块预览(仅管理员)")
async def list_chunks(
    kb_id: int,
    document_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_admin),
) -> ChunkPage:
    doc = await session.get(Document, document_id)
    if doc is None or doc.knowledge_base_id != kb_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在")

    total = (
        await session.execute(
            select(func.count(Chunk.id)).where(Chunk.document_id == document_id)
        )
    ).scalar_one()

    rows = (
        await session.execute(
            select(Chunk)
            .where(Chunk.document_id == document_id)
            .order_by(Chunk.chunk_index)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()

    return ChunkPage(
        items=[ChunkOut.model_validate(c) for c in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/{document_id}/reprocess", response_model=DocumentOut,
             summary="重新处理文档(仅管理员)")
async def reprocess_document(
    kb_id: int,
    document_id: int,
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_admin),
) -> Document:
    """重新解析、分块、向量化。改了分块参数或先前处理失败时很有用。"""
    doc = await session.get(Document, document_id)
    if doc is None or doc.knowledge_base_id != kb_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在")

    doc.status = DocumentStatus.PROCESSING
    doc.error_message = ""
    await session.commit()
    await session.refresh(doc)

    background.add_task(_ingest_in_background, doc.id)
    return doc
