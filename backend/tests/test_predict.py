from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import time_machine

from nordpool_predictor.ml import predict
from nordpool_predictor.ml.features import TARGET


async def test_forecast_persists_and_uses_actual_issuance(mock_db_session):
    issued = datetime(2026, 1, 1, 12, 8, 37, tzinfo=UTC)
    targets = pd.date_range("2026-01-01 12:15", periods=2, freq="15min", tz=UTC)
    factory, session = mock_db_session
    session.execute.return_value.fetchone.return_value = ("model", "/artifacts")
    estimator = MagicMock(feature_name_=["temp_mean"])
    estimator.predict.return_value = [0.5, 0.5]
    with (
        time_machine.travel(issued, tick=False),
        patch.object(predict, "get_session", factory),
        patch.object(
            predict, "_load_models", return_value=dict.fromkeys(["p10", "p50", "p90"], estimator)
        ),
        patch.object(
            predict,
            "_build_forecast_features",
            return_value=(pd.DataFrame({"temp_mean": [7, 7]}, index=targets), targets),
        ) as build,
    ):
        await predict.run_forecast("DK1", horizon_steps=2)
    build.assert_called_once_with("DK1", issued, 2)
    assert session.execute.call_args_list[1].args[1]["issued_at"] == issued


async def test_fresh_weather_is_used_while_targets_stay_on_quarter_grid(
    sync_database, test_settings
):
    from sqlalchemy import text

    with sync_database.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO weather_forecasts (area, issued_at, ts, temperature_c, source)
            VALUES ('DK1_TEST', '2026-01-01 12:07Z', '2026-01-01 12:00Z', 7, 'test')
        """)
        )
    issued = datetime(2026, 1, 1, 12, 8, 37, tzinfo=UTC)
    with (
        patch("nordpool_predictor.ml.features._get_sync_engine", return_value=sync_database),
        patch("nordpool_predictor.ml.asof.get_settings", return_value=test_settings),
        patch.object(predict, "load_prices", return_value=pd.DataFrame(columns=[TARGET])),
    ):
        features, targets = predict._build_forecast_features("DK1", issued, 2)
    assert targets.tolist() == list(
        pd.date_range("2026-01-01 12:15", periods=2, freq="15min", tz=UTC)
    )
    assert features["temp_mean"].tolist() == [7, 7]
