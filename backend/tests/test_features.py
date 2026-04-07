from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from nordpool_predictor.ml.features import (
    TARGET,
    add_calendar_features,
    add_cross_features,
    add_price_lag_features,
)


def _quarter_index(start: str, periods: int) -> pd.DatetimeIndex:
    return pd.date_range(start, periods=periods, freq="15min", tz=UTC)


# ---------------------------------------------------------------------------
# Calendar features
# ---------------------------------------------------------------------------


class TestAddCalendarFeatures:
    def test_sin_cos_bounded(self):
        idx = _quarter_index("2025-01-06", periods=672)
        df = pd.DataFrame({TARGET: np.random.default_rng(42).random(672)}, index=idx)
        result = add_calendar_features(df)

        for col in ("hour_sin", "hour_cos", "dow_sin", "dow_cos", "quarter_sin", "quarter_cos"):
            assert result[col].min() >= -1.0 - 1e-10
            assert result[col].max() <= 1.0 + 1e-10

    def test_sin_cos_pythagorean_identity(self):
        idx = _quarter_index("2025-01-01", periods=192)
        df = pd.DataFrame({TARGET: np.ones(192)}, index=idx)
        result = add_calendar_features(df)

        hour_identity = result["hour_sin"] ** 2 + result["hour_cos"] ** 2
        np.testing.assert_allclose(hour_identity.values, 1.0, atol=1e-10)

        dow_identity = result["dow_sin"] ** 2 + result["dow_cos"] ** 2
        np.testing.assert_allclose(dow_identity.values, 1.0, atol=1e-10)

        quarter_identity = result["quarter_sin"] ** 2 + result["quarter_cos"] ** 2
        np.testing.assert_allclose(quarter_identity.values, 1.0, atol=1e-10)

    def test_is_weekend_known_dates(self):
        idx = pd.DatetimeIndex(
            [
                datetime(2025, 1, 6, 12, tzinfo=UTC),  # Monday
                datetime(2025, 1, 7, 12, tzinfo=UTC),  # Tuesday
                datetime(2025, 1, 10, 12, tzinfo=UTC),  # Friday
                datetime(2025, 1, 11, 12, tzinfo=UTC),  # Saturday
                datetime(2025, 1, 12, 12, tzinfo=UTC),  # Sunday
            ]
        )
        df = pd.DataFrame({TARGET: range(5)}, index=idx)
        result = add_calendar_features(df)

        assert result["is_weekend"].tolist() == [0, 0, 0, 1, 1]

    def test_month_column(self):
        idx = pd.DatetimeIndex(
            [
                datetime(2025, 3, 15, 0, tzinfo=UTC),
                datetime(2025, 7, 1, 0, tzinfo=UTC),
                datetime(2025, 12, 25, 0, tzinfo=UTC),
            ]
        )
        df = pd.DataFrame({TARGET: [1.0, 2.0, 3.0]}, index=idx)
        result = add_calendar_features(df)

        assert result["month"].tolist() == [3, 7, 12]

    def test_holiday_flag_christmas(self):
        idx = pd.DatetimeIndex(
            [
                datetime(2025, 12, 25, 12, tzinfo=UTC),  # Christmas Day
                datetime(2025, 12, 23, 12, tzinfo=UTC),  # regular Tuesday
            ]
        )
        df = pd.DataFrame({TARGET: [1.0, 2.0]}, index=idx)
        result = add_calendar_features(df)

        assert result["is_holiday"].iloc[0] == 1
        assert result["is_holiday"].iloc[1] == 0

    def test_quarter_feature_distinct_within_hour(self):
        idx = _quarter_index("2025-01-01T12:00", periods=4)
        df = pd.DataFrame({TARGET: [1.0, 2.0, 3.0, 4.0]}, index=idx)
        result = add_calendar_features(df)

        assert len(result["quarter_sin"].unique()) == 4


# ---------------------------------------------------------------------------
# Price lag features
# ---------------------------------------------------------------------------


