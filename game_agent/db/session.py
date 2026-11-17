"""异步引擎与会话工厂。

团队版用 MySQL（Linux 包内置 MariaDB），SQLite 只留给本机调试与冒烟测试。
两种方言共用一个 engine；SQLite 专用的 PRAGMA 只在 sqlite3 连接上执行。
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.engine import Engine as SyncEngine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from game_agent.config import DATABASE_URL, is_sqlite
from game_agent.db.models import Base

# SQLite 落盘强度：NORMAL（默认，WAL 下只在 checkpoint fsync）| FULL（每事务 fsync，最安全）
_SQLITE_SYNC = os.environ.get("GA_SQLITE_SYNC", "NORMAL").strip().upper()
if _SQLITE_SYNC not in ("NORMAL", "FULL"):
    _SQLITE_SYNC = "NORMAL"

_pool_kwargs: dict = {}
if not is_sqlite():
    # MySQL 会在 wait_timeout（默认 8h）后单方面掐掉空闲连接；pre_ping 之外再做
    # 定期回收，避免高峰期拿到死连接。团队并发不大，池子不需要开很大。
    import multiprocessing

    workers = max(1, int(os.environ.get("GA_WEB_WORKERS", "1") or 1))
    _pool_kwargs = {
        "pool_size": max(5, multiprocessing.cpu_count() * 2 // workers),
        "max_overflow": 10,
        "pool_recycle": 1800,
        "pool_timeout": 30,
        "connect_args": {"charset": "utf8mb4"},
    }

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    future=True,
    **_pool_kwargs,
)


def _is_sqlite_connection(con) -> bool:  # type: ignore[no-untyped-def]
    if isinstance(con, sqlite3.Connection):
        return True
    # aiosqlite 走 SQLAlchemy 适配层，拿到的是 AsyncAdapt_aiosqlite_connection 而不是原生连接
    return "sqlite" in type(con).__module__.lower()


@event.listens_for(SyncEngine, "connect")
def _sqlite_pragmas(dbapi_con, con_record):  # type: ignore[no-untyped-def]
    # 这个监听器挂在全局 Engine 上，MySQL 连接也会走到这里；PRAGMA 在 MySQL 上是语法错误。
    if not _is_sqlite_connection(dbapi_con):
        return
    cur = dbapi_con.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    # WAL：默认的 rollback journal（journal_mode=delete）每次提交都要 fsync 主库 + 建/删
    # 日志文件，与正在跑的 run 抢同一把写锁。实测单次 300KB 提交 7.1ms（delete）vs 2.7ms（WAL），
    # 而长 run 一轮要写十几条事件/消息，6.5GB 库上还常撞「database is locked」。
    # WAL 允许读不阻塞写，synchronous=NORMAL 把每次提交的 fsync 降到「只在 checkpoint 时」，
    # 断电最多丢最后几个事务（这是本地单机库可接受的取舍；要极致安全可设 GA_SQLITE_SYNC=FULL）。
    for pragma in ("journal_mode=WAL", f"synchronous={_SQLITE_SYNC}",
                   "wal_autocheckpoint=1000", "busy_timeout=15000",
                   "temp_store=MEMORY", "cache_size=-64000"):
        cur.execute(f"PRAGMA {pragma}")
    cur.close()


SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
