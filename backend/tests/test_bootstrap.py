from __future__ import annotations

from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from nordpool_predictor.scheduler import bootstrap as module


@pytest.mark.parametrize("existing_forecast", [False, True])
async def test_partial_bootstrap_preserves_ready_area_and_completes_missing_area(
    test_settings,
    existing_forecast,
):
    with (
        patch.object(module, "get_settings", return_value=test_settings),
        patch.object(module, "ready_models", AsyncMock(return_value={"DK1": "v1"})),
        patch.object(module, "forecast_ready", AsyncMock(return_value=existing_forecast)),
        patch.object(module, "train_models", AsyncMock()) as train,
        patch.object(module, "run_forecast", AsyncMock()) as forecast,
        patch.object(module, "backfill_prices", AsyncMock()),
        patch.object(module, "backfill_weather", AsyncMock()),
        patch.object(module, "backfill_production", AsyncMock()),
        patch.object(module, "backfill_crossborder", AsyncMock()),
        patch.object(module, "ingest_weather_forecasts", AsyncMock()),
        patch.object(module, "ingest_production_forecasts", AsyncMock()),
    ):
        await module.bootstrap()
    train.assert_awaited_once_with("DK2")
    assert [call.args[0] for call in forecast.await_args_list] == (
        ["DK2"] if existing_forecast else ["DK1", "DK2"]
    )


@pytest.fixture
def recovery_mocks(test_settings):
    with ExitStack() as stack:
        stack.enter_context(patch.object(module, "get_settings", return_value=test_settings))
        stack.enter_context(
            patch.object(module, "ready_models", AsyncMock(return_value={"DK1": "v1"}))
        )
        stack.enter_context(patch.object(module, "forecast_ready", AsyncMock(return_value=False)))
        operations = {
            name: stack.enter_context(patch.object(module, name, AsyncMock()))
            for name in (
                "backfill_prices",
                "backfill_weather",
                "backfill_production",
                "backfill_crossborder",
                "ingest_weather_forecasts",
                "ingest_production_forecasts",
                "train_models",
                "run_forecast",
            )
        }
        yield operations


@pytest.mark.parametrize("failed", ["backfill_production", "backfill_weather"])
async def test_backfill_failure_still_recovers_area_with_usable_model(recovery_mocks, failed):
    recovery_mocks[failed].side_effect = RuntimeError("upstream failure")
    with pytest.raises(RuntimeError, match="Bootstrap incomplete"):
        await module.bootstrap()
    recovery_mocks["train_models"].assert_not_awaited()
    recovery_mocks["run_forecast"].assert_awaited_once_with("DK1")


async def test_area_weather_failure_does_not_block_other_area_or_training(recovery_mocks):
    async def weather(*, area):
        if area == "DK2":
            raise RuntimeError("DK2 unavailable")

    recovery_mocks["ingest_weather_forecasts"].side_effect = weather
    with pytest.raises(RuntimeError, match="DK2 weather forecasts"):
        await module.bootstrap()
    recovery_mocks["train_models"].assert_awaited_once_with("DK2")
    recovery_mocks["run_forecast"].assert_awaited_once_with("DK1")


async def test_live_production_failure_happens_after_training(recovery_mocks):
    recovery_mocks["ingest_production_forecasts"].side_effect = RuntimeError("unavailable")
    with pytest.raises(RuntimeError, match="production forecasts"):
        await module.bootstrap()
    recovery_mocks["train_models"].assert_awaited_once_with("DK2")
    recovery_mocks["run_forecast"].assert_not_awaited()


async def test_weather_backfill_is_isolated_per_training_area(recovery_mocks):
    async def weather(*, days, area):
        if area == "DK1":
            raise RuntimeError("DK1 unavailable")

    with patch.object(module, "ready_models", AsyncMock(return_value={})):
        recovery_mocks["backfill_weather"].side_effect = weather
        with pytest.raises(RuntimeError, match="DK1 weather backfill"):
            await module.bootstrap()
    recovery_mocks["train_models"].assert_awaited_once_with("DK2")
    recovery_mocks["run_forecast"].assert_awaited_once_with("DK2")


@pytest.mark.parametrize("price_failure", [False, True])
async def test_reused_models_restore_recent_prices_without_retraining(
    recovery_mocks, price_failure
):
    with patch.object(module, "ready_models", AsyncMock(return_value={"DK1": "v1", "DK2": "v2"})):
        if price_failure:
            recovery_mocks["backfill_prices"].side_effect = RuntimeError("prices unavailable")
            with pytest.raises(RuntimeError, match="recent price backfill"):
                await module.bootstrap()
            recovery_mocks["run_forecast"].assert_not_awaited()
        else:
            await module.bootstrap()
            assert [call.args[0] for call in recovery_mocks["run_forecast"].await_args_list] == [
                "DK1",
                "DK2",
            ]
    recovery_mocks["backfill_prices"].assert_awaited_once_with(days=8)
    recovery_mocks["train_models"].assert_not_awaited()


async def test_failed_full_price_backfill_can_still_recover_existing_model(recovery_mocks):
    async def prices(*, days):
        if days != 8:
            raise RuntimeError("full history unavailable")

    recovery_mocks["backfill_prices"].side_effect = prices
    with pytest.raises(RuntimeError, match="price backfill"):
        await module.bootstrap()
    recovery_mocks["train_models"].assert_not_awaited()
    recovery_mocks["run_forecast"].assert_awaited_once_with("DK1")


async def test_ready_models_reject_missing_artifacts(database):
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO model_versions (model_version, model_type, area, horizon_strategy,
                feature_set_version, trained_at, artifact_path, is_active)
            VALUES ('test', 'test', 'DK1', 'direct', 'v2-asof', NOW(), '/missing', TRUE)
        """)
        )
    with patch.object(module, "_load_models", side_effect=FileNotFoundError):
        assert await module.ready_models() == {}


@pytest.mark.parametrize(
    "status,model_version,future,expected",
    [
        ("completed", "v1", True, True),
        ("scored", "v1", True, True),
        ("completed", "v1", False, False),
        ("completed", "v2", True, False),
        ("failed", "v1", True, False),
        ("running", "v1", True, False),
    ],
)
async def test_forecast_readiness_uses_matching_successful_unexpired_values(
    database, status, model_version, future, expected
):
    now = datetime.now(UTC)
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO model_versions (model_version, model_type, area, horizon_strategy,
                feature_set_version, trained_at, artifact_path)
            VALUES (:version, 'test', 'DK1', 'direct', 'v2-asof', NOW(), '/tmp/test')
        """),
            {"version": model_version},
        )
        run_id = (
            await conn.execute(
                text("""
            INSERT INTO forecast_runs (area, model_version, issued_at, horizon_steps, status)
            VALUES ('DK1', :version, :issued, 1, :status) RETURNING run_id
        """),
                {"version": model_version, "issued": now, "status": status},
            )
        ).scalar_one()
        await conn.execute(
            text("""
            INSERT INTO forecast_values (run_id, ts, horizon_step, predicted_price_dkk_kwh)
            VALUES (:id, :ts, 1, 0.5)
        """),
            {"id": run_id, "ts": now + timedelta(days=1 if future else -1)},
        )
    assert await module.forecast_ready("DK1", "v1") is expected
    assert await module.forecast_ready("DK2", "v1") is False
