from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest
import time_machine
from sqlalchemy import text

from nordpool_predictor.ingestion import prices
from nordpool_predictor.ml import predict
from nordpool_predictor.scheduler import bootstrap


@pytest.mark.parametrize("rollover_during", ["weather", "coverage", "other_area_recovery"])
async def test_midnight_recovery_uses_validated_inputs_and_repairs_expired_forecasts(
    database, sync_database, test_settings, rollover_during
):
    before = datetime(2026, 1, 10, 23, 59, tzinfo=UTC)
    after = datetime(2026, 1, 11, 0, 1, tzinfo=UTC)
    versions = {"DK1": "v1", "DK2": "v2"}
    validated = {}
    prediction_inputs = {}

    async with database.begin() as conn:
        await conn.execute(
            text("""
                INSERT INTO model_versions (model_version, model_type, area, horizon_strategy,
                    feature_set_version, trained_at, artifact_path, is_active)
                VALUES (:version, 'test', :area, 'direct', 'v2-asof', NOW(), '/tmp/test', TRUE)
            """),
            [{"area": area, "version": version} for area, version in versions.items()],
        )
        if rollover_during == "other_area_recovery":
            run_id = (
                await conn.execute(
                    text("""
                        INSERT INTO forecast_runs
                            (area, model_version, issued_at, horizon_steps, status)
                        VALUES ('DK1', 'v1', :issued, 672, 'completed') RETURNING run_id
                    """),
                    {"issued": before - timedelta(hours=1)},
                )
            ).scalar_one()
            await conn.execute(
                text("""
                    INSERT INTO forecast_values
                        (run_id, ts, horizon_step, predicted_price_dkk_kwh)
                    VALUES (:id, :expiry, 672, 0.5)
                """),
                {"id": run_id, "expiry": after.replace(minute=0)},
            )

    async def fetch(start, end, areas):
        return [
            {"area": area, "ts": ts.to_pydatetime(), "price_dkk_kwh": 0.5, "source": "test"}
            for area in areas
            for ts in pd.date_range(start, end, freq="15min", inclusive="left")
        ]

    def load_models(version, path, area):
        def prediction(features):
            prediction_inputs[area] = features.copy()
            return np.full(len(features), 0.5)

        estimator = MagicMock(feature_name_=["price_lag_24h", "price_rolling_mean_24h"])
        estimator.predict.side_effect = prediction
        return dict.fromkeys(["p10", "p50", "p90"], estimator)

    verify = bootstrap.verify_price_history
    with (
        time_machine.travel(before, tick=False) as clock,
        patch.object(bootstrap, "get_settings", return_value=test_settings),
        patch.object(prices, "get_settings", return_value=test_settings),
        patch.object(prices, "_fetch_chunk", AsyncMock(side_effect=fetch)),
        patch.object(predict, "_load_models", side_effect=load_models),
        patch.object(bootstrap, "ingest_production_forecasts", AsyncMock()),
        patch.object(bootstrap, "ingest_production_actuals", AsyncMock()),
        patch.object(bootstrap, "ingest_crossborder_flows", AsyncMock()),
        patch("nordpool_predictor.ml.features._get_sync_engine", return_value=sync_database),
        patch("nordpool_predictor.ml.asof.get_settings", return_value=test_settings),
    ):

        async def weather(*, area):
            if (rollover_during == "weather" and area == "DK1") or (
                rollover_during == "other_area_recovery" and area == "DK2"
            ):
                clock.move_to(after)

        async def coverage(area, *, issued_at):
            await verify(area, issued_at=issued_at)
            validated[area] = issued_at
            if rollover_during == "coverage" and area == "DK1":
                clock.move_to(after)

        with (
            patch.object(bootstrap, "ingest_weather_forecasts", side_effect=weather),
            patch.object(bootstrap, "verify_price_history", side_effect=coverage),
        ):
            await bootstrap.bootstrap()
        for area, version in versions.items():
            assert await bootstrap.forecast_ready(area, version, as_of=after)

    assert set(prediction_inputs) == {"DK1", "DK2"}
    for area, features in prediction_inputs.items():
        assert features["price_lag_24h"].iloc[:96].notna().all()
        assert features["price_rolling_mean_24h"].notna().all()
    async with database.connect() as conn:
        runs = (
            (
                await conn.execute(
                    text("""
                    SELECT DISTINCT ON (area) area, issued_at, status
                    FROM forecast_runs ORDER BY area, issued_at DESC
                """)
                )
            )
            .mappings()
            .all()
        )
    assert len(runs) == 2
    for run in runs:
        assert run["issued_at"] == validated[run["area"]]
        assert run["status"] == "completed"
    if rollover_during == "coverage":
        assert validated["DK1"] == before
        assert validated["DK2"] == after
    elif rollover_during == "weather":
        assert validated["DK1"] == after
    else:
        assert list(prediction_inputs) == ["DK2", "DK1"]
