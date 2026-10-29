from __future__ import annotations

from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from unittest.mock import AsyncMock, patch

import pytest
import time_machine
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from nordpool_predictor import tariffs
from nordpool_predictor.api.routers import homeassistant
from nordpool_predictor.api.routers.tariffs import price_breakdown


async def seed_forecast(database, issued, timestamps):
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO model_versions (model_version, model_type, area, horizon_strategy,
                feature_set_version, trained_at, artifact_path)
            VALUES ('test', 'test', 'DK1', 'direct', 'v2-asof', NOW(), '/tmp/test')
        """)
        )
        run_id = (
            await conn.execute(
                text("""
            INSERT INTO forecast_runs (area, model_version, issued_at, horizon_steps, status)
            VALUES ('DK1', 'test', :issued, 672, 'completed') RETURNING run_id
        """),
                {"issued": issued},
            )
        ).scalar_one()
        await conn.execute(
            text("""
            INSERT INTO forecast_values (run_id, ts, horizon_step, predicted_price_dkk_kwh)
            VALUES (:id, :ts, :step, 2.0)
        """),
            [{"id": run_id, "ts": ts, "step": i + 1} for i, ts in enumerate(timestamps)],
        )


@pytest.mark.parametrize(
    "instant,count",
    [
        ("2026-03-29T01:05:00Z", 92),
        ("2026-10-25T01:05:00Z", 100),
        ("2026-04-17T22:05:00Z", 96),
    ],
)
async def test_ha_preserves_zero_and_uses_complete_copenhagen_days(
    database,
    test_settings,
    instant,
    count,
):
    now = datetime.fromisoformat(instant)
    today = now.astimezone(tariffs.CPH_TZ).date()
    start = datetime.combine(today, dtime.min, tzinfo=tariffs.CPH_TZ).astimezone(UTC)
    current = now.replace(minute=now.minute // 15 * 15, second=0)
    timestamps = [start + timedelta(minutes=15 * i) for i in range(count)]
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO price_observations (area, ts, price_dkk_kwh, source)
            VALUES ('DK1', :ts, :price, 'test')
        """),
            [{"ts": ts, "price": 0 if ts == current else 0.1} for ts in timestamps]
            + [{"ts": start - timedelta(minutes=15), "price": 99}],
        )
    await seed_forecast(database, now - timedelta(days=1), [current])
    app = FastAPI()
    app.include_router(homeassistant.router)
    with (
        patch.object(homeassistant, "get_settings", return_value=test_settings),
        time_machine.travel(instant),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/ha/DK1")
    body = response.json()
    assert body["state"] == 0
    assert body["attributes"]["current_price_source"] == "actual"
    assert body["attributes"]["today_coverage"] == 1
    assert body["attributes"]["today_average"] == round(0.1 * (count - 1) / count, 4)
    assert body["attributes"]["today_max"] == 0.1


async def test_ha_reports_unavailable_when_both_actual_and_forecast_are_missing(
    database, test_settings
):
    app = FastAPI()
    app.include_router(homeassistant.router)
    with patch.object(homeassistant, "get_settings", return_value=test_settings):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/ha/DK1")
    assert response.status_code == 200
    assert response.json()["state"] == "unavailable"
    assert response.json()["attributes"]["available"] is False
    assert response.json()["attributes"]["today_average"] is None


async def test_autumn_tariffs_fill_all_100_slots_and_label_forecasts(database, test_settings):
    from nordpool_predictor.api.routers import tariffs as router

    start = datetime(2026, 10, 24, 22, tzinfo=UTC)
    timestamps = [start + timedelta(minutes=15 * i) for i in range(100)]
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO price_observations (area, ts, price_dkk_kwh, source)
            VALUES ('DK1', :ts, 0.0, 'test')
        """),
            [{"ts": ts} for ts in timestamps[:96]],
        )
    await seed_forecast(database, start, timestamps[96:])
    with (
        patch.object(router, "get_settings", return_value=test_settings),
        patch.object(tariffs, "_fetch_latest_tariff", AsyncMock(return_value=[0.5] * 24)),
        patch.object(tariffs, "_fetch_nettab_tariff", AsyncMock(return_value=[0.1] * 24)),
        patch.object(tariffs, "_fetch_flat_tariff", AsyncMock(return_value=0.05)),
        time_machine.travel("2026-10-25T10:00:00Z"),
    ):
        response = await price_breakdown("DK1", gln="5790000000000", code="CD")
    assert len(response.slots) == 100
    assert all(
        slot.spot_source == "actual" and slot.spot_price == 0 for slot in response.slots[:96]
    )
    assert all(slot.spot_source == "forecast" for slot in response.slots[96:])


async def test_ha_can_serve_actual_zero_without_any_model(database, test_settings):
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO price_observations (area, ts, price_dkk_kwh, source)
            VALUES ('DK1', :ts, 0, 'test')
        """),
            {"ts": now},
        )
    with (
        patch.object(homeassistant, "get_settings", return_value=test_settings),
        time_machine.travel(now),
    ):
        response = await homeassistant.ha_sensor("DK1")
    assert response["state"] == 0
    assert response["attributes"]["available"] is True
    assert response["attributes"]["model_version"] is None
    assert response["attributes"]["today_average"] is None
