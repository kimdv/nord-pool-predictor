from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from nordpool_predictor.config import get_settings

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
        )
    return _engine


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    engine = get_engine()
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session


def _find_migrations_dir() -> Path | None:
    candidates = [
        Path.cwd() / "migrations",
        Path(__file__).resolve().parent.parent.parent / "migrations",
    ]
    return next((d for d in candidates if d.is_dir()), None)


async def run_migrations() -> None:
    migrations_dir = _find_migrations_dir()
    if migrations_dir is None:
        logger.warning("Migrations directory not found")
        return

    files = sorted(migrations_dir.glob("*.sql"))
    if not files:
        logger.warning("No .sql files in %s", migrations_dir)
        return

    engine = get_engine()

    async with engine.connect() as conn:
        raw = await conn.get_raw_connection()
        raw_conn = raw.driver_connection  # type: ignore[union-attr]

        await raw_conn.execute(
            "CREATE TABLE IF NOT EXISTS _migrations_applied ("
            "  filename TEXT PRIMARY KEY,"
            "  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
            ")"
        )

        rows = await raw_conn.fetch("SELECT filename FROM _migrations_applied")
        applied = {r[0] for r in rows}

        for f in files:
            if f.name in applied:
                continue
            logger.info("Applying migration: %s", f.name)
            sql = f.read_text()
            async with raw_conn.transaction():
                await raw_conn.execute(sql)
                await raw_conn.execute(
                    "INSERT INTO _migrations_applied (filename) VALUES ($1)",
                    f.name,
                )
            logger.info("Applied migration: %s", f.name)

    logger.info("Database migrations up to date (%d files)", len(files))


async def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
