"""临时探针:验证 pgembed 能否拉起 PostgreSQL 并启用 pgvector。

覆盖:initdb → 启动 → 建库 → CREATE EXTENSION vector → 插入 → 近邻查询 → HNSW 索引。
验证通过后本文件可删除。
"""

import sys
from pathlib import Path

import pgembed
import psycopg

PGDATA = Path(r"D:\LangChainRAG\.pgdata")
DB_NAME = "ragdb"

print("=" * 60)
print("[1] 启动 PostgreSQL(首次会自动 initdb)")
print("=" * 60)
server = pgembed.get_server(PGDATA)
print("pgdata  :", server.pgdata)
print("pid     :", server.get_pid())
uri = server.get_uri()
print("uri     :", uri)

print()
print("=" * 60)
print("[2] 建库")
print("=" * 60)
admin_uri = server.get_uri(database="postgres")
with psycopg.connect(admin_uri, autocommit=True) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,))
        if not cur.fetchone():
            cur.execute(f'CREATE DATABASE "{DB_NAME}"')
            print(f"已创建数据库 {DB_NAME}")
        else:
            print(f"数据库 {DB_NAME} 已存在")

db_uri = server.get_uri(database=DB_NAME)
print("db_uri  :", db_uri)

print()
print("=" * 60)
print("[3] 启用 pgvector 扩展")
print("=" * 60)
with psycopg.connect(db_uri, autocommit=True) as conn:
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute("SELECT extname, extversion FROM pg_extension WHERE extname='vector'")
        row = cur.fetchone()
        print(f"扩展已启用: {row[0]}  版本 {row[1]}")

        # 顺带看看还能装哪些
        cur.execute("SELECT name, default_version FROM pg_available_extensions "
                    "WHERE name IN ('vector','vectorscale','pg_search','pg_trgm','pgtextsearch') ORDER BY name")
        print("可用扩展:", cur.fetchall())

print()
print("=" * 60)
print("[4] 建表 + 插入向量 + 近邻查询")
print("=" * 60)
with psycopg.connect(db_uri, autocommit=True) as conn:
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS demo_chunks")
        cur.execute("""
            CREATE TABLE demo_chunks (
                id        BIGSERIAL PRIMARY KEY,
                doc_id    TEXT NOT NULL,
                content   TEXT NOT NULL,
                embedding vector(1024) NOT NULL
            )
        """)

        # 用三个正交方向构造向量,便于肉眼验证检索正确性
        def vec(axis: int, noise: float = 0.0) -> str:
            v = [noise] * 1024
            v[axis] = 1.0
            return "[" + ",".join(f"{x:.6f}" for x in v) + "]"

        rows = [
            ("D1", "这款手机电池容量是5000mAh,支持67W快充", vec(0)),
            ("D2", "商品支持七天无理由退货,运费由卖家承担", vec(1)),
            ("D3", "手机屏幕为6.7英寸AMOLED,刷新率120Hz", vec(2)),
        ]
        cur.executemany(
            "INSERT INTO demo_chunks (doc_id, content, embedding) VALUES (%s, %s, %s)",
            rows,
        )
        print(f"已插入 {len(rows)} 条向量(维度 1024)")

        # 查询:最接近 axis 0(电池)
        cur.execute("""
            SELECT doc_id, content,
                   1 - (embedding <=> %s::vector) AS cosine_sim
            FROM demo_chunks
            ORDER BY embedding <=> %s::vector
            LIMIT 3
        """, (vec(0, noise=0.05), vec(0, noise=0.05)))
        print("近邻检索结果(应把 D1 电池排第一):")
        for doc_id, content, sim in cur.fetchall():
            print(f"   [{doc_id}] 相似度={sim:.4f}  {content[:30]}")

print()
print("=" * 60)
print("[5] 建 HNSW 索引(企业级检索性能关键)")
print("=" * 60)
with psycopg.connect(db_uri, autocommit=True) as conn:
    with conn.cursor() as cur:
        cur.execute("CREATE INDEX IF NOT EXISTS idx_demo_hnsw "
                    "ON demo_chunks USING hnsw (embedding vector_cosine_ops)")
        cur.execute("SELECT indexname FROM pg_indexes WHERE tablename='demo_chunks'")
        print("索引:", [r[0] for r in cur.fetchall()])

print()
print("=" * 60)
print("结论: PostgreSQL + pgvector 链路全部打通,零安装即可用")
print("=" * 60)
