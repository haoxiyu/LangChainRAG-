"""RAG 编排的单元测试:上下文组装、引用快照、事件流的顺序与取舍。

这是整条链路的中枢,也是最值得锁住行为的地方,因为它同时决定三件事:
- 引用对不对 —— 编号 [n] 与前端引用卡片一一对应,错位会让「有据可查」变成误导;
  跨库检索后还要带上来源库名,否则同名文档无从分辨;
- 顺序对不对 —— references 必须先于首个 token 发出,否则前端来不及渲染来源;
- 缓存该不该写 —— 检索为空时的兜底回答(「知识库中未找到」)一旦被缓存,
  一次降级(知识库为空、全部文档处理失败)会在整个 TTL 内被反复复现。

检索范围是全部知识库,调用方不再传知识库 id,所以这里没有「无知识库」分支。

模型、向量、检索、数据库全部替换为假对象,所以本文件不联网、不连库。
"""

from __future__ import annotations

from typing import Any, AsyncIterator

import pytest
from langchain_core.documents import Document
from langchain_core.messages import AIMessage, HumanMessage

from app import db
from app.config import settings
from app.services import rag_chain
from app.services.cache import AppCache

pytestmark = pytest.mark.anyio

VECTOR = [0.25, 0.75]


# ---------- 测试替身 ----------
class _Msg:
    def __init__(self, content: Any) -> None:
        self.content = content


class _Chunk:
    def __init__(self, content: Any, usage: dict[str, Any] | None = None) -> None:
        self.content = content
        self.usage_metadata = usage


class _FakeLLM:
    """假的对话模型:记录收到的消息,按脚本回复/流式产出/抛错。"""

    def __init__(
        self,
        reply: Any = "改写后的问题",
        chunks: tuple[_Chunk, ...] = (),
        explode: bool = False,
    ) -> None:
        self.reply, self.chunks, self.explode = reply, chunks, explode
        self.invoked: list[list[Any]] = []
        self.streamed: list[list[Any]] = []

    async def ainvoke(self, messages: list[Any]) -> _Msg:
        self.invoked.append(messages)
        if self.explode:
            raise RuntimeError("模型不可用")
        return _Msg(self.reply)

    async def astream(self, messages: list[Any]) -> AsyncIterator[_Chunk]:
        self.streamed.append(messages)
        if self.explode:
            raise RuntimeError("模型不可用")
        for chunk in self.chunks:
            yield chunk


class _FakeEmbeddings:
    def __init__(self, vector: list[float] = VECTOR, explode: bool = False) -> None:
        self.vector, self.explode = vector, explode
        self.queries: list[str] = []

    async def aembed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        if self.explode:
            raise RuntimeError("向量接口不可用")
        return self.vector


class _NullSession:
    """retrieve 已被替换掉,这里只需要能当 async with 用。"""

    async def __aenter__(self) -> "_NullSession":
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


def _docs() -> list[Document]:
    return [
        Document(
            page_content="电池容量5000mAh,支持67W快充",
            metadata={
                "chunk_id": 11, "document_id": 3, "filename": "手机.md",
                "knowledge_base_id": 1, "knowledge_base_name": "星辰X1商品知识库",
                "chunk_index": 0, "rerank_score": 0.91, "rrf_score": 0.032,
            },
        ),
        Document(
            page_content="支持七天无理由退货,运费卖家承担",
            metadata={
                "chunk_id": 12, "document_id": 4, "filename": "售后.md",
                "knowledge_base_id": 2, "knowledge_base_name": "售后政策库",
                "chunk_index": 2, "rerank_score": 0.55, "rrf_score": 0.016,
            },
        ),
    ]


# ---------- 夹具:把外部依赖全部换成假的 ----------
@pytest.fixture()
def cache(tmp_path) -> AppCache:
    return AppCache(tmp_path / "cache", enabled=True)


