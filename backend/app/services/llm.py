"""百炼对话模型封装(OpenAI 兼容模式)。

注意:重排序不走这里 —— 兼容模式不提供 rerank 接口,见 reranker.py。
"""

from __future__ import annotations

from functools import lru_cache

from langchain_openai import ChatOpenAI

from ..config import settings


@lru_cache(maxsize=8)
def get_chat_model(
    temperature: float = 0.3,
    streaming: bool = True,
    model: str | None = None,
) -> ChatOpenAI:
    """返回配置好的对话模型实例。

    Args:
        temperature: 采样温度,知识库问答宜低(0.3)以保证忠实于检索内容。
        streaming: 是否流式输出。
        model: 覆盖默认模型名。
    """
    return ChatOpenAI(
        model=model or settings.chat_model,
        api_key=settings.dashscope_api_key,
        base_url=settings.dashscope_base_url,
        temperature=temperature,
        streaming=streaming,
        # 流式请求也返回 token 用量,便于统计与展示
        stream_usage=True,
        timeout=120,
        max_retries=2,
    )
