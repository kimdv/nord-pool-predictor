from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from nordpool_predictor.config import Settings


@pytest.fixture
def test_settings(tmp_path):
    """Settings wired to a temporary areas.yaml — no .env or real DB needed."""
    areas_file = tmp_path / "areas.yaml"
    areas_file.write_text(
        "areas:\n"
        "  DK1:\n"
        "    label: Vest\n"
        "    weather_points:\n"
        "      - id: DK1_TEST\n"
        "        name: Test City\n"
        "        lat: 56.0\n"
        "        lon: 10.0\n"
        "  DK2:\n"
        "    label: Øst\n"
        "    weather_points:\n"
        "      - id: DK2_TEST\n"
        "        name: Test Town\n"
        "        lat: 55.0\n"
        "        lon: 12.0\n"
    )
    return Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/testdb",
        areas_yaml_path=str(areas_file),
    )


@pytest.fixture
def mock_db_session():
    """Return (context-manager factory, raw session mock).

    Use the factory wherever ``get_session`` is expected, and the raw mock to
    configure return values or assert calls.
    """
    session = AsyncMock()
    result = MagicMock()
    result.mappings.return_value.first.return_value = None
    result.mappings.return_value.all.return_value = []
    result.scalar.return_value = None
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()

    @asynccontextmanager
    async def _factory() -> AsyncIterator[AsyncMock]:
        yield session

    return _factory, session


@pytest.fixture
async def database():
    """Run each integration test in an isolated PostgreSQL schema."""
    import os
    import uuid

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_{uuid.uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        from unittest.mock import patch

        from nordpool_predictor.database import run_migrations

        with patch("nordpool_predictor.database.get_engine", return_value=engine):
            await run_migrations()
            yield engine
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest.fixture
async def sync_database(database):
    from sqlalchemy import create_engine, text

    async with database.connect() as conn:
        schema = (await conn.execute(text("SELECT current_schema()"))).scalar_one()
    engine = create_engine(
        database.url.set(drivername="postgresql+psycopg2"),
        connect_args={"options": f"-csearch_path={schema}"},
    )
    try:
        yield engine
    finally:
        engine.dispose()
