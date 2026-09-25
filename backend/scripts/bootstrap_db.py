"""数据库引导:启动内嵌 PostgreSQL → 建表 → 建向量索引 → 播种管理员账号。

可重复执行(全部操作幂等)。

用法:
    python -m backend.scripts.bootstrap_db       # 从项目根目录
    python scripts/bootstrap_db.py               # 从 backend 目录
"""

import asyncio
import sys
from pathlib import Path

# 允许直接以脚本方式运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.compat import setup_event_loop_policy  # noqa: E402

setup_event_loop_policy()

from app.config import settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db import Base, dispose_engine, get_engine, get_sessionmaker, init_engine  # noqa: E402
from app.models import User, UserRole  # noqa: E402,F401  导入以注册表结构,勿删

# pgvector 的 HNSW 参数:在检索速度与召回率之间取平衡
HNSW_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw
ON chunks USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64)
"""


async def main() -> None:
    print("=" * 64)
    print("1. 启动数据库并初始化引擎")
    print("=" * 64)
    url = await init_engine()
    safe = url.split("@")[-1] if "@" in url else url
    print(f"   连接目标: ...@{safe}")

    print()
    print("=" * 64)
    print("2. 建表")
    print("=" * 64)
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        rows = await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
        )
        tables = [r[0] for r in rows]
    print(f"   表: {tables}")

    print()
    print("=" * 64)
    print("3. 建向量索引(HNSW)")
    print("=" * 64)
    async with get_engine().begin() as conn:
        await conn.execute(text(HNSW_INDEX_SQL))
        rows = await conn.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE tablename='chunks' ORDER BY indexname"
            )
        )
        for name, ddl in rows:
            print(f"   {name}")
            print(f"      {ddl[:110]}...")

    print()
    print("=" * 64)
    print("4. 播种管理员账号")
    print("=" * 64)
    async with get_sessionmaker()() as session:
        existing = await session.execute(
            text("SELECT id, username, role FROM users WHERE username = :u"),
            {"u": settings.admin_username},
        )
        row = existing.first()
        if row:
            print(f"   管理员已存在: id={row[0]} username={row[1]} role={row[2]}")
        else:
            admin = User(
                username=settings.admin_username,
                password_hash=hash_password(settings.admin_password),
                role=UserRole.ADMIN,
            )
            session.add(admin)
            await session.commit()
            print(f"   已创建管理员: {settings.admin_username} / {settings.admin_password}")

    print()
    print("=" * 64)
    print("5. 汇总")
    print("=" * 64)
    async with get_engine().connect() as conn:
        for tbl in ("users", "knowledge_bases", "documents", "chunks", "conversations", "messages"):
            n = (await conn.execute(text(f"SELECT count(*) FROM {tbl}"))).scalar()
            print(f"   {tbl:18s} {n} 行")

    await dispose_engine()
    print()
    print("引导完成。")


if __name__ == "__main__":
    asyncio.run(main())
