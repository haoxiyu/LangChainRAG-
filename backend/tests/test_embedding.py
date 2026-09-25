"""向量化的缓存拆分与分批逻辑。

embedding 是整条链路里唯一「按文本量计费」的环节,所以两个不变量直接决定成本:
- 同一段文本第二次出现必须命中缓存,不再请求云端(文档重跑不重复花钱);
- 超过单次上限时必须分批,否则接口会整批拒绝。

还有一个容易忽略但后果严重的:顺序必须与入参一一对应。
向量靠下标与分块配对,顺序一错,检索就会「答非所问」而且完全不报错。

全程不联网:OpenAI 客户端换成假的;缓存用真 AppCache(tmp_path),不写项目真实缓存。
"""

from __future__ import annotations

import pytest

from app.services import embedding
from app.services.cache import AppCache
from app.services.embedding import EMBED_BATCH_SIZE, BailianEmbeddings

pytestmark = pytest.mark.anyio


class _FakeClient:
    """假的 OpenAI 兼容客户端:按文本长度造向量,便于断言哪个文本配了哪个向量。"""

    def __init__(self, dim: int = 3) -> None:
        self.dim = dim
        self.doc_calls: list[list[str]] = []
        self.query_calls: list[str] = []

    def _vec(self, text: str) -> list[float]:
        return [float(len(text))] * self.dim

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.doc_calls.append(list(texts))
        return [self._vec(t) for t in texts]

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        self.doc_calls.append(list(texts))
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        self.query_calls.append(text)
        return self._vec(text)

    async def aembed_query(self, text: str) -> list[float]:
        self.query_calls.append(text)
        return self._vec(text)


