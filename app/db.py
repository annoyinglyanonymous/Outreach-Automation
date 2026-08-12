from __future__ import annotations

import asyncpg

from .config import config

_pool: asyncpg.Pool | None = None


async def init_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=config.DATABASE_URL,
            min_size=config.DB_POOL_MIN,
            max_size=config.DB_POOL_MAX,
            # The Supabase session pooler does not guarantee the same
            # backend per query, so cached prepared statements fail with a
            # misleading "prepared statement ... does not exist".
            statement_cache_size=0,
            command_timeout=60,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Pool not initialised - call init_pool() first")
    return _pool


async def healthcheck() -> bool:
    try:
        return await pool().fetchval("SELECT 1;") == 1
    except Exception:
        return False
