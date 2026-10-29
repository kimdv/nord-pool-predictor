from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from nordpool_predictor.scheduler import bootstrap as module


@pytest.mark.parametrize("existing_forecast", [False, True])
async def test_partial_bootstrap_preserves_ready_area_and_completes_missing_area(
    database,
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
