"""缓存服务(diskcache,免 Redis 依赖)。

三个用途:
1. embedding 缓存  —— 同一段文本不重复调用云端接口,直接省钱降延迟
2. 语义缓存        —— 相似问题直接返回历史答案,跳过整条 RAG 链路
3. 限流计数        —— 按用户/IP 滑动窗口计数
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import diskcache
import numpy as np

from ..config import settings

logger = logging.getLogger(__name__)

# 每个知识库在语义缓存索引里保留的最大条目数,防止索引无限膨胀
SEMANTIC_INDEX_LIMIT = 200


def _hash(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


class AppCache:
    """diskcache 的薄封装,统一处理序列化与命名空间。"""

    def __init__(self, directory: Path, enabled: bool = True) -> None:
        self.enabled = enabled
        self._lock = threading.Lock()
        if enabled:
            directory.mkdir(parents=True, exist_ok=True)
            # size_limit 单位为字节,512MB 足够毕设规模使用
            self._cache = diskcache.Cache(str(directory), size_limit=512 * 1024 * 1024)
        else:
            self._cache = None

    # ---------- 通用 ----------
    def get(self, key: str) -> Any | None:
        if not self.enabled or self._cache is None:
            return None
        return self._cache.get(key)

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        if not self.enabled or self._cache is None:
            return
        self._cache.set(key, value, expire=ttl)

    def delete(self, key: str) -> None:
        if self.enabled and self._cache is not None:
            self._cache.delete(key)

    # ---------- embedding 缓存 ----------
    def get_embedding(self, text: str, model: str) -> list[float] | None:
        return self.get(f"emb:{model}:{_hash(text)}")

    def set_embedding(self, text: str, model: str, vector: list[float], ttl: int = 7 * 86400) -> None:
        self.set(f"emb:{model}:{_hash(text)}", vector, ttl=ttl)

    # ---------- 语义缓存 ----------
    # 检索范围是全部知识库,所以缓存也只有一个全局索引 —— 同一个问题在哪儿
    # 问都走同一套检索,全局缓存反而比按库分片更准确。
    def semantic_lookup(
        self, query_vector: list[float], threshold: float
    ) -> dict[str, Any] | None:
        """在语义缓存中找相似问题,命中则返回历史答案。"""
        if not self.enabled or self._cache is None:
            return None
        index = self.get(self._sem_index_key())
        if not index:
            return None

        q = np.asarray(query_vector, dtype=np.float32)
        q_norm = np.linalg.norm(q)
        if q_norm == 0:
            return None

        best_key, best_sim = None, -1.0
        for entry in index:
            vec = np.asarray(entry["v"], dtype=np.float32)
            denom = q_norm * np.linalg.norm(vec)
            if denom == 0:
                continue
            sim = float(np.dot(q, vec) / denom)
            if sim > best_sim:
                best_sim, best_key = sim, entry["k"]

        if best_key is not None and best_sim >= threshold:
            payload = self.get(best_key)
            if payload is not None:
                logger.info("语义缓存命中 相似度=%.4f", best_sim)
                return payload
        return None

    def semantic_store(
        self, query_vector: list[float], payload: dict[str, Any], ttl: int
    ) -> None:
        if not self.enabled or self._cache is None:
            return
        key = f"sem:global:{_hash(json.dumps(payload.get('query', ''), ensure_ascii=False), str(time.time()))}"
        self.set(key, payload, ttl=ttl)

        with self._lock:
            index_key = self._sem_index_key()
            index = self.get(index_key) or []
            index.append({"k": key, "v": [float(x) for x in query_vector]})
            # 只保留最近的若干条,且清掉已过期的
            index = index[-SEMANTIC_INDEX_LIMIT:]
            self.set(index_key, index, ttl=ttl * 4)

    def invalidate_semantic(self) -> None:
        """知识库内容变更后必须调用,否则会答出旧数据。"""
        if not self.enabled or self._cache is None:
            return
        index_key = self._sem_index_key()
        index = self.get(index_key) or []
        with self._lock:
            for entry in index:
                self.delete(entry["k"])
            self.delete(index_key)
        logger.info("已失效语义缓存(%d 条)", len(index))

    @staticmethod
    def _sem_index_key() -> str:
        return "sem_index:global"

    # ---------- 限流(滑动窗口) ----------
    def rate_limit(self, key: str, limit: int, window_seconds: int = 60) -> tuple[bool, int]:
        """返回 (是否放行, 剩余额度)。窗口内计数超过 limit 则拒绝。"""
        if not self.enabled or self._cache is None:
            return True, limit

        bucket = f"rl:{key}:{int(time.time()) // window_seconds}"
        with self._lock:
            current = self._cache.get(bucket, 0) + 1
            self._cache.set(bucket, current, expire=window_seconds * 2)
        return current <= limit, max(0, limit - current)

    def stats(self) -> dict[str, Any]:
        if not self.enabled or self._cache is None:
            return {"enabled": False}
        return {
            "enabled": True,
            "entries": len(self._cache),
            "size_bytes": self._cache.volume(),
            "directory": str(self._cache.directory),
        }


_cache: AppCache | None = None


def get_cache() -> AppCache:
    global _cache
    if _cache is None:
        _cache = AppCache(settings.cache_dir, enabled=settings.cache_enabled)
    return _cache
