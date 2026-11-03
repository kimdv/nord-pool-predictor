from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import time_machine

from nordpool_predictor.ml import features, predict
from nordpool_predictor.ml.horizon import DEFAULT_HORIZON_STEPS
from nordpool_predictor.scheduler import jobs


async def test_default_forecast_persists_a_buffer_beyond_the_visible_week(mock_db_session):
    issued = datetime(2026, 11, 3, 18, 5, tzinfo=UTC)
    factory, session = mock_db_session
    session.execute.return_value.mappings.return_value.all.return_value = [
        {"model_version": "model", "artifact_path": "/artifacts"}
    ]
    targets = pd.date_range("2026-11-03 18:15", periods=768, freq="15min", tz=UTC)
    estimator = MagicMock(feature_name_=["feature"])
    estimator.predict.return_value = np.full(len(targets), 0.5)
    with (
        time_machine.travel(issued, tick=False),
        patch.object(predict, "get_session", factory),
        patch.object(
            predict, "_load_models", return_value=dict.fromkeys(["p10", "p50", "p90"], estimator)
        ),
        patch.object(
            predict,
            "_build_forecast_features",
            return_value=(pd.DataFrame({"feature": 0.5}, index=targets), targets),
        ) as build,
    ):
        await predict.run_forecast("DK1")
    build.assert_called_once_with("DK1", issued, 768)
    assert session.execute.call_args_list[1].args[1]["horizon_steps"] == 768
    rows = session.execute.call_args_list[2].args[1]
    assert len(rows) == 768
    assert rows[-1]["ts"] >= issued + timedelta(hours=7, days=7)


def test_training_samples_include_the_buffer_and_mask_unavailable_weekly_lags():
    index = pd.date_range("2026-01-01", periods=2000, freq="15min", tz=UTC)
    history = pd.DataFrame({features.TARGET: 0.5}, index=index)
    with ExitStack() as stack:
        stack.enter_context(patch.object(features, "load_prices", return_value=history))
        for name in (
            "add_issuance_price_features",
            "add_weather_features",
            "add_production_features",
            "add_crossborder_features",
            "add_cross_features",
            "add_residual_load_features",
        ):
            stack.enter_context(patch.object(features, name, side_effect=lambda df, *a, **kw: df))
        result = features.build_feature_matrix("DK1", index[0], index[-1])
    horizons = result[features.HORIZON_STEP_FEATURE]
    assert horizons.between(1, DEFAULT_HORIZON_STEPS).all()
    assert (horizons > 672).any()
    assert result.loc[horizons > 672, "price_lag_168h"].isna().all()


def test_forecast_refreshes_every_six_hours_including_the_existing_evening_run():
    scheduler = MagicMock()
    with (
        patch.object(jobs, "AsyncIOScheduler", return_value=scheduler),
        patch.object(jobs, "_scheduler", None),
    ):
        jobs.start_scheduler()
    refresh = next(
        call.kwargs
        for call in scheduler.add_job.call_args_list
        if call.kwargs["id"] == "refresh_forecast"
    )
    trigger = refresh["trigger"]
    first = trigger.get_next_fire_time(None, datetime(2026, 11, 3, 0, 0, tzinfo=UTC))
    fires = [first]
    for _ in range(4):
        fires.append(trigger.get_next_fire_time(fires[-1], fires[-1]))
    assert [fire.hour for fire in fires] == [6, 12, 18, 0, 6]
    assert all(fire.minute == 5 for fire in fires)
    assert all(b - a == timedelta(hours=6) for a, b in zip(fires, fires[1:]))
