"""混合检索编排层的单元测试:两路对齐、融合取候选、重排与截断。

test_retrieval.py 覆盖的是内部算子(RRF 公式、分词、稀疏打分),
这里覆盖的是**把它们串起来的编排逻辑**,而编排里藏着几个不报错但会答错的坑:

- 稠密路返回的是「分块 id」,稀疏路返回的是「索引下标」,两套坐标必须对齐;
  若某个分块在索引快照里不存在(索引建好之后又入库了新分块),必须丢弃而不是
  按错误的坐标取正文 —— 否则会把 A 商品的内容当成 B 商品的引用返回。
- 送进重排的候选数要多于最终条数,重排才有挑选空间;但也不能超过融合结果总数。
- 重排只在候选多于 1 条时才有意义,单独一条候选不该白花一次接口调用。

检索范围是全部知识库(调用方不再传知识库 id),来源库名要随 metadata 透出。

两路召回与重排全部替换为假对象,所以本文件不连库、不联网。
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.documents import Document

from app.config import settings
from app.services import retriever
from app.services.retriever import _BM25Index, hybrid_retrieve

pytestmark = pytest.mark.anyio


# ---------- 测试替身 ----------
class _FakeEmbeddings:
    def __init__(self, vector: list[float] | None = None) -> None:
        self.vector = vector or [0.1, 0.2]
        self.queries: list[str] = []

    async def aembed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return self.vector


class _FakeReranker:
    """假重排器。

    mode="reverse":把候选倒序,用来证明「重排确实生效了」;
    mode="identity":原顺序返回,用来模拟接口失败时的降级行为。
    """

    def __init__(self, mode: str = "reverse") -> None:
        assert mode in ("reverse", "identity")
        self.mode = mode
        self.calls = 0
        self.received: list[Document] = []
        self.queries: list[str] = []

    async def acompress_documents(
        self, documents: list[Document], query: str
    ) -> list[Document]:
        self.calls += 1
        self.received = list(documents)
        self.queries.append(query)
        return list(documents) if self.mode == "identity" else list(reversed(documents))


def _index(size: int, first_id: int = 100) -> _BM25Index:
    """构造一个有 size 个分块的索引:分块 id 从 first_id 起,正文为「分块N」。"""
    return _BM25Index(
        chunk_ids=[first_id + i for i in range(size)],
        metadatas=[
            {
                "chunk_id": first_id + i,
                "document_id": 1,
                "filename": "手机.md",
                "knowledge_base_id": 1,
                "knowledge_base_name": "星辰X1商品知识库",
                "chunk_index": i,
            }
            for i in range(size)
        ],
        contents=[f"分块{i}" for i in range(size)],
    )


@pytest.fixture()
def wire(monkeypatch: pytest.MonkeyPatch):
    """装配假的两路召回与重排,返回本次用到的所有假对象。"""

    def _wire(
        index: _BM25Index,
        dense_hits: list[tuple[int, float]] | None = None,
        sparse_hits: list[tuple[int, float]] | None = None,
        reranker: _FakeReranker | None = None,
    ) -> dict[str, Any]:
        embeddings = _FakeEmbeddings()
        reranker = reranker or _FakeReranker()

        async def _dense(_vec: list[float], _k: int) -> list[tuple[int, float]]:
            return list(dense_hits or [])

        async def _sparse(_q: str, _k: int) -> tuple[_BM25Index, list[tuple[int, float]]]:
            return index, list(sparse_hits or [])

        monkeypatch.setattr(retriever, "get_embeddings", lambda: embeddings)
        monkeypatch.setattr(retriever, "_dense_task", _dense)
        monkeypatch.setattr(retriever, "_sparse_task", _sparse)
        monkeypatch.setattr(retriever, "get_reranker", lambda *a, **kw: reranker)
        return {"embeddings": embeddings, "reranker": reranker}

    return _wire


# ---------- 短路 ----------
@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
async def test_blank_query_short_circuits(wire, blank: str) -> None:
    """空问题不该算向量、不该查库 —— 一次无意义的云端调用也是钱。"""
    state = wire(_index(3), dense_hits=[(100, 0.9)])
    assert await hybrid_retrieve(None, blank) == []
    assert state["embeddings"].queries == []


async def test_no_hits_on_either_route_returns_empty(wire) -> None:
    """两路都没召回就返回空,交给上层走「未找到」的兜底提示词。"""
    wire(_index(3))
    assert await hybrid_retrieve(None, "电池多大") == []


# ---------- 坐标对齐 ----------
async def test_dense_hits_missing_from_index_are_dropped(wire) -> None:
    """索引快照里没有的分块必须丢弃。

    稠密路查的是 chunks 表,稀疏路查的是内存索引;索引建好之后又入库的分块
    只在前者里出现。按 id 直接当坐标用会取到完全不相干的正文。
    """
    index = _BM25Index(
        chunk_ids=[101, 102, 103],
        metadatas=[{"chunk_id": i, "filename": "手机.md"} for i in (101, 102, 103)],
        contents=["甲", "乙", "丙"],
    )
    # 999 不在索引里(应被丢弃);101 对应下标 0,两路都排第一,融合后必然领先
    state = wire(index, dense_hits=[(999, 0.99), (101, 0.90)], sparse_hits=[(0, 5.0), (2, 1.0)])

    docs = await hybrid_retrieve(None, "电池", use_rerank=False)

    assert [d.metadata["chunk_id"] for d in docs] == [101, 103]
    assert [d.page_content for d in docs] == ["甲", "丙"]
    # 被丢弃的那条不能以任何形式出现在结果里
    assert 999 not in state["reranker"].received
    # 稠密分只写给真正被稠密路召回的那条
    assert docs[0].metadata["dense_score"] == 0.90
    assert docs[1].metadata["dense_score"] is None


async def test_documents_carry_scores_and_metadata(wire) -> None:
    """引用卡片要展示来源与分数,所以正文与 metadata 都得从索引里带出来。"""
    wire(_index(3), dense_hits=[(100, 0.88)], sparse_hits=[(0, 2.5)])

    docs = await hybrid_retrieve(None, "电池", use_rerank=False)
    doc = docs[0]

    assert doc.metadata["chunk_id"] == 100
    assert doc.metadata["filename"] == "手机.md"
    assert doc.metadata["chunk_index"] == 0
    assert doc.metadata["dense_score"] == 0.88
    assert doc.metadata["rrf_score"] > 0


async def test_documents_carry_source_knowledge_base(wire) -> None:
    """检索跨全部知识库,引用必须能说清内容出自哪个库。

    不同库可能有同名文档(比如各库都有一份「售后政策.md」),只报文件名
    用户无从核对,答辩时也答不上「这条是哪来的」。
    """
    wire(_index(3), dense_hits=[(100, 0.9)])

    docs = await hybrid_retrieve(None, "电池", use_rerank=False)

    assert docs[0].metadata["knowledge_base_id"] == 1
    assert docs[0].metadata["knowledge_base_name"] == "星辰X1商品知识库"


async def test_dense_score_is_none_for_sparse_only_hits(wire) -> None:
    """只在稀疏路出现的分块没有稠密分数,前端据此不展示该列。"""
    wire(_index(3), sparse_hits=[(1, 2.0)])
    docs = await hybrid_retrieve(None, "电池", use_rerank=False)
    assert docs[0].metadata["dense_score"] is None
    assert docs[0].metadata["chunk_id"] == 101


async def test_both_routes_agreeing_ranks_first(wire) -> None:
    """两路都排第一的分块必须排到只有一路召回的前面 —— 融合的意义所在。"""
    wire(_index(3), dense_hits=[(100, 0.9), (101, 0.8)], sparse_hits=[(0, 5.0), (2, 1.0)])

    docs = await hybrid_retrieve(None, "电池", use_rerank=False)
    assert docs[0].metadata["chunk_id"] == 100
    assert docs[-1].metadata["chunk_id"] == 102


# ---------- 候选数与截断 ----------
async def test_candidate_pool_is_wider_than_final_top_k(wire) -> None:
    """送进重排的候选要多于最终条数,重排才有挑选空间。"""
    state = wire(_index(30), dense_hits=[(100 + i, 0.9 - i * 0.01) for i in range(30)])

    docs = await hybrid_retrieve(None, "电池", top_k=5)

    assert len(state["reranker"].received) == 20  # max(top_k*4, top_k)
    assert len(docs) == 5


async def test_candidate_pool_never_exceeds_fused_count(wire) -> None:
    """召回结果本身很少时,候选数不能超过实际条数。"""
    state = wire(_index(3), dense_hits=[(100, 0.9), (101, 0.8)])

    await hybrid_retrieve(None, "电池", top_k=5)
    assert len(state["reranker"].received) == 2


async def test_results_are_truncated_to_top_k(wire) -> None:
    wire(_index(10), dense_hits=[(100 + i, 0.9 - i * 0.01) for i in range(10)])
    docs = await hybrid_retrieve(None, "电池", top_k=3)
    assert len(docs) == 3


async def test_top_k_defaults_to_settings(wire) -> None:
    wire(_index(30), dense_hits=[(100 + i, 0.9) for i in range(30)])
    docs = await hybrid_retrieve(None, "电池")
    assert len(docs) == settings.rerank_top_k


# ---------- 重排 ----------
async def test_rerank_reorders_and_truncates(wire) -> None:
    """重排把最相关的候选提到最前,再截断到 top_k。"""
    state = wire(_index(4), dense_hits=[(100 + i, 0.9 - i * 0.1) for i in range(4)])
    docs = await hybrid_retrieve(None, "电池", top_k=2)

    assert state["reranker"].calls == 1
    assert state["reranker"].queries == ["电池"]
    # 假重排器倒序,所以原本排最后的「分块3」应当成为第一条
    assert docs[0].page_content == "分块3"


async def test_rerank_is_skipped_for_single_candidate(wire) -> None:
    """只有一条候选时重排没有意义,不该白花一次接口调用。"""
    state = wire(_index(3), dense_hits=[(101, 0.9)])
    docs = await hybrid_retrieve(None, "电池")
    assert state["reranker"].calls == 0
    assert [d.page_content for d in docs] == ["分块1"]


async def test_rerank_can_be_disabled(wire) -> None:
    """关掉重排是省钱的降级开关,此时应保持融合排序。"""
    state = wire(_index(4), dense_hits=[(100 + i, 0.9 - i * 0.1) for i in range(4)])
    docs = await hybrid_retrieve(None, "电池", use_rerank=False)

    assert state["reranker"].calls == 0
    assert docs[0].page_content == "分块0"  # 稠密路排第一的仍在最前


async def test_rerank_failure_does_not_lose_candidates(wire) -> None:
    """重排降级成原顺序时,既不能丢候选,也不能把顺序弄乱。"""
    state = wire(
        _index(4),
        dense_hits=[(100 + i, 0.9 - i * 0.1) for i in range(4)],
        reranker=_FakeReranker(mode="identity"),
    )
    docs = await hybrid_retrieve(None, "电池", top_k=3)

    assert state["reranker"].calls == 1
    assert len(docs) == 3
    assert docs[0].page_content == "分块0"
