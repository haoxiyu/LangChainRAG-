"""端到端冒烟测试:建知识库 → 摄取样例文档 → 混合检索 → 流式问答。

跑通说明整条 RAG 链路可用。用法(在 backend 目录下):
    python scripts/smoke_rag.py
"""

import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.compat import setup_event_loop_policy  # noqa: E402

setup_event_loop_policy()

from sqlalchemy import delete, select, text  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import Base, dispose_engine, get_engine, get_sessionmaker, init_engine  # noqa: E402
from app.models import Chunk, Conversation, Document, DocumentStatus, KnowledgeBase  # noqa: E402
from app.services.ingestion import document_file_path, ingest_document  # noqa: E402
from app.services.rag_chain import answer_stream  # noqa: E402
from app.services.retriever import hybrid_retrieve  # noqa: E402

SAMPLE = Path(__file__).resolve().parent.parent.parent / "sample_data" / "星辰X1智能手机_商品知识库.md"


async def main() -> None:
    await init_engine()
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    sm = get_sessionmaker()

    # ---------- 1. 准备知识库 + 文档 ----------
    async with sm() as session:
        kb = (
            await session.execute(
                select(KnowledgeBase).where(KnowledgeBase.name == "星辰X1商品知识库")
            )
        ).scalar_one_or_none()
        if kb is None:
            kb = KnowledgeBase(name="星辰X1商品知识库", description="冒烟测试用")
            session.add(kb)
            await session.commit()
        await session.refresh(kb)
        kb_id = kb.id

        # 清掉旧文档,保证可重复运行
        await session.execute(delete(Document).where(Document.knowledge_base_id == kb_id))
        await session.commit()

        doc = Document(
            knowledge_base_id=kb_id,
            filename=SAMPLE.name,
            file_type=".md",
            file_size=SAMPLE.stat().st_size,
            status=DocumentStatus.PROCESSING,
        )
        session.add(doc)
        await session.commit()
        await session.refresh(doc)
        doc_id = doc.id

        # 落盘(id 命名规则与上传接口一致)
        settings.upload_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(SAMPLE, document_file_path(doc_id, SAMPLE.name))

    print("=" * 70)
    print("1. 摄取文档")
    print("=" * 70)
    n = await ingest_document(sm(), doc_id)
    print(f"   写入分块数: {n}")

    async with sm() as session:
        row = (await session.execute(select(Document).where(Document.id == doc_id))).scalar_one()
        print(f"   文档状态: {row.status.value}  分块数: {row.chunk_count}  错误: {row.error_message or '无'}")

    print()
    print("=" * 70)
    print("2. 混合检索(稠密 + 稀疏 + RRF + 重排)")
    print("=" * 70)
    async with sm() as session:
        docs = await hybrid_retrieve(session, "这手机电池多大,充电要多久")
    for i, d in enumerate(docs, 1):
        rr = d.metadata.get("rerank_score")
        rr_s = f"{rr:.4f}" if isinstance(rr, (int, float)) else "  -  "
        print(f"   [{i}] 重排={rr_s}  {d.page_content[:46].replace(chr(10), ' ')}")

    print()
    print("=" * 70)
    print("3. 流式问答(含引用)")
    print("=" * 70)
    question = "这个手机多少钱?支持七天无理由退货吗?"
    print(f"   提问: {question}")
    print("   回答: ", end="", flush=True)
    refs = []
    async for ev in answer_stream(question, []):
        t = ev["type"]
        if t == "token":
            print(ev["content"], end="", flush=True)
        elif t == "references":
            refs = ev["references"]
        elif t == "status":
            pass
        elif t == "done":
            print()
            print(f"   --- 耗时 {ev.get('latency_ms')}ms  用量 {ev.get('usage')}")
        elif t == "error":
            print(f"\n   !! {ev['message']}")

    print()
    print(f"   引用片段 {len(refs)} 条:")
    for r in refs:
        # 跨库检索,来源要连库名一起打印,否则同名文档无从分辨
        origin = f"{r.get('knowledge_base_name') or '未知库'} / {r['filename']}"
        score = r.get("rerank_score")
        score_s = f"  重排={score:.4f}" if isinstance(score, (int, float)) else ""
        print(f"      [{r['index']}] {origin}  片段#{r['chunk_index']}{score_s}")
        print(f"          {r['content'][:60].replace(chr(10), ' ')}")

    print()
    print("=" * 70)
    print("4. 多轮追问(验证历史感知改写)")
    print("=" * 70)
    history = [
        {"role": "user", "content": question},
        {"role": "assistant", "content": "见上文"},
    ]
    follow_up = "那保修多久?"
    print(f"   追问: {follow_up}")
    print("   回答: ", end="", flush=True)
    async for ev in answer_stream(follow_up, history):
        if ev["type"] == "rewrite":
            print(f"\n   [改写为] {ev['query']}")
            print("   回答: ", end="", flush=True)
        elif ev["type"] == "token":
            print(ev["content"], end="", flush=True)
        elif ev["type"] == "done":
            print(f"\n   --- 耗时 {ev.get('latency_ms')}ms")
        elif ev["type"] == "error":
            print(f"\n   !! {ev['message']}")

    print()
    print("=" * 70)
    print("5. 语义缓存验证(重复提问应命中缓存)")
    print("=" * 70)
    async for ev in answer_stream(question, []):
        if ev["type"] == "done":
            print(f"   cache_hit={ev.get('cache_hit')}  耗时 {ev.get('latency_ms')}ms")

    await dispose_engine()
    print()
    print("冒烟测试结束。")


if __name__ == "__main__":
    asyncio.run(main())
