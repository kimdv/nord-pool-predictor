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
