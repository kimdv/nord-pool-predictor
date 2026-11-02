from __future__ import annotations

import pickle
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text

from nordpool_predictor.ml import predict
from nordpool_predictor.scheduler import jobs


@pytest.fixture
async def active_candidates(database, test_settings):
    test_settings.areas.pop("DK2")
    now = datetime.now(UTC)
    async with database.begin() as conn:
        await conn.execute(
            text("""
                INSERT INTO model_versions (model_version, model_type, area, horizon_strategy,
                    feature_set_version, trained_at, artifact_path, is_active)
                VALUES (:version, 'test', :area, 'direct', :features, :trained, :path, :active)
            """),
            [
                {
                    "version": version,
                    "area": area,
                    "features": features,
                    "trained": now - timedelta(days=days),
                    "path": f"/artifacts/{version}",
                    "active": active,
                }
                for version, area, features, days, active in [
                    ("older", "DK1", "v2-asof", 3, True),
                    ("newest", "DK1", "v2-asof", 2, True),
                    ("legacy", "DK1", "v1", 1, True),
                    ("inactive", "DK1", "v2-asof", 0, False),
                    ("foreign", "DK2", "v2-asof", 0, True),
                ]
            ],
        )


def build_features(area, issued_at, steps):
    targets = pd.date_range(
        pd.Timestamp(issued_at).floor("15min") + timedelta(minutes=15),
        periods=steps,
        freq="15min",
    )
    return pd.DataFrame({"feature": 0.5}, index=targets), targets


@pytest.mark.parametrize("artifact_error", [FileNotFoundError, pickle.UnpicklingError])
async def test_repeated_daily_refreshes_fall_back_to_usable_artifacts(
    database, test_settings, active_candidates, artifact_error
):
    loads = []

    def load(version, path, area):
        loads.append(version)
        if version == "newest":
            raise artifact_error("unusable quantile artifact")
        estimator = MagicMock(feature_name_=["feature"])
        estimator.predict.side_effect = lambda features: np.full(len(features), 0.5)
        return dict.fromkeys(["p10", "p50", "p90"], estimator)

    with (
        patch.object(jobs, "get_settings", return_value=test_settings),
        patch.object(predict, "_load_models", side_effect=load),
        patch.object(predict, "_build_forecast_features", side_effect=build_features),
    ):
        await jobs.job_refresh_forecast()
        await jobs.job_refresh_forecast()

    assert loads == ["newest", "older", "newest", "older"]
    async with database.connect() as conn:
        runs = (await conn.execute(text("SELECT model_version, status FROM forecast_runs"))).all()
        statuses = (await conn.execute(text("SELECT status FROM job_runs"))).scalars().all()
    assert runs == [("older", "completed"), ("older", "completed")]
    assert statuses == ["completed", "completed"]


@pytest.mark.parametrize("pinned", [False, True])
async def test_unusable_models_report_failure_without_creating_a_forecast(
    database, test_settings, active_candidates, pinned
):
    with (
        patch.object(jobs, "get_settings", return_value=test_settings),
        patch.object(predict, "_load_models", side_effect=FileNotFoundError("missing")) as load,
    ):
        if pinned:
            with pytest.raises(predict.ModelUnavailableError):
                await predict.run_forecast("DK1", model_version="newest")
        else:
            with pytest.raises(RuntimeError, match="refresh_forecast failed for areas: DK1"):
                await jobs.job_refresh_forecast()

    assert [call.args[0] for call in load.call_args_list] == (
        ["newest"] if pinned else ["newest", "older"]
    )
    async with database.connect() as conn:
        assert (await conn.execute(text("SELECT COUNT(*) FROM forecast_runs"))).scalar() == 0
        if not pinned:
            assert (
                await conn.execute(text("SELECT status FROM job_runs"))
            ).scalar_one() == "failed"
