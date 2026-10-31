from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text

from nordpool_predictor.config import WeatherPoint
from nordpool_predictor.ml.asof import load_asof_covariates
from nordpool_predictor.ml.features import TARGET, add_issuance_price_features


def test_future_prices_cannot_change_issuance_rolling_features():
    index = pd.date_range("2026-01-01", periods=1000, freq="15min", tz=UTC)
    history = pd.DataFrame({TARGET: np.arange(1000, dtype=float)}, index=index)
    targets = pd.DataFrame(index=index[-100:])
    issued_at = index[700].to_pydatetime()
    expected = add_issuance_price_features(targets.copy(), history, issued_at)
    history.loc[history.index >= issued_at, TARGET] = 99999
    actual = add_issuance_price_features(targets.copy(), history, issued_at)
    pd.testing.assert_frame_equal(actual, expected)
    assert actual["price_rolling_mean_24h"].nunique() == 1


@pytest.mark.parametrize(
    "kind,table,owner,field,feature",
    [
        ("weather", "weather_forecasts", "area", "temperature_c", "temp_mean"),
        ("production", "production_forecasts", "area", "wind_mw", "wind_mw"),
        ("crossborder", "crossborder_forecasts", "connection", "flow_mw", "net_import_mw"),
    ],
)
async def test_covariates_select_only_snapshots_available_at_each_issuance(
    sync_database,
    test_settings,
    kind,
    table,
    owner,
    field,
    feature,
):
    source_owner = {"weather": "DK1_TEST", "production": "DK1", "crossborder": "DK1-DE"}[kind]
    with sync_database.begin() as conn:
        conn.execute(
            text(f"""
            INSERT INTO {table} ({owner}, issued_at, ts, {field}, source)
            VALUES (:owner, '2026-01-01 00:00Z', '2026-01-02 12:00Z', 10, 'test'),
                   (:owner, '2026-01-01 12:00Z', '2026-01-02 12:00Z', 20, 'test')
        """),
            {"owner": source_owner},
        )
    index = pd.date_range("2026-01-02 12:00", periods=3, freq="15min", tz=UTC)
    cutoffs = pd.Series(
        [
            datetime(2025, 12, 31, tzinfo=UTC),
            datetime(2026, 1, 1, 6, tzinfo=UTC),
            datetime(2026, 1, 1, 18, tzinfo=UTC),
        ],
        index=index,
    )
    with patch("nordpool_predictor.ml.asof.get_settings", return_value=test_settings):
        actual = load_asof_covariates(
            sync_database, pd.DataFrame(index=index), "DK1", cutoffs, kind
        )
    assert np.isnan(actual[feature].iloc[0])
    assert actual[feature].iloc[1:].tolist() == [10, 20]


@pytest.mark.parametrize(
    "cloud_values,issued_before_cutoff,expected",
    [
        pytest.param([], True, None, id="no-snapshots"),
        pytest.param([None], True, None, id="missing-cloud-measurement"),
        pytest.param([20.0], False, None, id="snapshot-after-issuance"),
        pytest.param([20.0], True, 0.0, id="single-measurement"),
        pytest.param([0.0], True, 0.0, id="valid-zero-measurement"),
        pytest.param([20.0, 40.0], True, np.sqrt(200.0), id="multiple-measurements"),
    ],
)
async def test_cloud_variance_preserves_missing_data(
    sync_database, test_settings, cloud_values, issued_before_cutoff, expected
):
    points = [
        WeatherPoint(id=f"DK1_TEST_{i}", name="Test", lat=56.0, lon=10.0)
        for i in range(max(1, len(cloud_values)))
    ]
    test_settings.areas["DK1"].weather_points = points
    issued_at = datetime(2026, 1, 1, 0 if issued_before_cutoff else 12, tzinfo=UTC)
    if cloud_values:
        with sync_database.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO weather_forecasts (area, issued_at, ts, cloud_cover_pct, source)
                    VALUES (:area, :issued, '2026-01-02 12:00Z', :cloud_cover, 'test')
                """),
                [
                    {"area": point.id, "issued": issued_at, "cloud_cover": value}
                    for point, value in zip(points, cloud_values, strict=True)
                ],
            )

    index = pd.date_range("2026-01-02 12:15", periods=1, tz=UTC)
    cutoff = datetime(2026, 1, 1, 6, tzinfo=UTC)
    with patch("nordpool_predictor.ml.asof.get_settings", return_value=test_settings):
        actual = load_asof_covariates(
            sync_database, pd.DataFrame(index=index), "DK1", cutoff, "weather"
        )
    variance = actual["cloud_cover_std"].iloc[0]
    if expected is None:
        assert pd.isna(variance)
    else:
        assert variance == pytest.approx(expected)
