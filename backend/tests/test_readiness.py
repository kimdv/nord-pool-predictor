from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import ANY, AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from nordpool_predictor.api.routers import health
from nordpool_predictor.config import WeatherPoint
from nordpool_predictor.ingestion import crossborder, production
from nordpool_predictor.ingestion.crossborder import EXCHANGE_COLUMNS
from nordpool_predictor.scheduler import bootstrap
from nordpool_predictor.scheduler.queue import enqueue_job


@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_bootstrap_completion_remains_in_job_summary(database, status):
    from nordpool_predictor.api.routers.jobs import jobs_summary

    job_id = await enqueue_job("bootstrap")
    async with database.begin() as conn:
        await conn.execute(
            text("""
            UPDATE job_requests SET status = :status, started_at = NOW(), finished_at = NOW()
            WHERE job_id = :id
        """),
            {"status": status, "id": job_id},
        )
    first = {row.job_type: row for row in await jobs_summary()}
    second = {row.job_type: row for row in await jobs_summary()}
    assert first["bootstrap"].last_status == status
    assert first["bootstrap"].last_finished_at is not None
    assert second["bootstrap"] == first["bootstrap"]


async def seed_fresh_sources(database, settings):
    async with database.begin() as conn:
        for area in settings.area_codes:
            await conn.execute(
                text("""
                    INSERT INTO price_observations (area, ts, price_dkk_kwh, source)
                    VALUES (:area, NOW(), 0.5, 'test')
                """),
                {"area": area},
            )
            await conn.execute(
                text("""
                    INSERT INTO production_observations (area, ts, wind_mw, source)
                    VALUES (:area, NOW(), 10, 'test')
                """),
                {"area": area},
            )
            for table, owner, ids in [
                ("weather_forecasts", "area", [p.id for p in settings.areas[area].weather_points]),
                ("production_forecasts", "area", [area]),
                (
                    "crossborder_observations",
                    "connection",
                    [
                        connection
                        for mapping in EXCHANGE_COLUMNS.values()
                        for connection in mapping.values()
                        if area in connection.split("-")
                    ],
                ),
            ]:
                for owner_id in ids:
                    columns = f"{owner}, ts, source"
                    values = (
                        ":owner, date_trunc('hour', NOW()) + :offset * interval '1 hour', 'test'"
                    )
                    if table.endswith("forecasts"):
                        columns += ", issued_at"
                        values += ", NOW()"
                    measurement = {
                        "weather_forecasts": "temperature_c",
                        "production_forecasts": "wind_mw",
                        "crossborder_observations": "flow_mw",
                    }[table]
                    columns += f", {measurement}"
                    values += ", 10"
                    # Seed current and next hourly buckets, including the :45 rollover.
                    for offset in [0, 1] if table.endswith("forecasts") else [0]:
                        await conn.execute(
                            text(
                                f"INSERT INTO {table} ({columns}) VALUES ({values}) "
                                "ON CONFLICT DO NOTHING"
                            ),
                            {"owner": owner_id, "offset": offset},
                        )
        await conn.execute(
            text("""
            INSERT INTO model_versions (model_version, model_type, area, horizon_strategy,
                feature_set_version, trained_at, artifact_path)
            VALUES ('test', 'test', 'DK1', 'direct', 'v2-asof', NOW(), '/tmp/test')
        """)
        )


async def seed_fresh_forecasts(database, areas):
    async with database.begin() as conn:
        for area in areas:
            run_id = (
                await conn.execute(
                    text("""
                        INSERT INTO forecast_runs
                            (area, model_version, issued_at, horizon_steps, status)
                        VALUES (:area, 'test', NOW(), 672, 'completed') RETURNING run_id
                    """),
                    {"area": area},
                )
            ).scalar_one()
            await conn.execute(
                text("""
                    INSERT INTO forecast_values (run_id, ts, horizon_step, predicted_price_dkk_kwh)
                    VALUES (:run_id, NOW() + interval '1 hour', 1, 0.5)
                """),
                {"run_id": run_id},
            )


