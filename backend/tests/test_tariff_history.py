from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
import time_machine
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from nordpool_predictor.api.routers import tariffs as router
from nordpool_predictor.tariffs import TariffUnavailableError, build_price_breakdown


def result(rows):
    value = MagicMock()
    value.mappings.return_value.all.return_value = rows
    value.mappings.return_value.first.return_value = None
    return value


@pytest.fixture
def setup_history(monkeypatch, test_settings, mock_db_session):
    factory, session = mock_db_session
    monkeypatch.setattr(router, "get_settings", lambda: test_settings)
    monkeypatch.setattr(router, "get_session", factory)
    from nordpool_predictor import tariffs

    async def hourly(gln, code, day):
        return [day.day / 100] * 24

    monkeypatch.setattr(tariffs, "_fetch_latest_tariff", hourly)
    monkeypatch.setattr(tariffs, "_fetch_nettab_tariff", AsyncMock(return_value=[0.0] * 24))
    monkeypatch.setattr(tariffs, "_fetch_flat_tariff", AsyncMock(return_value=0.0))
    return session


@pytest.mark.parametrize("instant", ["2026-03-30T12:00:00Z", "2026-10-26T12:00:00Z"])
async def test_history_is_672_actual_slots_with_date_specific_tariffs(setup_history, instant):
    end = datetime.fromisoformat(instant)
    start = end - timedelta(hours=168)
    rows = [{"ts": start + timedelta(minutes=15 * i), "price_dkk_kwh": 0.0} for i in range(672)]
    today = [
        r
        for r in rows
        if r["ts"].astimezone(router.CPH_TZ).date() == end.astimezone(router.CPH_TZ).date()
    ]
    setup_history.execute.side_effect = [result(today), result([]), result(rows)]
    with time_machine.travel(end, tick=False):
        response = await router.price_breakdown("DK1", "5790000000000", "CD", history_end=end)
    history = response.historical_slots
    assert len(history) == 672
    assert datetime.fromisoformat(history[0].ts) == start
    assert datetime.fromisoformat(history[-1].ts) == end - timedelta(minutes=15)
    assert all(s.spot_source == "actual" and s.spot_price == 0 for s in history)
    for slot in history:
        day = datetime.fromisoformat(slot.ts).astimezone(router.CPH_TZ).day
        assert slot.total_incl_vat == pytest.approx(day / 100 * 1.25)


async def test_historical_failure_leaves_today_and_other_days_available(
    setup_history, monkeypatch
):
    end = datetime(2026, 11, 4, 12, tzinfo=UTC)
    rows = [{"ts": end - timedelta(days=d), "price_dkk_kwh": -0.5} for d in [1, 2]]
    setup_history.execute.side_effect = [result([]), result([]), result(rows)]

    async def build(*args, day, **kwargs):
        if day.day == 2:
            raise TariffUnavailableError("missing historical tariffs")
        return await build_price_breakdown(*args, day=day, **kwargs)

    monkeypatch.setattr(router, "build_price_breakdown", build)
    with time_machine.travel(end, tick=False):
        response = await router.price_breakdown("DK1", "5790000000000", "CD", end)
    assert len(response.slots) == 96
    assert len(response.historical_slots) == 1
    assert response.historical_slots[0].spot_price == -0.5


async def test_no_history_parameter_preserves_today_only(setup_history):
    setup_history.execute.side_effect = [result([]), result([])]
    with time_machine.travel("2026-11-04T12:00:00Z", tick=False):
        response = await router.price_breakdown("DK1", "5790000000000", "CD")
    assert len(response.slots) == 96
    assert response.historical_slots == []
    assert setup_history.execute.await_count == 2


@pytest.mark.parametrize(
    "end",
    [
        "2026-11-04T12:01:00Z",
        "2026-11-04T12:00:01Z",
        "2026-11-04T12:15:00Z",
        "2026-11-04T12:00:00",
    ],
)
async def test_endpoint_rejects_invalid_history_end(setup_history, end):
    app = FastAPI()
    app.include_router(router.router)
    with time_machine.travel("2026-11-04T12:08:00Z", tick=False):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/tariffs/breakdown/DK1",
                params={
                    "gln": "5790000000000",
                    "code": "CD",
                    "history_end": end,
                },
            )
    assert response.status_code == 422
    setup_history.execute.assert_not_awaited()


async def test_same_day_history_never_includes_forecast_fallback(setup_history):
    end = datetime(2026, 11, 4, 12, tzinfo=UTC)
    actual = {"ts": end - timedelta(minutes=30), "price_dkk_kwh": 0.0}
    run = result([])
    run.mappings.return_value.first.return_value = {"run_id": "test"}
    forecast = {"ts": end - timedelta(minutes=15), "predicted_price_dkk_kwh": -99.0}
    setup_history.execute.side_effect = [
        result([actual]),
        run,
        result([forecast]),
        result([actual]),
    ]
    with time_machine.travel(end, tick=False):
        response = await router.price_breakdown("DK1", "5790000000000", "CD", end)
    assert any(slot.spot_source == "forecast" for slot in response.slots)
    assert len(response.historical_slots) == 1
    assert response.historical_slots[0].spot_price == 0
    assert response.historical_slots[0].spot_source == "actual"
