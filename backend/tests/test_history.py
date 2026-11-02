from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text

from nordpool_predictor.api.routers import forecasts, models, prices


@pytest.fixture
async def history_client(database, test_settings):
    app = FastAPI()
    for router in (forecasts.router, models.router, prices.router):
        app.include_router(router)
    with (
        patch.object(forecasts, "get_settings", return_value=test_settings),
        patch.object(prices, "get_settings", return_value=test_settings),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client


async def test_forecast_pages_handle_ties_with_two_queries(database, history_client):
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO model_versions (model_version, model_type, area, horizon_strategy,
                feature_set_version, trained_at, artifact_path)
            VALUES ('test', 'test', 'DK1', 'direct', 'v2-asof', NOW(), '/tmp/test')
        """)
        )
        for identifier in (1, 2, 3):
            run_id = f"00000000-0000-0000-0000-{identifier:012d}"
            await conn.execute(
                text("""
                INSERT INTO forecast_runs
                    (run_id, area, model_version, issued_at, horizon_steps, status)
                VALUES (CAST(:id AS uuid), 'DK1', 'test', '2026-01-01 00:00Z', 1, 'completed')
            """),
                {"id": run_id},
            )
            await conn.execute(
                text("""
                INSERT INTO forecast_values (run_id, ts, horizon_step, predicted_price_dkk_kwh)
                VALUES (CAST(:id AS uuid), NOW(), 1, 0.5)
            """),
                {"id": run_id},
            )
    queries = []

    def record(_conn, _cursor, statement, _params, _context, _many):
        queries.append(statement)

    event.listen(database.sync_engine, "before_cursor_execute", record)
    try:
        first = await history_client.get("/api/forecasts/DK1?limit=2")
    finally:
        event.remove(database.sync_engine, "before_cursor_execute", record)
    assert first.status_code == 200
    assert len(queries) == 2
    second = await history_client.get(
        "/api/forecasts/DK1",
        params={
            "limit": 2,
            "cursor": first.headers["X-Next-Cursor"],
        },
    )
    assert len(first.json()) == 2
    assert len(second.json()) == 1
    assert len({row["run_id"] for row in first.json() + second.json()}) == 3
    assert all(len(row["values"]) == 1 for row in first.json() + second.json())
    assert "X-Next-Cursor" not in second.headers


async def test_price_pages_preserve_all_rows(database, history_client):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO price_observations (area, ts, price_dkk_kwh, source)
            VALUES ('DK1', :ts, 0.5, 'test')
        """),
            [{"ts": start + timedelta(minutes=15 * i)} for i in range(3)],
        )
    params = {
        "start": start.isoformat(),
        "end": (start + timedelta(days=1)).isoformat(),
        "limit": 2,
    }
    first = (await history_client.get("/api/prices/DK1", params=params)).json()
    second = (
        await history_client.get(
            "/api/prices/DK1",
            params={
                **params,
                "after": first["next_cursor"],
            },
        )
    ).json()
    assert len(first["prices"]) == 2
    assert len(second["prices"]) == 1
    assert second["next_cursor"] is None


@pytest.mark.parametrize(
    "path",
    [
        "/api/forecasts/DK1?cursor=bad",
        "/api/models?cursor=bad",
        "/api/prices/DK1?start=2026-01-02T00:00:00Z&end=2026-01-01T00:00:00Z",
        "/api/prices/DK1?start=2026-01-01T00:00:00",
        "/api/forecasts/DK1?limit=101",
    ],
)
async def test_history_rejects_invalid_bounds_and_cursors(history_client, path):
    assert (await history_client.get(path)).status_code == 422


async def test_model_pages_handle_equal_training_timestamps(database, history_client):
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO model_versions (model_version, model_type, area, horizon_strategy,
                feature_set_version, trained_at, artifact_path)
            VALUES (:version, 'test', 'DK1', 'direct', 'v2-asof', '2026-01-01 00:00Z', '/tmp/test')
        """),
            [{"version": name} for name in ("a", "b", "c")],
        )
    first = await history_client.get("/api/models?limit=2")
    second = await history_client.get(
        "/api/models",
        params={
            "limit": 2,
            "cursor": first.headers["X-Next-Cursor"],
        },
    )
    assert [row["model_version"] for row in first.json() + second.json()] == ["c", "b", "a"]
