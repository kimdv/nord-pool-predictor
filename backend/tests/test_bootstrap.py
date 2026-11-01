from __future__ import annotations

from contextlib import ExitStack
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


@pytest.mark.parametrize("failed", ["backfill_prices", "backfill_weather"])
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
