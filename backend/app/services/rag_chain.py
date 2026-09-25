"""RAG 编排:历史感知改写 → 语义缓存 → 混合检索 → 重排 → 流式生成 → 引用回填。

对外只暴露一个异步生成器 answer_stream,以事件流的形式产出结果,
供上层直接转成 SSE 推给浏览器。
"""

from __future__ import annotations

import logging
import time
from typing import Any, AsyncIterator

from langchain_core.documents import Document
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from ..config import settings
from .cache import get_cache
from .embedding import get_embeddings
from .llm import get_chat_model
from .retriever import hybrid_retrieve

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是电商平台的智能客服助手,基于阿里云百炼平台的通义千问(qwen-plus)模型构建,
负责依据「知识库内容」回答用户关于商品的问题。

被问及你自身(用的什么模型、是什么系统)时,如实说明即可,不要编造身份。

回答要求:
1. 只依据知识库内容作答,禁止编造。知识库中没有的信息,直接说明"知识库中未找到相关内容",并建议用户联系人工客服。
2. 凡是引用了知识库内容的句子,必须在句末用方括号标注对应编号,例如 [1];同时引用多条则写 [1][2]。这是硬性要求,便于用户核对来源。
3. 回答要简洁、口语化、面向消费者,不要原文罗列资料,要转述成通顺的回答。
4. 价格、库存、售后政策、保修期限等信息必须严格以知识库为准,不要推测或给出范围。

知识库内容:
{context}"""

NO_CONTEXT_PROMPT = """你是电商平台的智能客服助手(基于阿里云百炼平台的通义千问 qwen-plus 构建)。

本轮没有检索到相关商品知识。请礼貌地告知用户知识库中未找到相关信息,
建议其换个说法或联系人工客服,不要编造任何商品信息。"""

REWRITE_PROMPT = """请把用户最新的问题改写成一个可以独立检索的完整问题。

