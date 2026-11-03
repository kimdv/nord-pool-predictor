from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url

from nordpool_predictor.config import Settings


@pytest.mark.parametrize(
    "driver,expected",
    [
        ("postgresql", "postgresql+psycopg2"),
        ("postgresql+asyncpg", "postgresql+psycopg2"),
        ("postgresql+psycopg2", "postgresql+psycopg2"),
        ("postgresql+psycopg", "postgresql+psycopg"),
        ("postgresql+pg8000", "postgresql+pg8000"),
    ],
)
def test_sync_url_preserves_connection_components(driver, expected):
    original = URL.create(
        driver,
        username="user@name",
        password="p@ss:/?#%+asyncpg",
        host="::1",
        port=5433,
        database="prices",
        query={"application_name": "trainer+asyncpg", "options": "-csearch_path=public"},
    )
    configured = original.render_as_string(hide_password=False)
    settings = Settings(database_url=configured, _env_file=None)
    assert make_url(settings.sync_database_url) == original.set(drivername=expected)
    assert settings.database_url == configured


def test_sync_engine_loads_installed_driver_without_connecting():
    settings = Settings(_env_file=None, database_url="postgresql+asyncpg://u:p@localhost/db")
    engine = create_engine(settings.sync_database_url)
    try:
        assert engine.dialect.driver == "psycopg2"
        assert engine.dialect.dbapi.__name__ == "psycopg2"
    finally:
        engine.dispose()
