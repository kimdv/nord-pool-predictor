from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from sqlalchemy import text

from nordpool_predictor.ml.score import score_forecasts
from nordpool_predictor.ml.train import evaluate_holdout


def test_evaluation_fits_only_development_rows_and_predicts_holdout():
    development = pd.DataFrame({"feature": [1, 2, 3]})
    holdout = pd.DataFrame({"feature": [4, 5]})
    estimator = MagicMock()
    estimator.predict.return_value = [0, 0]
    with patch("nordpool_predictor.ml.train.lgb.LGBMRegressor", return_value=estimator):
        metrics = evaluate_holdout(
            {}, development, pd.Series([1, 2, 3]), holdout, pd.Series([2, 4])
        )
    assert estimator.fit.call_args.args[0] is development
    assert estimator.predict.call_args.args[0] is holdout
    assert metrics["mae"] == 3


async def test_partial_forecast_is_rescored_until_all_horizons_are_complete(database):
    issued = datetime(2026, 1, 1, tzinfo=UTC)
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
            VALUES (:id, :ts, :step, 0.5)
        """),
            [
                {"id": run_id, "ts": issued + timedelta(minutes=15 * step), "step": step}
                for step in range(1, 673)
            ],
        )
        await conn.execute(
            text("""
            INSERT INTO price_observations (area, ts, price_dkk_kwh, source)
            VALUES ('DK1', :ts, 0.6, 'test')
        """),
            [{"ts": issued + timedelta(minutes=15 * step)} for step in range(1, 337)],
        )
    await score_forecasts()
    async with database.connect() as conn:
        row = (await conn.execute(text("SELECT * FROM forecast_run_metrics"))).mappings().one()
        assert row["coverage_168h"] == 0.5
        assert row["mae_168h"] is None
        assert row["mae_24h"] == pytest.approx(0.1)
        assert (
            await conn.execute(text("SELECT status FROM forecast_runs"))
        ).scalar() == "completed"
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO price_observations (area, ts, price_dkk_kwh, source)
            VALUES ('DK1', :ts, 0.7, 'test')
        """),
            [{"ts": issued + timedelta(minutes=15 * step)} for step in range(337, 673)],
        )
    await score_forecasts()
    await score_forecasts()
    async with database.connect() as conn:
        row = (await conn.execute(text("SELECT * FROM forecast_run_metrics"))).mappings().one()
        assert row["coverage_168h"] == 1
        assert row["mae_168h"] == pytest.approx(0.15)
        assert (await conn.execute(text("SELECT status FROM forecast_runs"))).scalar() == "scored"
        assert (await conn.execute(text("SELECT COUNT(*) FROM forecast_errors"))).scalar() == 672