要求:
- 若最新问题含有指代(如"它""这款""还有别的吗"),结合对话历史把指代补全
- 只输出改写后的问题本身,不要解释、不要加引号
- 若最新问题本身已经完整,原样输出"""


def _format_context(docs: list[Document]) -> str:
    """把检索结果编号后拼进 prompt,编号与前端引用卡片一一对应。"""
    blocks: list[str] = []
    for i, doc in enumerate(docs, start=1):
        source = doc.metadata.get("filename", "未知来源")
        blocks.append(f"[{i}] (来源: {source})\n{doc.page_content}")
    return "\n\n".join(blocks)


def build_references(docs: list[Document]) -> list[dict[str, Any]]:
    """构造随回答一起返回、并落库快照的引用列表。

    检索跨全部知识库,所以必须带上来源库名 —— 不同库可能有同名文档,
    只报文件名用户无从核对内容出自哪里。
    """
    refs: list[dict[str, Any]] = []
    for i, doc in enumerate(docs, start=1):
        refs.append(
            {
                "index": i,
                "chunk_id": doc.metadata.get("chunk_id"),
                "document_id": doc.metadata.get("document_id"),
                "filename": doc.metadata.get("filename", ""),
                "knowledge_base_id": doc.metadata.get("knowledge_base_id"),
                "knowledge_base_name": doc.metadata.get("knowledge_base_name", ""),
                "chunk_index": doc.metadata.get("chunk_index"),
                "content": doc.page_content,
                # 重排分与融合分都留着,便于答辩时展示检索质量
                "rerank_score": doc.metadata.get("rerank_score"),
                "rrf_score": doc.metadata.get("rrf_score"),
                "dense_score": doc.metadata.get("dense_score"),
            }
        )
    return refs


def _history_messages(history: list[dict[str, str]], turns: int) -> list[Any]:
    """把最近若干轮对话转成 LangChain 消息。"""
    if turns <= 0:
        return []
    recent = history[-turns * 2:]
    msgs: list[Any] = []
    for item in recent:
        role, content = item.get("role"), item.get("content", "")
        if not content:
            continue
        if role == "user":
            msgs.append(HumanMessage(content=content))
        elif role == "assistant":
            msgs.append(AIMessage(content=content))
    return msgs


async def rewrite_query(question: str, history: list[dict[str, str]]) -> str:
    """把带指代的追问改写为可独立检索的问题。无历史时原样返回。"""
    if not history or settings.history_rewrite_turns <= 0:
        return question
    try:
        llm = get_chat_model(temperature=0.0, streaming=False)
        msgs = [
            SystemMessage(content=REWRITE_PROMPT),
            *_history_messages(history, settings.history_rewrite_turns),
            HumanMessage(content=f"最新问题: {question}"),
        ]
        result = await llm.ainvoke(msgs)
        rewritten = (result.content or "").strip()
        # 模型偶尔会带引号或前缀,做个兜底清理
        rewritten = rewritten.strip('"').strip("'").strip()
        if rewritten and len(rewritten) <= 200:
            if rewritten != question:
                logger.info("查询改写: %r -> %r", question, rewritten)
            return rewritten
    except Exception as e:
        logger.warning("查询改写失败,沿用原问题: %s", e)
    return question


async def generate_title(question: str) -> str:
    """用首个问题生成简短的会话标题。"""
    fallback = question.strip()[:20] or "新对话"
    try:
        llm = get_chat_model(temperature=0.0, streaming=False)
        result = await llm.ainvoke(
            [
                SystemMessage(
                    content="用不超过 12 个字概括用户问题的主题,只输出标题本身,"
                            "不要标点、不要引号。"
                ),
                HumanMessage(content=question),
            ]
        )
        title = (result.content or "").strip().strip('"').strip("'")[:20]
        return title or fallback
    except Exception:
        return fallback


async def answer_stream(
    question: str,
    history: list[dict[str, str]],
) -> AsyncIterator[dict[str, Any]]:
    """执行完整 RAG 流程,以事件流形式产出结果。

    检索范围是全部知识库:用户不知道该选哪个库,让他选等于把检索失败的责任
    推给他。哪些内容胜出交给打分排序决定,而不是提前选库。

    事件类型:
      rewrite    —— 改写后的检索问题
      references —— 引用片段列表(在首个 token 之前发出,便于前端先渲染来源)
      token      —— 增量文本
      done       —— 结束,含用量与耗时
      error      —— 出错
    """
    started = time.perf_counter()
    cache = get_cache()

    try:
        yield {"type": "status", "message": "正在理解问题"}

        # ---------- 1. 历史感知改写 ----------
        search_query = await rewrite_query(question, history)
        if search_query != question:
            yield {"type": "rewrite", "query": search_query}

        # ---------- 2. 检索 ----------
        docs: list[Document] = []
        references: list[dict[str, Any]] = []
        # 查询向量算一次就够:命中缓存时用于查,未命中时留到写缓存复用
        query_vector: list[float] | None = None

        yield {"type": "status", "message": "正在检索知识库"}

        # 语义缓存:先用改写后问题的向量找历史相似问题
        query_vector = await get_embeddings().aembed_query(search_query)
        cached = cache.semantic_lookup(query_vector, settings.semantic_cache_threshold)
        if cached:
            references = cached.get("references", [])
            if references:
                yield {"type": "references", "references": references}
            yield {"type": "token", "content": cached.get("answer", "")}
            yield {
                "type": "done",
                "usage": cached.get("usage", {}),
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "cache_hit": True,
            }
            return

        # 缓存未命中,走真正的混合检索
        # 这里复用外层传入的会话由调用方管理,retriever 内部自开独立会话并发
        from ..db import get_sessionmaker

        async with get_sessionmaker()() as session:
            docs = await hybrid_retrieve(session, search_query)

        references = build_references(docs)
        if references:
            yield {"type": "references", "references": references}

        # ---------- 3. 组装 prompt ----------
        if docs:
            system_content = SYSTEM_PROMPT.format(context=_format_context(docs))
        else:
            system_content = NO_CONTEXT_PROMPT

        messages = [
            SystemMessage(content=system_content),
            *_history_messages(history, settings.history_rewrite_turns),
            HumanMessage(content=question),
        ]

        # ---------- 4. 流式生成 ----------
        yield {"type": "status", "message": "正在生成回答"}
        llm = get_chat_model(streaming=True)
        parts: list[str] = []
        usage: dict[str, Any] = {}

        async for chunk in llm.astream(messages):
            text = chunk.content
            if isinstance(text, list):
                # 部分模型会返回结构化内容块,拼接其中的文本部分
                text = "".join(
                    b.get("text", "") for b in text if isinstance(b, dict)
                )
            if text:
                parts.append(text)
                yield {"type": "token", "content": text}
            # 流式响应里用量信息只挂在最后一个 chunk 上
            if getattr(chunk, "usage_metadata", None):
                usage = dict(chunk.usage_metadata)

        answer = "".join(parts)
        latency_ms = int((time.perf_counter() - started) * 1000)

        # ---------- 5. 写入语义缓存 ----------
        # 必须有 docs:检索为空时模型给的是「未找到」兜底,把它缓存下来会让一次降级
        # (知识库为空、全部文档处理失败)在整个 TTL 内被反复复现,用户始终等不到正确答案。
        if docs and answer and query_vector is not None:
            try:
                cache.semantic_store(
                    query_vector,
                    {"query": search_query, "answer": answer,
                     "references": references, "usage": usage},
                    ttl=settings.semantic_cache_ttl,
                )
            except Exception as e:
                logger.warning("写入语义缓存失败: %s", e)

        yield {
            "type": "done",
            "usage": usage,
            "latency_ms": latency_ms,
            "cache_hit": False,
        }

    except Exception as e:
        logger.exception("问答流程失败")
        yield {"type": "error", "message": f"生成回答失败: {e}"}