@pytest.fixture()
def build(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """返回一个构造函数:给出假客户端,拿到一个「假客户端 + 真缓存」的向量化器。

    必须连 get_cache 一起换掉 —— BailianEmbeddings.__init__ 里会取全局缓存,
    否则用例会往项目真实的 .cache 目录里写东西。
    """

    def _build(client: _FakeClient, enabled: bool = True) -> BailianEmbeddings:
        cache = AppCache(tmp_path / "cache", enabled=enabled)
        # OpenAIEmbeddings 的构造会校验 api_key,这里直接替换掉,避免依赖 .env
        monkeypatch.setattr(embedding, "OpenAIEmbeddings", lambda **kw: client)
        monkeypatch.setattr(embedding, "get_cache", lambda: cache)
        return BailianEmbeddings()

    return _build


# ---------- 缓存拆分 ----------
def test_split_missing_reports_all_when_cache_empty(build) -> None:
    emb = build(_FakeClient())
    results, missing = emb._split_missing(["甲", "乙"])
    assert results == [None, None]
    assert missing == [0, 1]


def test_split_missing_reports_none_when_all_cached(build) -> None:
    emb = build(_FakeClient())
    emb._cache.set_embedding("甲", emb.model, [1.0, 2.0])
    results, missing = emb._split_missing(["甲"])
    assert results == [[1.0, 2.0]]
    assert missing == []


def test_split_missing_reports_only_the_gap(build) -> None:
    """部分命中是常态(改了一段文档再重跑),必须只请求缺的那几条。"""
    emb = build(_FakeClient())
    emb._cache.set_embedding("甲", emb.model, [1.0])
    results, missing = emb._split_missing(["甲", "乙", "丙"])
    assert results[0] == [1.0]
    assert missing == [1, 2]


def test_split_missing_is_namespaced_by_model(build) -> None:
    """换向量模型时必须当作未命中,否则会把上一个模型的向量当成新模型的结果。"""
    emb = build(_FakeClient())
    emb._cache.set_embedding("甲", "old-model", [9.0])
    assert emb._split_missing(["甲"])[1] == [0]


# ---------- 批量与顺序 ----------
def test_empty_input_short_circuits(build) -> None:
    client = _FakeClient()
    emb = build(client)
    assert emb.embed_documents([]) == []
    assert client.doc_calls == []


def test_documents_are_batched(build) -> None:
    """超过单次上限必须分批,否则接口直接拒绝整批。"""
    client = _FakeClient()
    emb = build(client)
    texts = [f"分块{i}" for i in range(EMBED_BATCH_SIZE * 2 + 5)]
    vectors = emb.embed_documents(texts)

    assert [len(c) for c in client.doc_calls] == [EMBED_BATCH_SIZE, EMBED_BATCH_SIZE, 5]
    assert len(vectors) == len(texts)


def test_vectors_stay_aligned_with_input(build) -> None:
    """混合命中/未命中时,结果顺序仍必须与入参一一对应,否则检索会答非所问。"""
    client = _FakeClient()
    emb = build(client)
    emb._cache.set_embedding("甲", emb.model, [111.0, 111.0, 111.0])

    texts = ["甲", "乙乙", "丙丙丙"]
    vectors = emb.embed_documents(texts)

    # 向量值取文本长度,所以 [3 个 111] 说明「甲」来自缓存,[2 个 2] 是「乙乙」算出来的
    assert vectors == [[111.0] * 3, [2.0] * 3, [3.0] * 3]
    # 命中的那条不该出现在请求里
    assert client.doc_calls == [["乙乙", "丙丙丙"]]


def test_second_call_is_fully_served_from_cache(build) -> None:
    """同一段文本第二次出现必须不再请求云端 —— 这是「省钱」的核心断言。"""
    client = _FakeClient()
    emb = build(client)
    first = emb.embed_documents(["七天无理由退货", "电池容量"])
    calls_after_first = len(client.doc_calls)

    second = emb.embed_documents(["七天无理由退货", "电池容量"])
    assert second == first
    assert len(client.doc_calls) == calls_after_first


def test_duplicate_texts_in_one_call_are_not_deduped(build) -> None:
    """一次调用里的重复文本仍会各占一次额度。

    缓存是在「请求回来后」才写入的,所以同一批内的重复文本查不到缓存,会一起发出去。
    结果正确(两条都拿到向量),只是多花一次钱;跨调用(比如文档重跑)才会真正省下来。
    """
    client = _FakeClient()
    emb = build(client)
    vectors = emb.embed_documents(["同样的话", "同样的话"])
    assert vectors[0] == vectors[1]
    assert client.doc_calls == [["同样的话", "同样的话"]]


# ---------- 单条查询 ----------
def test_query_is_cached_after_first_call(build) -> None:
    client = _FakeClient()
    emb = build(client)
    first = emb.embed_query("电池多大")
    assert emb.embed_query("电池多大") == first
    assert client.query_calls == ["电池多大"]


def test_query_and_document_caches_share_namespace(build) -> None:
    """查询词恰好等于某段正文时可以直接复用向量,不必再调一次接口。"""
    client = _FakeClient()
    emb = build(client)
    emb.embed_documents(["电池容量5000mAh"])
    assert emb.embed_query("电池容量5000mAh") == [11.0] * 3
    assert client.query_calls == []


# ---------- 异步 ----------
async def test_async_documents_are_batched_and_ordered(build) -> None:
    client = _FakeClient()
    emb = build(client)
    texts = [f"分块{i}" for i in range(EMBED_BATCH_SIZE + 1)]
    vectors = await emb.aembed_documents(texts)

    assert [len(c) for c in client.doc_calls] == [EMBED_BATCH_SIZE, 1]
    assert vectors[-1] == [float(len(texts[-1]))] * client.dim


async def test_async_empty_input_short_circuits(build) -> None:
    client = _FakeClient()
    assert await build(client).aembed_documents([]) == []
    assert client.doc_calls == []


async def test_async_query_is_cached(build) -> None:
    client = _FakeClient()
    emb = build(client)
    first = await emb.aembed_query("屏幕多大")
    assert await emb.aembed_query("屏幕多大") == first
    assert client.query_calls == ["屏幕多大"]


async def test_async_and_sync_share_the_same_cache(build) -> None:
    """入库走异步、检索走同步时也要复用同一份缓存,否则等于缓存没生效。"""
    client = _FakeClient()
    emb = build(client)
    await emb.aembed_documents(["七天无理由退货"])
    assert emb.embed_query("七天无理由退货") == [7.0] * 3
    assert client.query_calls == []


# ---------- 关闭缓存 ----------
def test_disabled_cache_still_returns_vectors(build) -> None:
    """缓存关掉只是不省钱了,功能不能坏 —— 每条都应真的去请求。"""
    client = _FakeClient()
    emb = build(client, enabled=False)
    emb.embed_documents(["甲"])
    emb.embed_documents(["甲"])
    assert len(client.doc_calls) == 2