class TestAddPriceLagFeatures:
    def test_lag_24h(self):
        idx = _quarter_index("2025-01-01", periods=288)
        prices = np.arange(288, dtype=float)
        df = pd.DataFrame({TARGET: prices}, index=idx)
        result = add_price_lag_features(df)

        assert result["price_lag_24h"].iloc[96] == pytest.approx(0.0)
        assert result["price_lag_24h"].iloc[192] == pytest.approx(96.0)

    def test_lag_48h(self):
        idx = _quarter_index("2025-01-01", periods=288)
        prices = np.arange(288, dtype=float)
        df = pd.DataFrame({TARGET: prices}, index=idx)
        result = add_price_lag_features(df)

        assert result["price_lag_48h"].iloc[192] == pytest.approx(0.0)

    def test_lag_168h(self):
        idx = _quarter_index("2025-01-01", periods=800)
        prices = np.arange(800, dtype=float)
        df = pd.DataFrame({TARGET: prices}, index=idx)
        result = add_price_lag_features(df)

        assert result["price_lag_168h"].iloc[672] == pytest.approx(0.0)

    def test_early_lags_are_nan(self):
        idx = _quarter_index("2025-01-01", periods=192)
        df = pd.DataFrame({TARGET: np.ones(192)}, index=idx)
        result = add_price_lag_features(df)

        assert np.isnan(result["price_lag_48h"].iloc[0])

    def test_rolling_columns_present(self):
        idx = _quarter_index("2025-01-01", periods=192)
        df = pd.DataFrame({TARGET: np.ones(192)}, index=idx)
        result = add_price_lag_features(df)

        expected = {
            "price_rolling_mean_24h",
            "price_rolling_std_24h",
            "price_rolling_mean_168h",
            "price_rolling_std_168h",
        }
        assert expected.issubset(set(result.columns))

    def test_no_target_column_unchanged(self):
        idx = _quarter_index("2025-01-01", periods=96)
        df = pd.DataFrame({"other": np.ones(96)}, index=idx)
        result = add_price_lag_features(df)

        assert "price_lag_24h" not in result.columns


# ---------------------------------------------------------------------------
# Cross (interaction) features
# ---------------------------------------------------------------------------


class TestAddCrossFeatures:
    def test_wind_x_hour_sin(self):
        idx = _quarter_index("2025-01-01", periods=3)
        df = pd.DataFrame(
            {
                "wind_speed_mean": [5.0, 10.0, 15.0],
                "hour_sin": [0.0, 0.5, -1.0],
            },
            index=idx,
        )
        result = add_cross_features(df)

        assert "wind_x_hour_sin" in result.columns
        np.testing.assert_allclose(result["wind_x_hour_sin"].values, [0.0, 5.0, -15.0])

    def test_temp_x_is_weekend(self):
        idx = _quarter_index("2025-01-01", periods=3)
        df = pd.DataFrame(
            {
                "temp_mean": [2.0, 3.0, 4.0],
                "is_weekend": [0, 1, 0],
            },
            index=idx,
        )
        result = add_cross_features(df)

        assert "temp_x_is_weekend" in result.columns
        np.testing.assert_allclose(result["temp_x_is_weekend"].values, [0.0, 3.0, 0.0])

    def test_both_interactions(self):
        idx = _quarter_index("2025-01-01", periods=2)
        df = pd.DataFrame(
            {
                "wind_speed_mean": [5.0, 10.0],
                "hour_sin": [1.0, -1.0],
                "temp_mean": [2.0, 3.0],
                "is_weekend": [0, 1],
            },
            index=idx,
        )
        result = add_cross_features(df)

        assert result["wind_x_hour_sin"].iloc[0] == pytest.approx(5.0)
        assert result["temp_x_is_weekend"].iloc[1] == pytest.approx(3.0)

    def test_missing_columns_no_error(self):
        idx = _quarter_index("2025-01-01", periods=2)
        df = pd.DataFrame({"unrelated": [1.0, 2.0]}, index=idx)
        result = add_cross_features(df)

        assert "wind_x_hour_sin" not in result.columns
        assert "temp_x_is_weekend" not in result.columns