async def test_recent_failed_forecasts_do_not_hide_stale_successes(database, test_settings):
    test_settings._areas = {"DK1": test_settings.areas["DK1"]}
    await seed_fresh_sources(database, test_settings)
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
    await seed_fresh_sources(database, test_settings)
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO forecast_runs (area, model_version, issued_at, horizon_steps, status)
            VALUES ('DK1', 'test', NOW(), 672, 'completed')
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
            # A successful DK1 run must not hide DK2's missing run.
            response = await client.get("/api/health/ready")
            assert response.status_code == 503
            assert response.json()["last_forecast_at"] is None
            async with database.begin() as conn:
                await conn.execute(
                    text("""
                    INSERT INTO forecast_runs
                        (area, model_version, issued_at, horizon_steps, status)
                    VALUES ('DK2', 'test', NOW(), 672, 'scored')
                """)
                )
                await conn.execute(
                    text("""
                    INSERT INTO forecast_values (run_id, ts, horizon_step, predicted_price_dkk_kwh)
                    SELECT run_id, NOW() - interval '1 hour', 1, 0.5 FROM forecast_runs
                    WHERE area = 'DK2'
                """)
                )
            # A successful run with only past values is still unavailable.
            assert (await client.get("/api/health/ready")).status_code == 503
            async with database.begin() as conn:
                await conn.execute(
                    text("""
                    UPDATE forecast_values SET ts = NOW() + interval '1 hour'
                    WHERE run_id IN (SELECT run_id FROM forecast_runs WHERE area = 'DK2')
                """)
                )
            assert (await client.get("/api/health/ready")).status_code == 200
            await enqueue_job("bootstrap")
            assert (await client.get("/api/health/ready")).status_code == 503
            async with database.begin() as conn:
                await conn.execute(text("UPDATE job_requests SET status = 'completed'"))
            assert (await client.get("/api/health/ready")).status_code == 200


@pytest.mark.parametrize(
    "table,owner_column,owner,source",
    [
        ("price_observations", "area", "DK2", "prices"),
        ("weather_forecasts", "area", "DK2_TEST", "weather"),
        ("weather_forecasts", "area", "DK2_EXTRA", "weather"),
        ("production_observations", "area", "DK2", "production"),
        ("production_forecasts", "area", "DK2", "production"),
        ("crossborder_observations", "connection", "DK2-SE4", "crossborder"),
        ("crossborder_observations", "connection", "DK1-NO2", "crossborder"),
    ],
)
@pytest.mark.parametrize("failure", ["missing", "historical"])
async def test_readiness_checks_each_input_by_data_time(
    database, test_settings, table, owner_column, owner, source, failure
):
    test_settings.areas["DK2"].weather_points.append(WeatherPoint("DK2_EXTRA", "Extra", 55, 12))
    await seed_fresh_sources(database, test_settings)
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
            async with database.begin() as conn:
                if failure == "missing":
                    statement = f"DELETE FROM {table} WHERE {owner_column} = :owner"
                else:
                    statement = (
                        f"UPDATE {table} SET ts = ts - interval '3 days' "
                        f"WHERE {owner_column} = :owner"
                    )
                await conn.execute(text(statement), {"owner": owner})
            response = await client.get("/api/health/ready")
    assert response.status_code == 503
    sources = {item["source"]: item for item in response.json()["sources"]}
    assert sources[source]["is_stale"]
    assert all(not item["is_stale"] for name, item in sources.items() if name != source)


@pytest.mark.parametrize("issued_offset", [timedelta(days=-3), timedelta(hours=1)])
async def test_forecast_inputs_require_recent_available_issuance(
    database, test_settings, issued_offset
):
    await seed_fresh_sources(database, test_settings)
    async with database.begin() as conn:
        for table in ["weather_forecasts", "production_forecasts"]:
            await conn.execute(
                text(f"UPDATE {table} SET issued_at = NOW() + CAST(:offset AS interval)"),
                {"offset": issued_offset},
            )
    with patch.object(health, "get_settings", return_value=test_settings):
        body = await health.health_check()
    assert {item.source for item in body.sources if item.is_stale} == {"weather", "production"}


