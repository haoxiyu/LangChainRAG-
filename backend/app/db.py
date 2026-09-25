"""数据库层:内嵌 PostgreSQL 引导 + SQLAlchemy 异步引擎。

默认走 pgembed 启动项目内嵌的 PostgreSQL(免安装、免 Docker);
若 .env 里配置了 DATABASE_URL,则优先使用外部数据库。
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import AsyncGenerator

import psutil
import psycopg
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings

logger = logging.getLogger(__name__)

# pgembed 的 PostgresServer 实例缓存,避免重复启动
_server = None

# PostgreSQL 崩溃恢复在 Windows 上要重试 30 秒(原因见 _start_embedded_pg),
# 而 pgembed 写死的 pg_ctl 超时只有 10 秒,所以要留足等待余量。
_PG_RECOVERY_WAIT_SECONDS = 90.0


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def _prune_dead_handles(pgdata: Path) -> None:
    """清掉 .handle_pids.json 里已经退出的进程记录。

    pgembed 用这个文件记「还有哪些进程持有该 PG 实例」,只有自己是唯一持有者时
    才会在退出时正常关闭数据库。进程被强杀(直接关控制台窗口)时来不及清理,
    死 pid 就永久留在文件里 —— 此后哪怕每次都正常退出,它也会认为「还有别人在
    用」而拒绝关库,于是数据库一直不退,残留的 postgres.exe 越积越多。

    这些死 pid 对环境已无意义,启动时顺手剔掉即可。
    """
    handles = pgdata / ".handle_pids.json"
    if not handles.exists():
        return
    try:
        pids = json.loads(handles.read_text())
    except (OSError, ValueError):
        return  # 文件损坏时交给 pgembed 自己去处理,不要在这里抛异常挡住启动
    if not isinstance(pids, list):
        return

    alive = [p for p in pids if isinstance(p, int) and psutil.pid_exists(p)]
    if len(alive) != len(pids):
        handles.write_text(json.dumps(alive))
        logger.info("清理了 %d 条已退出的数据库句柄记录", len(pids) - len(alive))


def _wait_until_pg_ready(pgdata: Path, timeout: float) -> bool:
    """轮询 postmaster.pid,等它报告 ready。

    pgembed 的 PostmasterInfo.is_running() 只看进程在不在、不看状态 ——
    崩溃恢复期间进程活着但状态是 starting。这时候把控制权交回 pgembed,
    会撞上它结尾的 `assert status == "ready"`,所以必须自己先等到真 ready。
    """
    from pgembed.utils import PostmasterInfo

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = PostmasterInfo.read_from_pgdata(pgdata)
        if info is not None and info.is_running() and info.status == "ready":
            return True
        time.sleep(1.0)
    return False


def _start_embedded_pg(pgdata: Path):
    """启动内嵌 PostgreSQL,容忍「崩溃恢复导致的启动超时」。

    关掉控制台窗口会强杀 PostgreSQL,它下次启动就得先做崩溃恢复。恢复的第一
    步是 fsync 整个数据目录,而 PostgreSQL 自己的日志文件就在数据目录里
    (pgembed 固定用 pgdata/log),Windows 上会被文件锁挡住并重试 30 秒 ——
    远超 pgembed 写死的 10 秒 pg_ctl 超时。于是它误报 "Timeout starting
    server"、应用启动失败;可实际上再等 20 秒数据库自己就 ready 了。

    所以超时后等它真正就绪,再重试一次 —— 那时 pgembed 会直接复用已在运行的
    实例,不再走 pg_ctl,也就不会再超时。
    """
    _prune_dead_handles(pgdata)

    import pgembed

    try:
        return pgembed.get_server(pgdata)
    except subprocess.TimeoutExpired:
        pass

    # 关键:pgembed 的 __init__ 是「先把实例注册进 _instances,再启动」,
    # 启动抛异常后那个坏实例仍留在缓存里,而 get_server() 命中缓存就原样返回、
    # 不会重新启动 —— 必须先摘掉它,否则重试拿到的是连 pid 都没有的空壳。
    from pgembed.postgres_server import PostgresServer

    PostgresServer._instances.pop(pgdata, None)

    logger.warning(
        "PostgreSQL 启动超过 10 秒 —— 多半是上次被强杀后在做崩溃恢复。"
        "正在等它就绪(最多 %.0f 秒),请勿关闭窗口…",
        _PG_RECOVERY_WAIT_SECONDS,
    )
    if not _wait_until_pg_ready(pgdata, _PG_RECOVERY_WAIT_SECONDS):
        raise RuntimeError(
            f"PostgreSQL 在 {_PG_RECOVERY_WAIT_SECONDS:.0f} 秒内仍未就绪。"
            f"请查看 {pgdata / 'log'} 末尾的报错,"
            f"或删除 {pgdata} 目录后重新启动(注意会清空已有数据)。"
        )
    logger.info("PostgreSQL 已恢复就绪,继续启动")
    return pgembed.get_server(pgdata)


def _ensure_embedded_pg() -> str:
    """启动项目内嵌的 PostgreSQL,确保目标数据库存在,返回同步连接串。"""
    global _server

    pgdata: Path = settings.pgdata_dir
    pgdata.mkdir(parents=True, exist_ok=True)

    if _server is None:
        logger.info("启动内嵌 PostgreSQL,数据目录: %s", pgdata)
        _server = _start_embedded_pg(pgdata)
        logger.info("PostgreSQL 已就绪,pid=%s", _server.get_pid())

    # 确保业务库存在
    admin_uri = _server.get_uri(database="postgres")
    with psycopg.connect(admin_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (settings.db_name,))
            if not cur.fetchone():
                # 库名来自配置而非用户输入,标识符用双引号包裹即可安全拼接
                cur.execute(f'CREATE DATABASE "{settings.db_name}"')
                logger.info("已创建数据库 %s", settings.db_name)

    return _server.get_uri(database=settings.db_name)


def resolve_database_url() -> str:
    """返回同步连接串:优先 .env 配置,否则拉起内嵌 PG。"""
    if settings.database_url:
        return settings.database_url
    return _ensure_embedded_pg()


def to_async_url(sync_url: str) -> str:
    """把同步连接串转成 SQLAlchemy 异步驱动形式(psycopg3)。"""
    if sync_url.startswith("postgresql+psycopg://"):
        return sync_url
    for prefix in ("postgresql+psycopg2://", "postgresql://", "postgres://"):
        if sync_url.startswith(prefix):
            return "postgresql+psycopg://" + sync_url[len(prefix):]
    return sync_url


# 引擎在 lifespan 里初始化
engine = None
SessionLocal: async_sessionmaker[AsyncSession] | None = None


async def init_engine() -> str:
    """初始化异步引擎与 sessionmaker,返回生效的连接串(已脱敏用于日志)。"""
    global engine, SessionLocal

    sync_url = resolve_database_url()
    async_url = to_async_url(sync_url)

    engine = create_async_engine(
        async_url,
        echo=False,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_pre_ping=True,  # 连接池健康检查,避免拿到失效连接
        pool_recycle=1800,
    )
    logger.info(
        "连接池: pool_size=%d max_overflow=%d pool_timeout=%ds(上限 %d 条)",
        settings.db_pool_size,
        settings.db_max_overflow,
        settings.db_pool_timeout,
        settings.db_pool_size + settings.db_max_overflow,
    )
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # 启用 pgvector 扩展(幂等)
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

    return sync_url


async def dispose_engine() -> None:
    global engine, SessionLocal
    if engine is not None:
        await engine.dispose()
        engine = None
        SessionLocal = None


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖:提供一个自动提交/回滚的会话。"""
    assert SessionLocal is not None, "引擎未初始化,应在 lifespan 中调用 init_engine()"
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_engine():
    """返回当前异步引擎,未初始化时抛错。

    init_engine() 会给模块级 engine 赋值,所以调用方必须通过本函数取,
    不能在 import 时直接 from .db import engine(那样拿到的永远是 None)。
    """
    assert engine is not None, "引擎未初始化,应在 lifespan 中调用 init_engine()"
    return engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    assert SessionLocal is not None, "会话工厂未初始化,应先调用 init_engine()"
    return SessionLocal