@pytest.fixture()
def wire(monkeypatch: pytest.MonkeyPatch, cache: AppCache):
    """返回一个装配函数,可选地装上一次「假的检索结果」。"""

    def _wire(
        llm: _FakeLLM | None = None,
        embeddings: _FakeEmbeddings | None = None,
        docs: list[Document] | None = None,
    ) -> dict[str, Any]:
        llm = llm or _FakeLLM(chunks=(_Chunk("好的。", usage={"input_tokens": 8}),))
        embeddings = embeddings or _FakeEmbeddings()

        monkeypatch.setattr(rag_chain, "get_chat_model", lambda **kw: llm)
        monkeypatch.setattr(rag_chain, "get_embeddings", lambda: embeddings)
        monkeypatch.setattr(rag_chain, "get_cache", lambda: cache)
        monkeypatch.setattr(db, "get_sessionmaker", lambda: (lambda: _NullSession()))

        async def _fake_retrieve(*_a: Any, **_kw: Any) -> list[Document]:
            return list(docs or [])

        monkeypatch.setattr(rag_chain, "hybrid_retrieve", _fake_retrieve)
        return {"llm": llm, "embeddings": embeddings, "cache": cache}

    return _wire


async def _collect(stream: AsyncIterator[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event async for event in stream]


def _types(events: list[dict[str, Any]]) -> list[str]:
    return [e["type"] for e in events]


def _first(events: list[dict[str, Any]], etype: str) -> dict[str, Any]:
    return next(e for e in events if e["type"] == etype)


# ---------- 上下文组装 ----------
def test_format_context_numbers_and_labels_sources() -> None:
    """编号必须从 1 开始且与引用卡片一致 —— 提示词里写 [1],前端也认第 1 条。"""
    text = rag_chain._format_context(_docs())
    assert text.startswith("[1] (来源: 手机.md)")
    assert "[2] (来源: 售后.md)" in text
    assert "电池容量5000mAh" in text


def test_format_context_falls_back_for_missing_filename() -> None:
    text = rag_chain._format_context([Document(page_content="正文", metadata={})])
    assert "未知来源" in text


def test_format_context_without_docs_is_empty() -> None:
    assert rag_chain._format_context([]) == ""


def test_build_references_carries_scores_and_ids() -> None:
    """引用要能在知识库被改动后仍可追溯,所以 id 与分数都要快照下来。"""
    refs = rag_chain.build_references(_docs())
    assert [r["index"] for r in refs] == [1, 2]
    assert refs[0]["chunk_id"] == 11
    assert refs[0]["filename"] == "手机.md"
    assert refs[0]["content"].startswith("电池容量")
    assert refs[0]["rerank_score"] == 0.91
    assert refs[0]["rrf_score"] == 0.032
    # dense_score 未提供时留 None,前端据此决定是否展示
    assert refs[0]["dense_score"] is None


def test_build_references_carries_source_knowledge_base() -> None:
    """检索跨全部知识库,引用卡片要靠库名说清这条内容出自哪里。

    只给文件名不够:不同库可能有同名文档,那样用户和评委都无从核对。
    """
    refs = rag_chain.build_references(_docs())
    assert refs[0]["knowledge_base_id"] == 1
    assert refs[0]["knowledge_base_name"] == "星辰X1商品知识库"
    # 两条引用分别来自不同的库,证明来源确实逐条透传
    assert refs[1]["knowledge_base_name"] == "售后政策库"


def test_build_references_tolerates_missing_metadata() -> None:
    refs = rag_chain.build_references([Document(page_content="正文", metadata={})])
    assert refs[0]["chunk_id"] is None
    assert refs[0]["filename"] == ""
    # 旧数据(本次改动前落库的引用快照)没有库名字段,不能因此报错
    assert refs[0]["knowledge_base_name"] == ""


# ---------- 历史窗口 ----------
def test_history_messages_maps_roles() -> None:
    msgs = rag_chain._history_messages(
        [{"role": "user", "content": "电池多大"}, {"role": "assistant", "content": "5000mAh"}], 2
    )
    assert isinstance(msgs[0], HumanMessage)
    assert isinstance(msgs[1], AIMessage)
    assert msgs[0].content == "电池多大"


def test_history_messages_keeps_only_recent_turns() -> None:
    """历史太长会顶掉检索上下文的预算,所以只取最近 N 轮。

    注意参数单位是「轮」而不是「条」:一轮 = 用户 + 助手两条消息,
    所以 turns=2 对应最近 4 条。
    """
    history = [{"role": "user", "content": f"问题{i}"} for i in range(10)]
    assert [m.content for m in rag_chain._history_messages(history, 2)] == [
        "问题6", "问题7", "问题8", "问题9",
    ]
    assert [m.content for m in rag_chain._history_messages(history, 1)] == ["问题8", "问题9"]


def test_history_messages_skips_empty_content() -> None:
    """空内容会让部分模型直接报错,必须在拼装前丢掉。"""
    msgs = rag_chain._history_messages(
        [{"role": "user", "content": ""}, {"role": "user", "content": "有效"}], 2
    )
    assert [m.content for m in msgs] == ["有效"]


def test_history_messages_ignores_unknown_roles() -> None:
    msgs = rag_chain._history_messages(
        [{"role": "system", "content": "x"}, {"role": "user", "content": "有效"}], 2
    )
    assert [m.content for m in msgs] == ["有效"]


def test_history_messages_with_zero_turns_is_empty() -> None:
    assert rag_chain._history_messages([{"role": "user", "content": "x"}], 0) == []


# ---------- 查询改写 ----------
async def test_rewrite_without_history_skips_model(
    monkeypatch: pytest.MonkeyPatch, wire
) -> None:
    """首轮提问没有指代可消解,不该白白多花一次模型调用。"""
    llm = _FakeLLM(reply="不该被调用")
    wire(llm)
    assert await rag_chain.rewrite_query("电池多大", []) == "电池多大"
    assert llm.invoked == []


async def test_rewrite_can_be_disabled_by_config(
    monkeypatch: pytest.MonkeyPatch, wire
) -> None:
    monkeypatch.setattr(settings, "history_rewrite_turns", 0)
    llm = _FakeLLM(reply="不该被调用")
    wire(llm)
    history = [{"role": "user", "content": "电池多大"}]
    assert await rag_chain.rewrite_query("它呢", history) == "它呢"
    assert llm.invoked == []


async def test_rewrite_resolves_pronoun(wire) -> None:
    llm = _FakeLLM(reply="星辰X1的电池容量是多少")
    wire(llm)
    history = [{"role": "user", "content": "星辰X1怎么样"}]
    assert await rag_chain.rewrite_query("它电池多大", history) == "星辰X1的电池容量是多少"
    # 历史被送进模型才有指代可消解
    assert len(llm.invoked[0]) == 3


async def test_rewrite_strips_wrapping_quotes(wire) -> None:
    """模型经常把答案用引号包起来,不去掉会污染向量检索。"""
    wire(_FakeLLM(reply='"星辰X1电池容量"'))
    history = [{"role": "user", "content": "星辰X1"}]
    assert await rag_chain.rewrite_query("它呢", history) == "星辰X1电池容量"


@pytest.mark.parametrize("bad_reply", ["", "   ", "太长了" * 100])
async def test_rewrite_falls_back_on_unusable_reply(wire, bad_reply: str) -> None:
    """空回复或长到不像问题的回复都不可信,沿用原问题比用错的强。"""
    wire(_FakeLLM(reply=bad_reply))
    history = [{"role": "user", "content": "星辰X1"}]
    assert await rag_chain.rewrite_query("它呢", history) == "它呢"


async def test_rewrite_failure_keeps_original_question(wire) -> None:
    """改写只是增强,失败必须安静降级,不能让整个提问失败。"""
    wire(_FakeLLM(explode=True))
    history = [{"role": "user", "content": "星辰X1"}]
    assert await rag_chain.rewrite_query("它呢", history) == "它呢"


# ---------- 会话标题 ----------
async def test_generate_title_uses_model_reply(wire) -> None:
    wire(_FakeLLM(reply="电池容量咨询"))
    assert await rag_chain.generate_title("这款手机电池多大") == "电池容量咨询"


async def test_generate_title_strips_quotes_and_truncates(wire) -> None:
    """侧栏宽度有限:去掉模型爱加的引号,并硬截到 20 字。"""
    wire(_FakeLLM(reply='"' + "很长的标题" * 10 + '"'))
    title = await rag_chain.generate_title("问题")
    assert title == "很长的标题" * 4  # 20 字
    assert len(title) == 20
    assert not title.startswith('"')


async def test_generate_title_falls_back_to_question(wire) -> None:
    wire(_FakeLLM(explode=True))
    assert await rag_chain.generate_title("这款手机电池多大呢") == "这款手机电池多大呢"


async def test_generate_title_on_empty_question(wire) -> None:
    wire(_FakeLLM(reply=""))
    assert await rag_chain.generate_title("   ") == "新对话"


# ---------- 事件流:检索为空 ----------
async def test_stream_without_hits_uses_fallback_prompt(wire) -> None:
    """全库都没召回到内容时用兜底提示词,绝不能把「没有依据」当成「有依据」来答。"""
    llm = _FakeLLM(chunks=(_Chunk("您好,"), _Chunk("请问需要什么?", {"input_tokens": 9, "output_tokens": 4})))
    state = wire(llm, docs=[])

    events = await _collect(rag_chain.answer_stream("你好", []))
    # 检索总是跑:理解问题 → 检索知识库 → 生成回答
    assert _types(events) == ["status", "status", "status", "token", "token", "done"]
    # 没检索到就不该有引用事件
    assert "references" not in _types(events)

    system = llm.streamed[0][0]
    assert system.content == rag_chain.NO_CONTEXT_PROMPT
    # 检索总是跑一遍:改为跨全库后没有「跳过检索」这条分支了
    assert state["embeddings"].queries == ["你好"]


async def test_stream_without_hits_reports_usage(wire) -> None:
    llm = _FakeLLM(chunks=(_Chunk("答案", {"input_tokens": 9, "output_tokens": 4}),))
    wire(llm, docs=[])
    events = await _collect(rag_chain.answer_stream("你好", []))

    done = _first(events, "done")
    assert done["cache_hit"] is False
    assert done["usage"] == {"input_tokens": 9, "output_tokens": 4}
    assert done["latency_ms"] >= 0


async def test_stream_joins_structured_content_blocks(wire) -> None:
    """部分模型返回结构化内容块,拼接时要只取文本部分。"""
    llm = _FakeLLM(chunks=(_Chunk([{"type": "text", "text": "电池"}, {"noise": 1}, {"text": "5000mAh"}]),))
    wire(llm)
    events = await _collect(rag_chain.answer_stream("电池多大", []))

    tokens = [e["content"] for e in events if e["type"] == "token"]
    assert tokens == ["电池5000mAh"]


async def test_stream_skips_empty_tokens(wire) -> None:
    """空增量不该推给前端,否则会多出无意义的 SSE 帧。"""
    llm = _FakeLLM(chunks=(_Chunk(""), _Chunk("有内容")))
    wire(llm)
    events = await _collect(rag_chain.answer_stream("你好", []))

    assert [e["content"] for e in events if e["type"] == "token"] == ["有内容"]


# ---------- 事件流:命中语义缓存 ----------
async def test_stream_cache_hit_skips_model_and_retrieval(wire) -> None:
    """命中语义缓存要直接返回,既不检索也不调模型 —— 这是缓存省钱的地方。"""
    cached_refs = [{"index": 1, "content": "电池容量5000mAh", "filename": "手机.md"}]
    payload = {"query": "电池多大", "answer": "5000mAh", "references": cached_refs,
               "usage": {"input_tokens": 1, "output_tokens": 2}}

    llm = _FakeLLM(explode=True)  # 一旦被调用就失败,用来证明「完全没走模型」
    state = wire(llm, _FakeEmbeddings())
    state["cache"].semantic_store(VECTOR, payload, ttl=60)

    events = await _collect(rag_chain.answer_stream("电池多大", []))

    assert _types(events) == ["status", "status", "references", "token", "done"]
    assert _first(events, "token")["content"] == "5000mAh"
    assert _first(events, "references")["references"] == cached_refs
    done = _first(events, "done")
    assert done["cache_hit"] is True
    assert done["usage"] == payload["usage"]
    assert llm.streamed == []


async def test_stream_cache_hit_without_references(wire) -> None:
    """历史缓存可能没有引用(早期版本写入的),不能因此报错。"""
    state = wire(_FakeLLM(explode=True), _FakeEmbeddings())
    state["cache"].semantic_store(
        VECTOR, {"query": "q", "answer": "答案", "references": []}, ttl=60
    )
    events = await _collect(rag_chain.answer_stream("电池多大", []))
    assert "references" not in _types(events)
    assert _first(events, "done")["cache_hit"] is True


# ---------- 事件流:检索与引用 ----------
async def test_stream_emits_references_before_first_token(wire) -> None:
    """引用必须先于正文:前端要先把来源卡片渲染出来,再往里填流式文字。"""
    llm = _FakeLLM(chunks=(_Chunk("电池是5000mAh[1]。"),))
    wire(llm, docs=_docs())

    events = await _collect(rag_chain.answer_stream("电池多大", []))
    types = _types(events)
    assert types.index("references") < types.index("token")

    refs = _first(events, "references")["references"]
    assert [r["index"] for r in refs] == [1, 2]
    # 提示词里的编号与引用卡片同源,模型写 [1] 才指得准
    system = llm.streamed[0][0]
    assert "[1] (来源: 手机.md)" in system.content


async def test_stream_searches_with_rewritten_query(wire) -> None:
    """带指代的追问要用改写后的完整问题去检索,否则召回会明显变差。"""
    llm = _FakeLLM(reply="星辰X1的电池容量")
    state = wire(llm, docs=_docs())

    events = await _collect(
        rag_chain.answer_stream("它呢", [{"role": "user", "content": "星辰X1"}])
    )

    assert _first(events, "rewrite")["query"] == "星辰X1的电池容量"
    assert state["embeddings"].queries == ["星辰X1的电池容量"]


async def test_stream_omits_rewrite_event_when_unchanged(wire) -> None:
    """问题没被改写就不发 rewrite 事件,免得前端闪一个重复的检索词。"""
    wire(_FakeLLM(reply="电池多大"), docs=_docs())
    events = await _collect(
        rag_chain.answer_stream("电池多大", [{"role": "user", "content": "在吗"}])
    )
    assert "rewrite" not in _types(events)


async def test_stream_writes_answer_into_semantic_cache(wire) -> None:
    """成功回答要写回缓存,下一次相同问题才能直接命中。"""
    state = wire(_FakeLLM(chunks=(_Chunk("电池是5000mAh[1]。"),)), docs=_docs())
    await _collect(rag_chain.answer_stream("电池多大", []))

    cached = state["cache"].semantic_lookup(VECTOR, threshold=0.95)
    assert cached is not None
    assert cached["answer"] == "电池是5000mAh[1]。"
    assert cached["references"][0]["chunk_id"] == 11


async def test_stream_does_not_cache_answer_when_retrieval_is_empty(wire) -> None:
    """检索为空时的兜底回答绝不能被缓存。

    否则一次降级(知识库为空、全部文档处理失败)会在整个 TTL 内被反复复现,
    用户在这段时间里永远等不到正确答案。
    """
    state = wire(_FakeLLM(chunks=(_Chunk("知识库中未找到相关内容"),)), docs=[])
    events = await _collect(rag_chain.answer_stream("电池多大", []))

    assert "references" not in _types(events)
    # 语义缓存索引未被写入 —— 没有任何可命中的条目
    assert state["cache"].get(state["cache"]._sem_index_key()) is None
    assert state["cache"].semantic_lookup(VECTOR, threshold=0.95) is None


# ---------- 事件流:错误处理 ----------
async def test_stream_reports_model_failure_as_error_event(wire) -> None:
    """生成阶段失败要以 error 事件收尾,而不是把异常抛给 SSE 连接。"""
    wire(_FakeLLM(explode=True), docs=_docs())
    events = await _collect(rag_chain.answer_stream("电池多大", []))

    assert _types(events)[-1] == "error"
    assert "生成回答失败" in _first(events, "error")["message"]


async def test_stream_reports_embedding_failure_as_error_event(wire) -> None:
    """检索阶段(算查询向量)失败同样要收敛成 error 事件。"""
    wire(_FakeEmbeddings(explode=True), docs=_docs())
    events = await _collect(rag_chain.answer_stream("电池多大", []))
    assert _types(events)[-1] == "error"


async def test_stream_error_message_does_not_leak_traceback(wire) -> None:
    """错误信息要能看懂,但不该把整段堆栈塞给前端。"""
    wire(_FakeLLM(explode=True), docs=_docs())
    events = await _collect(rag_chain.answer_stream("电池多大", []))
    message = _first(events, "error")["message"]
    assert len(message) < 200
    assert "Traceback" not in message
