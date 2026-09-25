"""FastAPI 应用入口。

注意:compat.setup_event_loop_policy() 必须在任何事件循环创建之前调用,
否则 Windows 上 psycopg 的异步模式会直接报错。
"""

from __future__ import annotations

from app.compat import setup_event_loop_policy

setup_event_loop_policy()  # noqa: E402  必须早于其他异步相关导入

import logging  # noqa: E402
import time  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402
from pathlib import Path  # noqa: E402

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from sqlalchemy import text  # noqa: E402

from .api import admin, auth, chat, conversations, documents, knowledge_bases  # noqa: E402
from .config import ROOT_DIR, settings  # noqa: E402
from .db import Base, dispose_engine, get_engine, init_engine  # noqa: E402
from .models import User, UserRole  # noqa: E402,F401  导入以注册表结构

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时拉起数据库内嵌实例并建表,关闭时释放连接池。"""
    logger.info("正在启动服务,初始化数据库…")
    url = await init_engine()
    safe = url.split("@")[-1] if "@" in url else url
    logger.info("数据库就绪: ...@%s", safe)

    # create_all 幂等,已存在的表不会被改动;正式迁移可用 alembic
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw "
                "ON chunks USING hnsw (embedding vector_cosine_ops) "
                "WITH (m = 16, ef_construction = 64)"
            )
        )

    for d in (settings.upload_dir, settings.cache_dir):
        d.mkdir(parents=True, exist_ok=True)

    logger.info("启动完成。接口文档: http://127.0.0.1:8000/docs")
    try:
        yield
    finally:
        logger.info("正在关闭服务…")
        await dispose_engine()


app = FastAPI(
    title="电商商品知识库问答系统",
    description=(
        "基于 LangChain + 阿里云百炼 + PostgreSQL(pgvector) 的企业级 RAG 问答系统。\n\n"
        "- 检索:稠密向量 + BM25 稀疏 + RRF 融合 + 重排序\n"
        "- 生成:百炼 qwen 流式输出,回答带知识库引用\n"
        "- 存储:用户/会话/知识库/向量统一在 PostgreSQL"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def access_log(request: Request, call_next):
    """记录每个请求的耗时,便于定位性能瓶颈。"""
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000
    # 静态资源与文档页不记日志,避免刷屏
    if not request.url.path.startswith(("/docs", "/openapi", "/assets")):
        logger.info(
            "%s %s -> %d  %.1fms",
            request.method, request.url.path, response.status_code, elapsed_ms,
        )
    response.headers["X-Process-Time-Ms"] = f"{elapsed_ms:.1f}"
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底异常处理:记录堆栈,但只向客户端返回简短信息。"""
    logger.exception("未处理异常: %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误,请稍后重试"})


app.include_router(auth.router)
app.include_router(knowledge_bases.router)
app.include_router(documents.router)
app.include_router(conversations.router)
app.include_router(chat.router)
app.include_router(admin.router)


@app.get("/api/health", tags=["系统"], summary="健康检查")
async def health() -> dict[str, object]:
    """探活接口:同时验证数据库连通性与 pgvector 是否可用。"""
    db_ok, vector_ok, error = False, False, ""
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
            db_ok = True
            version = (
                await conn.execute(
                    text("SELECT extversion FROM pg_extension WHERE extname='vector'")
                )
            ).scalar_one_or_none()
            vector_ok = version is not None
            pgvector_version = version
    except Exception as e:
        error = str(e)[:200]
        pgvector_version = None

    return {
        "status": "ok" if (db_ok and vector_ok) else "degraded",
        "database": db_ok,
        "pgvector": vector_ok,
        "pgvector_version": pgvector_version,
        "chat_model": settings.chat_model,
        "embedding_model": settings.embedding_model,
        "rerank_model": settings.rerank_model,
        "error": error,
    }


# 生产模式下直接由后端托管前端构建产物(存在 dist 时才挂载)
_frontend_dist = ROOT_DIR / "frontend" / "dist"
if _frontend_dist.is_dir():
    app.mount("/", StaticFiles(directory=str(_frontend_dist), html=True), name="frontend")
    logger.info("已挂载前端静态资源: %s", _frontend_dist)
