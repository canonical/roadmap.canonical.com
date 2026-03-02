"""Database connection layer — async pool for request handlers, sync for background tasks."""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager

import psycopg
from psycopg_pool import AsyncConnectionPool

from .settings import settings

# ---------------------------------------------------------------------------
# Async connection pool (used by FastAPI route handlers)
# ---------------------------------------------------------------------------

_pool: AsyncConnectionPool | None = None


async def open_pool() -> None:
    """Create and open the async connection pool.  Called during app startup."""
    global _pool  # noqa: PLW0603
    _pool = AsyncConnectionPool(
        conninfo=settings.database_url,
        min_size=2,
        max_size=10,
        open=False,
    )
    await _pool.open()


async def close_pool() -> None:
    """Close the async connection pool.  Called during app shutdown."""
    global _pool  # noqa: PLW0603
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def get_async_conn():
    """Yield an async connection from the pool."""
    if _pool is None:
        raise RuntimeError("Async connection pool is not open — call open_pool() first")
    async with _pool.connection() as conn:
        yield conn


# ---------------------------------------------------------------------------
# Sync connection (used by background tasks / Jira sync / schema setup)
# ---------------------------------------------------------------------------


@contextmanager
def get_db_connection():
    """Yield a plain synchronous psycopg connection; closes on exit."""
    conn = psycopg.connect(settings.database_url)
    try:
        yield conn
    finally:
        conn.close()
