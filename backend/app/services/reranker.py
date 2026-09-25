"""重排序:把百炼原生 rerank 接口包装成 LangChain 的 BaseDocumentCompressor。

为什么不用 langchain 自带的压缩器:
百炼的 rerank **只在原生接口提供**,OpenAI 兼容模式没有对应端点(实测 404),
所以无法走 langchain-openai 的通用封装,必须直接调原生 API。
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Sequence

import httpx
from langchain_core.callbacks import Callbacks
from langchain_core.documents import Document
from langchain_core.documents.compressor import BaseDocumentCompressor
from pydantic import ConfigDict, Field

from ..config import settings

logger = logging.getLogger(__name__)


class BailianReranker(BaseDocumentCompressor):
    """调用百炼 rerank 接口对候选文档按相关度重排。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    model_name: str = Field(default_factory=lambda: settings.rerank_model)
    api_key: str = Field(default_factory=lambda: settings.dashscope_api_key)
    native_base: str = Field(default_factory=lambda: settings.dashscope_native_base)
    top_n: int = Field(default_factory=lambda: settings.rerank_top_k)
    timeout: float = 60.0

    @property
    def _endpoint(self) -> str:
        return f"{self.native_base}/rerank/text-rerank/text-rerank"

    def _payload(self, query: str, documents: list[str]) -> dict[str, Any]:
        return {
            "model": self.model_name,
            "input": {"query": query, "documents": documents},
            # 不要求回传原文,只取 index + 分数,减少响应体积
            "parameters": {"return_documents": False, "top_n": min(self.top_n, len(documents))},
        }

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _apply(
        results: list[dict[str, Any]], documents: Sequence[Document]
    ) -> list[Document]:
        """把接口返回的下标映射回原文档,并写入重排分数。"""
        out: list[Document] = []
        for item in results:
            idx = item.get("index")
            if idx is None or not (0 <= idx < len(documents)):
                continue
            doc = documents[idx]
            doc.metadata = {**doc.metadata, "rerank_score": item.get("relevance_score")}
            out.append(doc)
        return out

    # ---------- 同步 ----------
    def compress_documents(
        self,
        documents: Sequence[Document],
        query: str,
        callbacks: Callbacks | None = None,
    ) -> Sequence[Document]:
        docs = list(documents)
        if not docs:
            return []
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    self._endpoint,
                    json=self._payload(query, [d.page_content for d in docs]),
                    headers=self._headers(),
                )
                resp.raise_for_status()
                results = resp.json().get("output", {}).get("results", [])
        except Exception as e:
            # 重排是增强环节,失败不应让整个问答崩掉 —— 降级为保留原顺序
            logger.warning("重排序失败,降级为原始排序: %s", e)
            return docs
        return self._apply(results, docs)

    # ---------- 异步 ----------
    async def acompress_documents(
        self,
        documents: Sequence[Document],
        query: str,
        callbacks: Callbacks | None = None,
    ) -> Sequence[Document]:
        docs = list(documents)
        if not docs:
            return []
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    self._endpoint,
                    json=self._payload(query, [d.page_content for d in docs]),
                    headers=self._headers(),
                )
                resp.raise_for_status()
                results = resp.json().get("output", {}).get("results", [])
        except Exception as e:
            logger.warning("重排序失败,降级为原始排序: %s", e)
            return docs
        return self._apply(results, docs)


@lru_cache(maxsize=4)
def get_reranker(top_n: int | None = None) -> BailianReranker:
    kwargs: dict[str, Any] = {}
    if top_n is not None:
        kwargs["top_n"] = top_n
    return BailianReranker(**kwargs)


if __name__ == "__main__":  # 手动冒烟测试
    docs = [
        Document(page_content="商品支持七天无理由退货,运费卖家承担"),
        Document(page_content="这款手机电池容量5000mAh,支持67W快充"),
        Document(page_content="屏幕6.7英寸AMOLED,120Hz刷新率"),
    ]
    rr = BailianReranker()
    ranked = rr.compress_documents(docs, "手机电池多大")
    for d in ranked:
        print(f"  {d.metadata.get('rerank_score'):.4f}  {d.page_content[:24]}")
