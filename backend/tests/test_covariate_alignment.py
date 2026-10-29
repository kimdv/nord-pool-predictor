from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from nordpool_predictor.ml import features


@pytest.mark.parametrize("kind", ["weather", "production"])
def test_interleaved_covariates_keep_observation_priority_and_expire_gaps(kind):
    index = pd.date_range("2026-01-01", periods=16, freq="15min", tz="UTC")
    column = "temp_mean" if kind == "weather" else "wind_mw"
    obs = pd.DataFrame({column: [10.0, 20.0]}, index=index[[0, 4]])
    forecast = pd.DataFrame({column: [15.0, 999.0, 25.0]}, index=index[[2, 4, 6]])
    if kind == "production":
        obs["solar_mw"] = obs[column]
        forecast["solar_mw"] = forecast[column]
    with (
        patch.object(features, "_get_sync_engine"),
        patch.object(features, "_weather_point_ids", return_value=["DK1_TEST"]),
        patch.object(features, f"_query_{kind}_obs", return_value=obs),
        patch.object(features, f"_query_{kind}_fc", return_value=forecast),
    ):
        result = getattr(features, f"add_{kind}_features")(pd.DataFrame(index=index), "DK1")
    assert result[column].iloc[:7].tolist() == [10, 10, 15, 15, 20, 20, 25]
    assert pd.isna(result[column].iloc[-1])
    assert result.index.equals(index)
