"""百炼向量化封装:批量调用 + diskcache 缓存。

优化点:
- 同一段文本只调用一次云端接口(hash 命中缓存直接返回),入库重跑不重复花钱
- 自动分批,规避单次请求的条数上限
- 同时提供同步与异步接口,异步路径供 FastAPI 使用
"""

from __future__ import annotations

import logging

from langchain_core.embeddings import Embeddings
from langchain_openai import OpenAIEmbeddings

from ..config import settings
from .cache import get_cache

logger = logging.getLogger(__name__)

# 单次请求的最大文本条数,超出则分批
EMBED_BATCH_SIZE = 10


class BailianEmbeddings(Embeddings):
    """带缓存的百炼向量化器。"""

    def __init__(self, model: str | None = None) -> None:
        self.model = model or settings.embedding_model
        self._client = OpenAIEmbeddings(
            model=self.model,
            api_key=settings.dashscope_api_key,
            base_url=settings.dashscope_base_url,
            dimensions=settings.embedding_dim,
            # 百炼不是 OpenAI 的分词器,tiktoken 会按模型名找不到编码而报错,
            # 关掉长度检查后直接发送原始字符串
            check_embedding_ctx_length=False,
        )
        self._cache = get_cache()

    # ---------- 内部工具 ----------
    def _split_missing(self, texts: list[str]) -> tuple[list[list[float] | None], list[int]]:
        """先查缓存,返回 (结果占位数组, 未命中的下标)。"""
        results: list[list[float] | None] = [None] * len(texts)
        missing: list[int] = []
        for i, text in enumerate(texts):
            cached = self._cache.get_embedding(text, self.model)
            if cached is not None:
                results[i] = cached
            else:
                missing.append(i)
        return results, missing

    async def _embed_missing_async(self, texts: list[str], missing: list[int],
                                   results: list[list[float] | None]) -> None:
        """对未命中的文本分批调用云端接口,并回填 + 写缓存。"""
        for start in range(0, len(missing), EMBED_BATCH_SIZE):
            batch_idx = missing[start:start + EMBED_BATCH_SIZE]
            batch_texts = [texts[i] for i in batch_idx]
            vectors = await self._client.aembed_documents(batch_texts)
            for i, vec in zip(batch_idx, vectors):
                results[i] = vec
                self._cache.set_embedding(texts[i], self.model, vec)

    def _embed_missing_sync(self, texts: list[str], missing: list[int],
                            results: list[list[float] | None]) -> None:
        for start in range(0, len(missing), EMBED_BATCH_SIZE):
            batch_idx = missing[start:start + EMBED_BATCH_SIZE]
            batch_texts = [texts[i] for i in batch_idx]
            vectors = self._client.embed_documents(batch_texts)
            for i, vec in zip(batch_idx, vectors):
                results[i] = vec
                self._cache.set_embedding(texts[i], self.model, vec)

    # ---------- 对外接口 ----------
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        results, missing = self._split_missing(texts)
        if missing:
            logger.info("向量化: 共 %d 条,缓存命中 %d 条,需请求 %d 条",
                        len(texts), len(texts) - len(missing), len(missing))
            self._embed_missing_sync(texts, missing, results)
        return results  # type: ignore[return-value]

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        results, missing = self._split_missing(texts)
        if missing:
            logger.info("向量化(异步): 共 %d 条,缓存命中 %d 条,需请求 %d 条",
                        len(texts), len(texts) - len(missing), len(missing))
            await self._embed_missing_async(texts, missing, results)
        return results  # type: ignore[return-value]

    def embed_query(self, text: str) -> list[float]:
        cached = self._cache.get_embedding(text, self.model)
        if cached is not None:
            return cached
        vec = self._client.embed_query(text)
        self._cache.set_embedding(text, self.model, vec)
        return vec

    async def aembed_query(self, text: str) -> list[float]:
        cached = self._cache.get_embedding(text, self.model)
        if cached is not None:
            return cached
        vec = await self._client.aembed_query(text)
        self._cache.set_embedding(text, self.model, vec)
        return vec


_embeddings: BailianEmbeddings | None = None


def get_embeddings() -> BailianEmbeddings:
    global _embeddings
    if _embeddings is None:
        _embeddings = BailianEmbeddings()
    return _embeddings


if __name__ == "__main__":  # 手动冒烟测试
    emb = BailianEmbeddings()
    vs = emb.embed_documents(["七天无理由退货", "七天无理由退货", "电池容量"])
    print("维度:", len(vs[0]), "条数:", len(vs))
    print("缓存命中验证(第二次应无请求日志):")
    vs2 = emb.embed_documents(["七天无理由退货"])
    print("一致:", vs[0] == vs2[0])
