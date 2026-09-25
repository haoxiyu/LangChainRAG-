"""重排序封装的单元测试:请求体、下标回映射、失败降级。

重排是「企业级优化」里性价比最高的一环,但它有三处容易出错又不容易发现:
- 请求体写错(漏 top_n、误开 return_documents)→ 白花钱或响应体积暴涨;
- 接口返回的是「原文档下标」,映射错了会把 A 商品的内容标成 B 商品的引用,
  而前端看起来一切正常,只有人工核对才发现;
- 接口超时/报错时若直接抛异常,整条问答链路都会挂掉。

第三条是可用性底线:重排只是增强环节,失败必须降级为原顺序。
全程不联网:httpx 客户端换成假的。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from langchain_core.documents import Document

from app.services.reranker import BailianReranker, get_reranker

pytestmark = pytest.mark.anyio


@pytest.fixture()
def docs() -> list[Document]:
    """每个用例一份全新的 Document。

    _apply 会**就地**给 Document 写 rerank_score,模块级共用一份列表会让
    「上一次成功重排」的分数漏进「这一次降级」的断言里。
    """
    return [
        Document(page_content="商品支持七天无理由退货,运费卖家承担", metadata={"chunk_id": 1}),
        Document(page_content="这款手机电池容量5000mAh,支持67W快充", metadata={"chunk_id": 2}),
        Document(page_content="屏幕6.7英寸AMOLED,120Hz刷新率", metadata={"chunk_id": 3}),
    ]


def _reranker(**kwargs: Any) -> BailianReranker:
    return BailianReranker(api_key="test-key", **kwargs)


# ---------- 测试替身 ----------
class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


def _results(*pairs: tuple[int, float]) -> _FakeResponse:
    """构造百炼 rerank 的响应体:{output: {results: [{index, relevance_score}]}}。"""
    return _FakeResponse(
        {"output": {"results": [
            {"index": i, "relevance_score": s} for i, s in pairs
        ]}}
    )


class _SyncClient:
    """假 httpx.Client:记录请求,按脚本返回或抛错。"""

    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.response, self.error = response, error
        self.requests: list[dict[str, Any]] = []

    def __enter__(self) -> "_SyncClient":
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False

    def post(self, url: str, json: Any = None, headers: Any = None) -> Any:
        self.requests.append({"url": url, "json": json, "headers": headers})
        if self.error is not None:
            raise self.error
        return self.response


class _AsyncClient(_SyncClient):
    """异步版。post 是协程,所以不能和同步那个共用一个类。"""

    async def __aenter__(self) -> "_AsyncClient":  # type: ignore[override]
        return self

    async def __aexit__(self, *_exc: Any) -> bool:  # type: ignore[override]
        return False

    async def post(self, url: str, json: Any = None, headers: Any = None) -> Any:  # type: ignore[override]
        return super().post(url, json=json, headers=headers)


def _install_sync(monkeypatch: pytest.MonkeyPatch, client: _SyncClient) -> _SyncClient:
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: client)
    return client


def _install_async(monkeypatch: pytest.MonkeyPatch, client: _AsyncClient) -> _AsyncClient:
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: client)
    return client


# ---------- 请求构造 ----------
def test_endpoint_uses_native_api_path() -> None:
    """百炼 rerank 只在原生接口提供,兼容模式会 404 —— 路径写错是最常见的坑。"""
    rr = _reranker(native_base="https://dashscope.aliyuncs.com/api/v1/services")
    assert rr._endpoint.endswith("/rerank/text-rerank/text-rerank")
    assert "compatible-mode" not in rr._endpoint


def test_payload_asks_for_scores_only(docs: list[Document]) -> None:
    """不要原文:减少响应体积,正文本来就在本地文档里。"""
    rr = _reranker(model_name="qwen3.7-text-rerank", top_n=2)
    payload = rr._payload("电池多大", [d.page_content for d in docs])
    assert payload["model"] == "qwen3.7-text-rerank"
    assert payload["input"]["query"] == "电池多大"
    assert payload["input"]["documents"] == [d.page_content for d in docs]
    assert payload["parameters"]["return_documents"] is False


def test_payload_clamps_top_n_to_document_count(docs: list[Document]) -> None:
    """候选比 top_n 少时传大值会被接口拒绝,必须取 min。"""
    texts = [d.page_content for d in docs]
    assert _reranker(top_n=10)._payload("q", texts)["parameters"]["top_n"] == 3
    # top_n 更小则以配置为准
    assert _reranker(top_n=1)._payload("q", texts)["parameters"]["top_n"] == 1


def test_headers_carry_bearer_token() -> None:
    headers = _reranker()._headers()
    assert headers["Authorization"].startswith("Bearer ")
    assert headers["Content-Type"] == "application/json"


# ---------- 下标回映射 ----------
def test_apply_maps_index_back_to_document(docs: list[Document]) -> None:
    out = BailianReranker._apply(
        [{"index": 2, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.5}], docs
    )
    assert [d.metadata["chunk_id"] for d in out] == [3, 1]
    assert out[0].metadata["rerank_score"] == 0.9


def test_apply_preserves_existing_metadata() -> None:
    """原 metadata 里有分块 id、文件名等,重排不能把它们冲掉。"""
    only = [Document(page_content="x", metadata={"chunk_id": 7, "filename": "a.md"})]
    out = BailianReranker._apply([{"index": 0, "relevance_score": 0.1}], only)
    assert out[0].metadata == {"chunk_id": 7, "filename": "a.md", "rerank_score": 0.1}


def test_apply_skips_out_of_range_and_missing_index(docs: list[Document]) -> None:
    """接口返回脏下标时不能 IndexError,也不能把别的文档塞进引用。"""
    out = BailianReranker._apply(
        [{"index": 99, "relevance_score": 0.9}, {"relevance_score": 0.8}, {"index": 1}], docs
    )
    assert [d.metadata["chunk_id"] for d in out] == [2]
    assert out[0].metadata["rerank_score"] is None


def test_apply_with_no_results_returns_empty(docs: list[Document]) -> None:
    assert BailianReranker._apply([], docs) == []


# ---------- 降级 ----------
def test_empty_documents_short_circuit(monkeypatch: pytest.MonkeyPatch) -> None:
    """空候选不该发请求 —— 接口对空 documents 会报错。"""
    client = _install_sync(monkeypatch, _SyncClient(response=_results()))
    assert _reranker().compress_documents([], "q") == []
    assert client.requests == []


def test_sync_failure_falls_back_to_original_order(
    monkeypatch: pytest.MonkeyPatch, docs: list[Document]
) -> None:
    _install_sync(monkeypatch, _SyncClient(error=httpx.ConnectError("boom")))
    out = _reranker().compress_documents(docs, "电池多大")
    assert out == docs
    assert "rerank_score" not in out[0].metadata


def test_sync_http_status_error_falls_back(
    monkeypatch: pytest.MonkeyPatch, docs: list[Document]
) -> None:
    """401/429 这类状态码由 raise_for_status 抛出,同样要降级而不是中断问答。"""
    class _Bad(_SyncClient):
        def post(self, url: str, json: Any = None, headers: Any = None) -> Any:
            raise httpx.HTTPStatusError("401", request=None, response=None)  # type: ignore[arg-type]

    _install_sync(monkeypatch, _Bad())
    assert _reranker().compress_documents(docs, "电池多大") == docs


def test_sync_success_sends_content_and_reorders(
    monkeypatch: pytest.MonkeyPatch, docs: list[Document]
) -> None:
    client = _install_sync(
        monkeypatch, _SyncClient(response=_results((1, 0.95), (0, 0.30), (2, 0.10)))
    )
    out = _reranker(top_n=3).compress_documents(docs, "电池多大")

    # 送上去的是正文本身,不是分块 id
    assert client.requests[0]["json"]["input"]["documents"] == [d.page_content for d in docs]
    # 最相关的「电池容量」被排到第一
    assert out[0].page_content.startswith("这款手机电池容量")
    assert [d.metadata["rerank_score"] for d in out] == [0.95, 0.30, 0.10]


# ---------- 异步 ----------
async def test_async_success_reorders(
    monkeypatch: pytest.MonkeyPatch, docs: list[Document]
) -> None:
    _install_async(monkeypatch, _AsyncClient(response=_results((2, 0.8), (1, 0.6), (0, 0.4))))
    out = await _reranker().acompress_documents(docs, "屏幕多大")
    assert [d.metadata["chunk_id"] for d in out] == [3, 2, 1]


async def test_async_failure_falls_back_to_original_order(
    monkeypatch: pytest.MonkeyPatch, docs: list[Document]
) -> None:
    _install_async(monkeypatch, _AsyncClient(error=httpx.ReadTimeout("slow")))
    assert await _reranker().acompress_documents(docs, "电池多大") == docs


async def test_async_empty_documents_short_circuit(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install_async(monkeypatch, _AsyncClient(response=_results()))
    assert await _reranker().acompress_documents([], "q") == []
    assert client.requests == []


# ---------- 实例缓存 ----------
def test_get_reranker_honours_top_n() -> None:
    assert get_reranker(3).top_n == 3


def test_get_reranker_is_cached_per_top_n() -> None:
    """每次问答都新建实例会让缓存失效、连接复用落空。"""
    assert get_reranker(2) is get_reranker(2)
