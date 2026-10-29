from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from nordpool_predictor.api.routers import health
from nordpool_predictor.scheduler.queue import enqueue_job


async def seed_fresh_sources(database):
    async with database.begin() as conn:
        for table, columns, values in [
            ("price_observations", "area, ts, price_dkk_kwh, source", "'DK1', NOW(), 0.5, 'test'"),
            (
                "weather_forecasts",
                "area, ts, issued_at, source",
                "'DK1_TEST', NOW(), NOW(), 'test'",
            ),
            ("production_observations", "area, ts, source", "'DK1', NOW(), 'test'"),
            ("crossborder_observations", "connection, ts, source", "'DK1-DE', NOW(), 'test'"),
        ]:
            await conn.execute(text(f"INSERT INTO {table} ({columns}) VALUES ({values})"))
        await conn.execute(
            text("""
            INSERT INTO model_versions (model_version, model_type, area, horizon_strategy,
                feature_set_version, trained_at, artifact_path)
            VALUES ('test', 'test', 'DK1', 'direct', 'v2-asof', NOW(), '/tmp/test')
        """)
        )


async def test_recent_failed_forecasts_do_not_hide_stale_successes(database, test_settings):
    test_settings._areas = {"DK1": test_settings.areas["DK1"]}
    await seed_fresh_sources(database)
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO forecast_runs (area, model_version, issued_at, horizon_steps, status)
            VALUES ('DK1', 'test', NOW() - interval '2 days', 672, 'completed'),
                   ('DK1', 'test', NOW(), 672, 'failed')
        """)
        )
        await conn.execute(
            text("""
            INSERT INTO forecast_values (run_id, ts, horizon_step, predicted_price_dkk_kwh)
            SELECT run_id, NOW() + interval '1 hour', 1, 0.5 FROM forecast_runs
            WHERE status = 'completed'
        """)
        )
    app = FastAPI()
    app.include_router(health.router)
    with patch.object(health, "get_settings", return_value=test_settings):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert all(not source["is_stale"] for source in body["sources"])
    assert body["degraded"]
    assert datetime.fromisoformat(body["last_forecast_at"]) < datetime.now(UTC) - timedelta(
        hours=26
    )


async def test_ready_requires_success_for_every_area_and_completed_bootstrap(
    database, test_settings
):
    await seed_fresh_sources(database)
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO forecast_runs (area, model_version, issued_at, horizon_steps, status)
            VALUES ('DK1', 'test', NOW(), 672, 'completed'),
                   ('DK2', 'test', NOW(), 672, 'completed')
        """)
        )
        await conn.execute(
            text("""
            INSERT INTO forecast_values (run_id, ts, horizon_step, predicted_price_dkk_kwh)
            SELECT run_id, NOW() + interval '1 hour', 1, 0.5 FROM forecast_runs
        """)
        )
    app = FastAPI()
    app.include_router(health.router)
    with patch.object(health, "get_settings", return_value=test_settings):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/api/health/ready")).status_code == 200
            await enqueue_job("bootstrap")
            assert (await client.get("/api/health/ready")).status_code == 503
