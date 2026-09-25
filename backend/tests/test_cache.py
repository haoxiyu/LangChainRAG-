"""缓存与限流的单元测试。

缓存直接决定「花了多少钱」和「答的是不是旧数据」,限流决定「会不会被刷爆」,
都是企业级要求里能拿分的部分,需要有用例锁住行为。

这里一律直接构造 AppCache(tmp_path),不走全局 get_cache() —— 否则用例会往
项目真实的 .cache 目录里写东西。
"""

from __future__ import annotations

import pytest

from app.services.cache import SEMANTIC_INDEX_LIMIT, AppCache


@pytest.fixture()
def cache(tmp_path) -> AppCache:
    return AppCache(tmp_path / "cache", enabled=True)


def _vec(*xs: float) -> list[float]:
    """构造小维度向量即可:相似度计算与维度无关,小向量让用例更快。"""
    return list(xs)


# ---------- embedding 缓存 ----------
def test_embedding_cache_roundtrip(cache: AppCache) -> None:
    cache.set_embedding("电池容量", "m1", [0.1, 0.2])
    assert cache.get_embedding("电池容量", "m1") == [0.1, 0.2]


def test_embedding_cache_misses_on_different_text_or_model(cache: AppCache) -> None:
    cache.set_embedding("电池容量", "m1", [0.1, 0.2])
    assert cache.get_embedding("屏幕尺寸", "m1") is None
    # 换模型必须换命名空间,否则会把上一个模型的向量当成新模型的结果用
    assert cache.get_embedding("电池容量", "m2") is None


# ---------- 语义缓存 ----------
# 检索跨全部知识库,所以缓存是单一全局索引,不再按库分片。
def test_semantic_hit_on_identical_vector(cache: AppCache) -> None:
    payload = {"query": "电池多大", "answer": "5000mAh", "references": []}
    cache.semantic_store(_vec(1.0, 0.0), payload, ttl=60)
    assert cache.semantic_lookup(_vec(1.0, 0.0), threshold=0.95) == payload


def test_semantic_miss_below_threshold(cache: AppCache) -> None:
    cache.semantic_store(_vec(1.0, 0.0), {"answer": "x"}, ttl=60)
    # 正交向量相似度 0,远低于阈值
    assert cache.semantic_lookup(_vec(0.0, 1.0), threshold=0.95) is None


def test_semantic_threshold_is_respected(cache: AppCache) -> None:
    """相似但不等价的问题不该命中,否则会把别人的答案当成这个问题的答案。"""
    cache.semantic_store(_vec(1.0, 0.0), {"answer": "x"}, ttl=60)
    # 夹角 45°,相似度约 0.707
    assert cache.semantic_lookup(_vec(1.0, 1.0), threshold=0.95) is None
    assert cache.semantic_lookup(_vec(1.0, 1.0), threshold=0.70) is not None


def test_semantic_lookup_on_empty_cache_returns_none(cache: AppCache) -> None:
    assert cache.semantic_lookup(_vec(1.0, 0.0), threshold=0.95) is None


def test_zero_vector_does_not_crash_or_match(cache: AppCache) -> None:
    """全零向量余弦无定义,必须安全返回 None 而不是抛 ZeroDivisionError。"""
    assert cache.semantic_lookup(_vec(0.0, 0.0), threshold=0.95) is None
    cache.semantic_store(_vec(0.0, 0.0), {"answer": "x"}, ttl=60)
    assert cache.semantic_lookup(_vec(1.0, 0.0), threshold=0.95) is None


def test_invalidate_clears_cache(cache: AppCache) -> None:
    """文档变更后必须失效,否则会在 TTL 内一直答旧内容。"""
    cache.semantic_store(_vec(1.0, 0.0), {"answer": "旧答案"}, ttl=600)
    cache.invalidate_semantic()
    assert cache.semantic_lookup(_vec(1.0, 0.0), threshold=0.95) is None


def test_invalidate_clears_every_entry(cache: AppCache) -> None:
    """全局索引意味着一次失效要清干净 —— 漏掉任何一条都是「答旧数据」。"""
    cache.semantic_store(_vec(1.0, 0.0), {"answer": "a"}, ttl=600)
    cache.semantic_store(_vec(0.0, 1.0), {"answer": "b"}, ttl=600)
    cache.invalidate_semantic()
    assert cache.semantic_lookup(_vec(1.0, 0.0), threshold=0.95) is None
    assert cache.semantic_lookup(_vec(0.0, 1.0), threshold=0.95) is None


def test_semantic_index_is_bounded(cache: AppCache) -> None:
    """索引无上限会随问答无限膨胀,遍历相似度也会越来越慢。"""
    for i in range(SEMANTIC_INDEX_LIMIT + 5):
        cache.semantic_store(_vec(1.0, float(i)), {"answer": f"a{i}"}, ttl=600)
    index = cache.get(cache._sem_index_key())
    assert index is not None
    assert len(index) == SEMANTIC_INDEX_LIMIT


# ---------- 限流 ----------
def test_rate_limit_allows_up_to_limit_then_blocks(cache: AppCache) -> None:
    results = [cache.rate_limit("user:1", limit=3) for _ in range(4)]
    assert [ok for ok, _ in results] == [True, True, True, False]
    # 剩余额度递减到 0,不再变成负数
    assert [left for _, left in results] == [2, 1, 0, 0]


def test_rate_limit_is_per_key(cache: AppCache) -> None:
    """按用户/来源分别计数:一个用户刷爆不能连累别人。"""
    for _ in range(3):
        cache.rate_limit("user:1", limit=3)
    assert cache.rate_limit("user:1", limit=3)[0] is False
    assert cache.rate_limit("user:2", limit=3)[0] is True


# ---------- 关闭缓存 ----------
def test_disabled_cache_is_a_safe_noop(tmp_path) -> None:
    disabled = AppCache(tmp_path / "off", enabled=False)
    assert disabled.enabled is False
    # 关掉缓存不能让功能坏掉,只应该「什么都不做」
    disabled.set("k", "v")
    assert disabled.get("k") is None
    disabled.set_embedding("t", "m", [1.0])
    assert disabled.get_embedding("t", "m") is None
    disabled.semantic_store([1.0, 0.0], {"answer": "x"}, ttl=60)
    assert disabled.semantic_lookup([1.0, 0.0], 0.95) is None
    disabled.invalidate_semantic()  # 不应抛异常
    # 关闭时一律放行,否则缓存故障会直接变成服务不可用
    assert disabled.rate_limit("user:1", limit=1) == (True, 1)
    assert disabled.stats() == {"enabled": False}


def test_stats_reports_enabled_cache(cache: AppCache) -> None:
    cache.set("k", "v")
    stats = cache.stats()
    assert stats["enabled"] is True
    assert stats["entries"] >= 1
    assert isinstance(stats["size_bytes"], int)