async def test_bootstrap_restores_stale_forecast_and_observation_feeds(database, test_settings):
    test_settings._areas = {"DK1": test_settings.areas["DK1"]}
    await seed_fresh_sources(database, test_settings)
    async with database.begin() as conn:
        for table in ["production_observations", "crossborder_observations"]:
            await conn.execute(text(f"UPDATE {table} SET ts = ts - interval '3 days'"))
        await conn.execute(
            text("""
            INSERT INTO forecast_runs (area, model_version, issued_at, horizon_steps, status)
            VALUES ('DK1', 'test', NOW() - interval '27 hours', 672, 'completed')
        """)
        )
        await conn.execute(
            text("""
            INSERT INTO forecast_values (run_id, ts, horizon_step, predicted_price_dkk_kwh)
            SELECT run_id, NOW() + interval '1 day', 1, 0.5 FROM forecast_runs
        """)
        )

    async def regenerate(area, *, issued_at, model_version):
        async with database.begin() as conn:
            run_id = (
                await conn.execute(
                    text("""
                INSERT INTO forecast_runs (area, model_version, issued_at, horizon_steps, status)
                VALUES (:area, :version, :issued, 672, 'completed') RETURNING run_id
            """),
                    {"area": area, "version": model_version, "issued": issued_at},
                )
            ).scalar_one()
            await conn.execute(
                text("""
                INSERT INTO forecast_values (run_id, ts, horizon_step, predicted_price_dkk_kwh)
                VALUES (:run_id, :ts, 1, 0.5)
            """),
                {"run_id": run_id, "ts": issued_at + timedelta(days=1)},
            )

    app = FastAPI()
    app.include_router(health.router)
    with (
        patch.object(health, "get_settings", return_value=test_settings),
        patch.object(bootstrap, "get_settings", return_value=test_settings),
        patch.object(bootstrap, "ready_models", AsyncMock(return_value={"DK1": "test"})),
        patch.object(bootstrap, "backfill_prices", AsyncMock()),
        patch.object(bootstrap, "verify_price_history", AsyncMock()),
        patch.object(bootstrap, "ingest_weather_forecasts", AsyncMock()),
        patch.object(bootstrap, "ingest_production_forecasts", AsyncMock()),
        patch.object(production, "get_settings", return_value=test_settings),
        patch.object(
            production,
            "eds_get",
            AsyncMock(
                return_value=[
                    {
                        "Minutes5UTC": datetime.now(UTC)
                        .replace(second=0, microsecond=0)
                        .isoformat(),
                        "PriceArea": "DK1",
                        "OffshoreWindPower": 10,
                    }
                ]
            ),
        ),
        patch.object(
            crossborder,
            "eds_get",
            AsyncMock(
                return_value=[
                    {
                        "TimeUTC": datetime.now(UTC).replace(second=0, microsecond=0).isoformat(),
                        "PriceArea": "DK1",
                        **dict.fromkeys(EXCHANGE_COLUMNS["DK1"], 10),
                    }
                ]
            ),
        ),
        patch.object(bootstrap, "run_forecast", AsyncMock(side_effect=regenerate)) as forecast,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/api/health/ready")).status_code == 503
            assert not await bootstrap.forecast_ready("DK1", "test")
            await bootstrap.bootstrap()
            forecast.assert_awaited_once_with("DK1", issued_at=ANY, model_version="test")
            assert (await client.get("/api/health/ready")).status_code == 200


@pytest.mark.parametrize(
    "table,source,fields",
    [
        (
            "weather_forecasts",
            "weather",
            [
                "temperature_c",
                "wind_speed_ms",
                "cloud_cover_pct",
                "precipitation_mm",
                "solar_irradiance_wm2",
            ],
        ),
        ("production_forecasts", "production", ["wind_mw", "solar_mw"]),
    ],
)
async def test_latest_all_null_snapshot_is_not_ready(
    database, test_settings, table, source, fields
):
    await seed_fresh_sources(database, test_settings)
    await seed_fresh_forecasts(database, test_settings.area_codes)
    with patch.object(health, "get_settings", return_value=test_settings):
        assert (await health.health_check()).status == "ok"
    async with database.begin() as conn:
        columns = ", ".join(["area", "ts", "issued_at", "source", *fields])
        values = ", ".join(["area", "ts", "issued_at - interval '1 hour'", "source", *fields])
        await conn.execute(text(f"INSERT INTO {table} ({columns}) SELECT {values} FROM {table}"))
        assignments = ", ".join(f"{field} = NULL" for field in fields)
        await conn.execute(
            text(
                f"UPDATE {table} SET {assignments} WHERE issued_at > NOW() - interval '30 minutes'"
            )
        )
    with patch.object(health, "get_settings", return_value=test_settings):
        body = await health.health_check()
    assert body.status == "degraded"
    assert {item.source for item in body.sources if item.is_stale} == {source}


async def test_dk2_only_requires_shared_connection_but_not_dk1_external_flows(
    database, test_settings
):
    test_settings._areas = {"DK2": test_settings.areas["DK2"]}
    await seed_fresh_sources(database, test_settings)
    await seed_fresh_forecasts(database, ["DK2"])
    with patch.object(health, "get_settings", return_value=test_settings):
        body = await health.health_check()
        assert body.status == "ok"
        sources = body.sources
        assert not next(item for item in sources if item.source == "crossborder").is_stale
        async with database.begin() as conn:
            await conn.execute(
                text("DELETE FROM crossborder_observations WHERE connection = 'DK1-DK2'")
            )
        body = await health.health_check()
        assert body.status == "degraded"
        sources = body.sources
        assert next(item for item in sources if item.source == "crossborder").is_stale


@pytest.mark.parametrize(
    "table", ["weather_forecasts", "production_forecasts", "price_observations"]
)
@pytest.mark.parametrize("failed_fetch", [None, "weather", "production"])
async def test_fresh_forecasts_do_not_skip_stale_source_recovery(
    database, test_settings, table, failed_fetch
):
    await seed_fresh_sources(database, test_settings)
    await seed_fresh_forecasts(database, test_settings.area_codes)
    column = "ts" if table == "price_observations" else "issued_at"
    async with database.begin() as conn:
        await conn.execute(text(f"UPDATE {table} SET {column} = {column} - interval '3 days'"))

    async def refresh_prices(*, days):
        if table == "price_observations":
            async with database.begin() as conn:
                await conn.execute(text("UPDATE price_observations SET ts = NOW()"))

    async def refresh_production():
        if failed_fetch == "production":
            raise RuntimeError("production unavailable")
        if table == "production_forecasts":
            async with database.begin() as conn:
                await conn.execute(text("UPDATE production_forecasts SET issued_at = NOW()"))

    async def refresh_weather(*, area):
        if failed_fetch == "weather":
            raise RuntimeError("weather unavailable")
        if table == "weather_forecasts":
            async with database.begin() as conn:
                await conn.execute(
                    text("""
                    UPDATE weather_forecasts SET issued_at = NOW()
                    WHERE area = ANY(CAST(:points AS text[]))
                """),
                    {"points": [point.id for point in test_settings.areas[area].weather_points]},
                )

    app = FastAPI()
    app.include_router(health.router)
    with (
        patch.object(health, "get_settings", return_value=test_settings),
        patch.object(bootstrap, "get_settings", return_value=test_settings),
        patch.object(
            bootstrap, "ready_models", AsyncMock(return_value={"DK1": "test", "DK2": "test"})
        ),
        patch.object(bootstrap, "backfill_prices", AsyncMock(side_effect=refresh_prices)),
        patch.object(bootstrap, "ingest_production_actuals", AsyncMock()),
        patch.object(bootstrap, "ingest_crossborder_flows", AsyncMock()),
        patch.object(
            bootstrap, "ingest_weather_forecasts", AsyncMock(side_effect=refresh_weather)
        ),
        patch.object(
            bootstrap, "ingest_production_forecasts", AsyncMock(side_effect=refresh_production)
        ),
        patch.object(bootstrap, "run_forecast", AsyncMock()) as forecast,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/health/ready")
            assert response.status_code == 503
            assert any(source["is_stale"] for source in response.json()["sources"])
            assert await bootstrap.forecast_ready("DK1", "test")
            assert await bootstrap.forecast_ready("DK2", "test")
            if failed_fetch:
                with pytest.raises(RuntimeError, match="Bootstrap incomplete"):
                    await bootstrap.bootstrap()
            else:
                await bootstrap.bootstrap()
            forecast.assert_not_awaited()
            response = await client.get("/api/health/ready")
            unrecovered = (table, failed_fetch) in {
                ("weather_forecasts", "weather"),
                ("production_forecasts", "production"),
            }
            assert response.status_code == (503 if unrecovered else 200)
            sources = {source["source"]: source for source in response.json()["sources"]}
            assert not sources["prices"]["is_stale"]
