"""混合检索:稠密(pgvector)+ 稀疏(BM25)+ RRF 融合 + 重排序。

为什么做混合检索:
- 稠密向量擅长语义相近("手机没电了" ↔ "电池续航"),但对型号、SKU 这类
  精确关键词不敏感
- BM25 恰好相反,对精确词命中强、对同义改写弱
两者互补,融合后召回率显著优于单路。

检索范围是**全部知识库**,而不是某一个库:
用户并不知道(也无从判断)资料被分在哪几个库里,让他先选库等于把检索失败的
责任推给用户 —— 选错库就必然检索为空。这里改为在全部库上召回、由打分排序
决定哪些内容胜出,「命中哪个库」是检索的结果而非提前的决策。

稀疏检索为何放在 Python 而非 PostgreSQL:
PG 原生全文检索对中文分词支持差(需额外 zhparser 扩展),而 jieba + rank_bm25
零依赖即可用。索引常驻内存,内容变更时失效重建。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from typing import Any

import jieba
from langchain_core.documents import Document
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from .embedding import get_embeddings
from .reranker import get_reranker

logger = logging.getLogger(__name__)

# 超过该分块数时给出告警 —— 全量载入内存的方案在这个量级开始不划算
BM25_MEMORY_WARN_CHUNKS = 50_000


@dataclass
class _BM25Index:
    """全库共用的稀疏检索索引。"""

    chunk_ids: list[int]
    metadatas: list[dict[str, Any]]
    contents: list[str]
    bm25: Any = None
    _tokenized: list[list[str]] = field(default_factory=list, repr=False)


_global_index: _BM25Index | None = None
_index_lock = threading.Lock()


def _tokenize(text: str) -> list[str]:
    """中文分词。lcut_for_search 会额外切出子词,提升召回。"""
    return [t for t in jieba.lcut_for_search(text.lower()) if t.strip()]


def invalidate_bm25() -> None:
    """任意知识库内容变更后调用,下次检索时重建索引。

    索引已全局化,无法只失效某个库的部分 —— 但重建成本与库数无关,
    只是把同一批数据再读一遍,代价可以接受。
    """
    global _global_index
    with _index_lock:
        _global_index = None
    logger.info("已失效全局 BM25 索引")


def bm25_index_stats() -> list[dict[str, int]]:
    """当前常驻内存的稀疏索引概览,供管理端展示。"""
    with _index_lock:
        if _global_index is None:
            return []
        return [{"chunks": len(_global_index.contents)}]


async def _load_bm25_index(session: AsyncSession) -> _BM25Index:
    """加载并构建全库 BM25 索引(带内存缓存)。"""
    global _global_index
    with _index_lock:
        cached = _global_index
    if cached is not None:
        return cached

    rows = (
        await session.execute(
            text(
                "SELECT c.id, c.document_id, c.content, c.chunk_index, d.filename, "
                "c.knowledge_base_id, kb.name "
                "FROM chunks c "
                "JOIN documents d ON d.id = c.document_id "
                "JOIN knowledge_bases kb ON kb.id = c.knowledge_base_id "
                "WHERE d.status = 'ready' "
                "ORDER BY c.id"
            )
        )
    ).all()

    idx = _BM25Index(
        chunk_ids=[r[0] for r in rows],
        metadatas=[
            {
                "chunk_id": r[0],
                "document_id": r[1],
                "filename": r[4],
                "chunk_index": r[3],
                # 引用卡片要显示来源库:不同库可能有同名文档,只报文件名无从溯源
                "knowledge_base_id": r[5],
                "knowledge_base_name": r[6],
            }
            for r in rows
        ],
        contents=[r[2] for r in rows],
    )

    if len(idx.contents) > BM25_MEMORY_WARN_CHUNKS:
        logger.warning(
            "全库分块数 %d 超过 %d,内存 BM25 检索效率将下降,"
            "生产环境应改用专用全文检索引擎",
            len(idx.contents), BM25_MEMORY_WARN_CHUNKS,
        )

    if idx.contents:
        from rank_bm25 import BM25Okapi

        idx._tokenized = [_tokenize(c) for c in idx.contents]
        idx.bm25 = BM25Okapi(idx._tokenized)

    with _index_lock:
        _global_index = idx
    logger.info("全局 BM25 索引构建完成,%d 个分块", len(idx.contents))
    return idx


def _sparse_search(idx: _BM25Index, query: str, top_k: int) -> list[tuple[int, float]]:
    """BM25 打分,返回 (分块下标, 分数) 降序。"""
    if idx.bm25 is None or not idx.contents:
        return []
    scores = idx.bm25.get_scores(_tokenize(query))
    ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
    # 过滤掉 0 分(完全无词命中),否则会往融合结果里灌噪声
    return [(i, float(s)) for i, s in ranked[:top_k] if s > 0]


def _rrf_fuse(
    ranked_lists: list[list[int]], k: int = 60, weights: list[float] | None = None
) -> dict[int, float]:
    """倒数排名融合。用排名而非原始分数,避免两路分数量纲不可比的问题。"""
    if weights is None:
        weights = [1.0] * len(ranked_lists)
    fused: dict[int, float] = {}
    for lst, w in zip(ranked_lists, weights):
        for rank, idx in enumerate(lst):
            fused[idx] = fused.get(idx, 0.0) + w / (k + rank + 1)
    return fused


def _format_vector(vec: list[float]) -> str:
    """pgvector 接受的字符串字面量形式。"""
    return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"


async def _dense_search(
    session: AsyncSession, query_vector: list[float], top_k: int
) -> list[tuple[int, float]]:
    """pgvector 近邻检索,返回 (分块 id, 相似度) 降序。"""
    vec = _format_vector(query_vector)
    rows = (
        await session.execute(
            text(
                "SELECT c.id, 1 - (c.embedding <=> CAST(:vec AS vector)) AS score "
                "FROM chunks c JOIN documents d ON d.id = c.document_id "
                "WHERE d.status = 'ready' "
                "ORDER BY c.embedding <=> CAST(:vec AS vector) "
                "LIMIT :k"
            ),
            {"vec": vec, "k": top_k},
        )
    ).all()
    return [(r[0], float(r[1])) for r in rows]


async def _dense_task(query_vector: list[float], top_k: int) -> list[tuple[int, float]]:
    """独立的稠密检索任务(自带会话,便于与稀疏路并发)。"""
    from ..db import get_sessionmaker

    async with get_sessionmaker()() as session:
        return await _dense_search(session, query_vector, top_k)


async def _sparse_task(
    query: str, top_k: int
) -> tuple[_BM25Index, list[tuple[int, float]]]:
    """独立的稀疏检索任务(自带会话,便于与稠密路并发)。"""
    from ..db import get_sessionmaker

    async with get_sessionmaker()() as session:
        idx = await _load_bm25_index(session)
    # BM25 打分是 CPU 密集的同步计算,放到线程里避免阻塞事件循环
    hits = await asyncio.to_thread(_sparse_search, idx, query, top_k)
    return idx, hits


async def hybrid_retrieve(
    session: AsyncSession,
    query: str,
    top_k: int | None = None,
    use_rerank: bool | None = None,
) -> list[Document]:
    """在全部知识库上执行混合检索,返回按相关度排序的 Document 列表。

    注意:两路召回各自开独立会话并发执行 —— AsyncSession 不是并发安全的,
    共用一个会话会让 SQLAlchemy 抛 "concurrent operations" 错误。
    传入的 session 仅用于本函数外层已持有的事务上下文。
    """
    top_k = top_k or settings.rerank_top_k
    use_rerank = settings.enable_rerank if use_rerank is None else use_rerank
    recall_k = settings.retrieve_top_k

    if not query.strip():
        return []

    # ---------- 1. 两路召回并行(独立会话 + 独立线程池) ----------
    query_vector = await get_embeddings().aembed_query(query)
    dense_hits, (idx, sparse_hits) = await asyncio.gather(
        _dense_task(query_vector, recall_k),
        _sparse_task(query, recall_k),
    )

    logger.info("召回: 稠密 %d 条,稀疏 %d 条", len(dense_hits), len(sparse_hits))

    # ---------- 2. 用分块 id 对齐到索引下标 ----------
    id_to_pos = {cid: pos for pos, cid in enumerate(idx.chunk_ids)}
    dense_ranked = [id_to_pos[cid] for cid, _ in dense_hits if cid in id_to_pos]
    sparse_ranked = [pos for pos, _ in sparse_hits]

    if not dense_ranked and not sparse_ranked:
        return []

    # ---------- 3. RRF 融合 ----------
    fused = _rrf_fuse([dense_ranked, sparse_ranked], k=settings.rrf_k)
    # 融合后多取一些候选交给重排,给重排模型更大的挑选空间
    candidate_count = min(max(top_k * 4, top_k), len(fused))
    top_positions = sorted(fused, key=lambda p: fused[p], reverse=True)[:candidate_count]

    dense_score_by_pos = {id_to_pos[cid]: s for cid, s in dense_hits if cid in id_to_pos}
    candidates = [
        Document(
            page_content=idx.contents[pos],
            metadata={
                **idx.metadatas[pos],
                "rrf_score": fused[pos],
                "dense_score": dense_score_by_pos.get(pos),
            },
        )
        for pos in top_positions
    ]

    # ---------- 4. 重排序 ----------
    if use_rerank and len(candidates) > 1:
        reranker = get_reranker(top_k)
        candidates = list(await reranker.acompress_documents(candidates, query))

    results = candidates[:top_k]
    logger.info("检索完成: 候选 %d 条 → 最终 %d 条", len(fused), len(results))
    return results
