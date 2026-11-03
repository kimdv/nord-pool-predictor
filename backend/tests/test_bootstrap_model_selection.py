from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text

from nordpool_predictor.ml import predict
from nordpool_predictor.scheduler import bootstrap


@pytest.mark.parametrize(
    "scenario", ["rejected_artifacts", "multiple_usable", "activation_changed"]
)
async def test_bootstrap_forecasts_only_with_the_validated_model(
    database, test_settings, scenario
):
    test_settings.areas.pop("DK2")
    now = datetime.now(UTC)
    other = "rejected" if scenario == "rejected_artifacts" else "other"
    async with database.begin() as conn:
        await conn.execute(
            text("""
                INSERT INTO model_versions (model_version, model_type, area, horizon_strategy,
                    feature_set_version, trained_at, artifact_path, is_active)
                VALUES (:version, 'test', 'DK1', 'direct', 'v2-asof', :trained, :path, :active)
            """),
            [
                {
                    "version": other,
                    "trained": now - timedelta(days=0 if scenario == "rejected_artifacts" else 2),
                    "path": f"/artifacts/{other}",
                    "active": scenario != "activation_changed",
                },
                {
                    "version": "selected",
                    "trained": now - timedelta(days=1),
                    "path": "/artifacts/selected",
                    "active": True,
                },
            ],
        )

    loads = []

    def load_models(version, path, area):
        loads.append(version)
        if version == "rejected":
            raise FileNotFoundError("rejected quantile artifacts")
        estimator = MagicMock(feature_name_=["feature"])
        estimator.predict.side_effect = lambda features: np.full(len(features), 0.5)
        return dict.fromkeys(["p10", "p50", "p90"], estimator)

    def build_features(area, issued_at, steps):
        targets = pd.date_range(
            pd.Timestamp(issued_at).floor("15min") + timedelta(minutes=15),
            periods=steps,
            freq="15min",
        )
        return pd.DataFrame({"feature": 0.5}, index=targets), targets

    async def weather(*, area):
        if scenario == "activation_changed":
            async with database.begin() as conn:
                await conn.execute(
                    text("""
                    UPDATE model_versions SET is_active = (model_version = 'other')
                    WHERE area = 'DK1'
                """)
                )

    with (
        patch.object(bootstrap, "get_settings", return_value=test_settings),
        patch.object(predict, "_load_models", side_effect=load_models),
        patch.object(predict, "_build_forecast_features", side_effect=build_features),
        patch.object(bootstrap, "backfill_prices", AsyncMock()),
        patch.object(bootstrap, "verify_price_history", AsyncMock()),
        patch.object(bootstrap, "ingest_production_forecasts", AsyncMock()),
        patch.object(bootstrap, "ingest_production_actuals", AsyncMock()),
        patch.object(bootstrap, "ingest_crossborder_flows", AsyncMock()),
        patch.object(bootstrap, "ingest_weather_forecasts", side_effect=weather),
    ):
        if scenario == "activation_changed":
            with pytest.raises(RuntimeError, match="DK1 forecast"):
                await bootstrap.bootstrap()
        else:
            await bootstrap.bootstrap()

    async with database.connect() as conn:
        runs = (await conn.execute(text("SELECT model_version, status FROM forecast_runs"))).all()
    if scenario == "activation_changed":
        assert loads == ["selected"]
        assert runs == []
    else:
        assert loads == (["rejected"] if scenario == "rejected_artifacts" else []) + [
            "selected",
            "selected",
        ]
        assert runs == [("selected", "completed")]
