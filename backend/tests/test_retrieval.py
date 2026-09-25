"""混合检索里「可离线验证」的那部分:RRF 融合、分词、稀疏打分、索引失效。

联网的部分(向量生成、rerank 调用)不在这里测,交给 scripts/e2e_check.py。
"""

from __future__ import annotations

import pytest

from app.services.retriever import (
    _BM25Index,
    _format_vector,
    _rrf_fuse,
    _sparse_search,
    _tokenize,
    bm25_index_stats,
    invalidate_bm25,
)
from app.services import retriever

K = 60


def _expected(rank: int, weight: float = 1.0) -> float:
    return weight / (K + rank + 1)


# ---------- RRF 融合 ----------
def test_rrf_single_list_preserves_order() -> None:
    fused = _rrf_fuse([[0, 1, 2]], k=K)
    assert fused[0] > fused[1] > fused[2]


def test_rrf_matches_manual_computation() -> None:
    """公式必须与论文一致:sum(1 / (k + rank + 1))。"""
    fused = _rrf_fuse([[0, 1, 2], [2, 0]], k=K)
    assert fused[0] == pytest.approx(_expected(0) + _expected(1))
    assert fused[1] == pytest.approx(_expected(1))
    assert fused[2] == pytest.approx(_expected(2) + _expected(0))


def test_rrf_rewards_documents_found_by_both_routes() -> None:
    """两路都召回的文档应排在只有一路召回的前面 —— 这正是融合的意义。"""
    # 1 号只被稠密路排第一,0 号被两路都召回
    fused = _rrf_fuse([[0, 1], [0]], k=K)
    assert fused[0] > fused[1]


def test_rrf_uses_rank_not_raw_score() -> None:
    """两路分数量纲不可比(余弦 ∈ [-1,1],BM25 无上界),所以只看排名。

    因此同样的排名必然得到同样的分数,即使实际分数差得很远。
    """
    assert _rrf_fuse([[0, 1]], k=K) == _rrf_fuse([[0, 1]], k=K)


def test_rrf_respects_weights() -> None:
    """加权后可以让某一路主导,便于按语料调参。"""
    fused = _rrf_fuse([[0], [1]], k=K, weights=[3.0, 1.0])
    assert fused[0] == pytest.approx(_expected(0, 3.0))
    assert fused[1] == pytest.approx(_expected(0, 1.0))
    assert fused[0] > fused[1]


def test_rrf_handles_empty_and_duplicate_inputs() -> None:
    assert _rrf_fuse([], k=K) == {}
    assert _rrf_fuse([[], []], k=K) == {}
    # 同一路里重复出现会累加,不抛异常
    assert _rrf_fuse([[0, 0]], k=K)[0] == pytest.approx(_expected(0) + _expected(1))


# ---------- 分词 ----------
def test_tokenize_splits_chinese_and_is_case_insensitive() -> None:
    tokens = _tokenize("电池容量 mAh")
    assert all(t == t.lower() for t in tokens)
    assert any("电池" in t for t in tokens)
    assert any("mah" in t for t in tokens)


def test_tokenize_drops_blank_tokens() -> None:
    """空白 token 会污染词表并让 BM25 给标点打分。"""
    tokens = _tokenize("电池。容量 ,  ")
    assert tokens
    assert all(t.strip() for t in tokens)


# ---------- 稀疏打分 ----------
class _StubBM25:
    """固定打分的假 BM25,用来验证「过滤 0 分」这段逻辑。"""

    def __init__(self, scores: list[float]) -> None:
        self._scores = scores

    def get_scores(self, _tokens: list[str]) -> list[float]:
        return self._scores


def _index_with_scores(scores: list[float]) -> _BM25Index:
    idx = _BM25Index(
        chunk_ids=list(range(len(scores))),
        metadatas=[{"chunk_id": i} for i in range(len(scores))],
        contents=[f"分块{i}" for i in range(len(scores))],
    )
    idx.bm25 = _StubBM25(scores)
    return idx


def test_sparse_search_returns_descending_hits() -> None:
    idx = _index_with_scores([0.5, 3.0, 1.0])
    hits = _sparse_search(idx, "电池", top_k=3)
    assert [pos for pos, _ in hits] == [1, 2, 0]


def test_sparse_search_filters_zero_scores() -> None:
    """0 分表示一个词都没命中,放进融合结果就是纯噪声。"""
    idx = _index_with_scores([0.0, 2.0, 0.0])
    assert _sparse_search(idx, "电池", top_k=3) == [(1, 2.0)]


def test_sparse_search_respects_top_k() -> None:
    idx = _index_with_scores([5.0, 4.0, 3.0])
    assert len(_sparse_search(idx, "电池", top_k=2)) == 2


def test_sparse_search_without_bm25_returns_empty() -> None:
    """知识库还没入库(没有分块)时索引里没有 BM25 对象,应当安静返回空。"""
    idx = _BM25Index(chunk_ids=[], metadatas=[], contents=[])
    assert idx.bm25 is None
    assert _sparse_search(idx, "电池", top_k=5) == []


# ---------- 索引生命周期 ----------
# 索引是全局单例:检索跨全部知识库,不再按库各存一份。
def test_invalidate_bm25_drops_the_global_index() -> None:
    retriever._global_index = _BM25Index(chunk_ids=[1], metadatas=[{}], contents=["x"])
    try:
        assert bm25_index_stats() == [{"chunks": 1}]
        invalidate_bm25()
        assert bm25_index_stats() == []
    finally:
        # 别把状态留给后面的用例
        retriever._global_index = None


def test_invalidate_before_first_build_is_safe() -> None:
    """首次检索前就删文档(索引还没建)不应抛异常。"""
    retriever._global_index = None
    invalidate_bm25()
    assert bm25_index_stats() == []


def test_stats_report_no_index_before_first_build() -> None:
    """管理端靠这个区分「还没建」与「建好了但为空」。"""
    retriever._global_index = None
    assert bm25_index_stats() == []


# ---------- 向量字面量 ----------
def test_format_vector_produces_pgvector_literal() -> None:
    """pgvector 只认 [a,b,c] 这种写法,维度或括号错了就是整条检索报错。"""
    assert _format_vector([0.1, 1.0]) == "[0.10000000,1.00000000]"


def test_format_vector_keeps_element_count() -> None:
    """元素个数必须与 vector(1024) 一致,少一个就是维度不匹配的硬错误。"""
    text = _format_vector([0.0] * 1024)
    assert text.count(",") == 1023
    assert text.startswith("[") and text.endswith("]")


def test_format_vector_is_deterministic() -> None:
    """同样的向量必须拼出同样的字符串,否则缓存与日志都无法比对。"""
    vec = [0.123456789, -0.987654321]
    assert _format_vector(vec) == _format_vector(list(vec))


def test_format_vector_avoids_scientific_notation() -> None:
    """极小值不退化成一串科学计数法,拼接结果始终是定点小数。"""
    text = _format_vector([1e-12, -1e-12, 0.0])
    assert "e" not in text.lower()
    assert text == "[0.00000000,-0.00000000,0.00000000]"


def test_format_vector_on_empty_input() -> None:
    assert _format_vector([]) == "[]"
