from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import numpy as np
import pandas as pd
import pytest
import time_machine
from sqlalchemy import text

from nordpool_predictor.ingestion import crossborder
from nordpool_predictor.ml import features, predict


def source_records(hour: int, scale: float = 1) -> list[dict]:
    return [
        {
            "TimeUTC": f"2026-01-01T{hour:02}:00:00",
            "PriceArea": "DK1",
            **{
                name: value * scale
                for name, value in {
                    "ExchangeGermany": 100,
                    "ExchangeNorway": -20,
                    "ExchangeSweden": 30,
                    "ExchangeGreatBelt": 10,
                    "ExchangeNetherlands": 50,
                    "ExchangeGreatBritain": -40,
                }.items()
            },
        },
        {
            "TimeUTC": f"2026-01-01T{hour:02}:00:00",
            "PriceArea": "DK2",
            "ExchangeGermany": -60 * scale,
            "ExchangeSweden": 70 * scale,
            # The provider's mirrored area value must not be counted a second time.
            "ExchangeGreatBelt": -10.01 * scale,
        },
    ]


def test_parser_keeps_all_documented_links_and_preserves_zero():
    records = source_records(9)
    records[0]["ExchangeGreatBritain"] = 0
    rows = crossborder._parse_records(records)
    flows = {row["connection"]: row["flow_mw"] for row in rows}
    assert flows == {
        "DK1-DE": 100,
        "DK1-NO2": -20,
        "DK1-SE3": 30,
        "DK1-DK2": 10,
        "DK1-NL": 50,
        "DK1-GB": 0,
        "DK2-DE": -60,
        "DK2-SE4": 70,
    }
    assert all(row["ts"] == datetime(2026, 1, 1, 9, tzinfo=UTC) for row in rows)


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), -float("inf")])
def test_nonfinite_or_missing_provider_flow_cannot_complete_a_net_import(value):
    records = source_records(9)
    records[0]["ExchangeGermany"] = value
    assert "DK1-DE" not in {row["connection"] for row in crossborder._parse_records(records)}


async def ingest_at(when: datetime, records: list[dict]) -> None:
    with (
        time_machine.travel(when, tick=False),
        patch.object(crossborder, "eds_get", AsyncMock(return_value=records)) as fetch,
    ):
        await crossborder.ingest_crossborder_flows()
    assert fetch.await_args.args[0] == "GenerationProdTypeExchange"
    assert set(crossborder.EXCHANGE_COLUMNS["DK1"]) <= set(
        fetch.await_args.args[1]["columns"].split(",")
    )


def observed_at(engine, area: str, cutoffs: list[datetime]) -> pd.Series:
    # Future target times must not cause observations at those times to be read.
    index = pd.date_range("2026-01-10", periods=len(cutoffs), freq="15min", tz=UTC)
    with patch.object(features, "_get_sync_engine", return_value=engine):
        frame = features.add_crossborder_features(
            pd.DataFrame(index=index), area, issued_at=pd.Series(cutoffs, index=index)
        )
    assert frame.columns.tolist() == ["observed_net_import_mw"]
    return frame["observed_net_import_mw"]


@pytest.mark.parametrize("area,expected", [("DK1", 130), ("DK2", 0)])
async def test_ingestion_first_seen_interval_boundaries_and_late_corrections(
    database, sync_database, area, expected
):
    receipt = datetime(2026, 1, 1, 10, 30, tzinfo=UTC)
    with patch("nordpool_predictor.database.get_engine", return_value=database):
        await ingest_at(receipt, source_records(9) + source_records(10, 2))
        cutoffs = [
            receipt - timedelta(microseconds=1),
            receipt,
            datetime(2026, 1, 1, 10, 59, 59, tzinfo=UTC),
            datetime(2026, 1, 1, 11, tzinfo=UTC),
        ]
        actual = observed_at(sync_database, area, cutoffs)
        assert pd.isna(actual.iloc[0])
        assert actual.iloc[1:].tolist() == [expected, expected, expected * 2]

        # Historical EDS revisions arrive later; re-fetching cannot rewrite what was known.
        late = datetime(2026, 1, 1, 13, tzinfo=UTC)
        await ingest_at(late, source_records(9, 999) + source_records(11, 3))
        actual = observed_at(
            sync_database,
            area,
            [receipt, late - timedelta(microseconds=1), late],
        )
        assert actual.tolist() == [expected, expected * 2, expected * 3]

    with sync_database.connect() as conn:
        first = conn.execute(
            text("""
                SELECT flow_mw, created_at FROM crossborder_observations
                WHERE connection = 'DK1-DE' AND ts = '2026-01-01 09:00Z'
            """)
        ).one()
    assert first == (100, receipt)


async def test_latest_complete_hour_falls_back_over_gaps_and_expires(database, sync_database):
    receipt = datetime(2026, 1, 1, 12, tzinfo=UTC)
    incomplete = source_records(11, 999)
    incomplete[0]["ExchangeGermany"] = None
    with patch("nordpool_predictor.database.get_engine", return_value=database):
        await ingest_at(receipt, source_records(9) + incomplete)
    boundary = datetime(2026, 1, 2, 11, tzinfo=UTC)
    result = observed_at(
        sync_database, "DK1", [receipt, boundary, boundary + timedelta(microseconds=1)]
    )
    assert result.iloc[:2].tolist() == [130, 130]
    assert pd.isna(result.iloc[2])
    with sync_database.begin() as conn:
        conn.execute(text("DELETE FROM crossborder_observations WHERE connection = 'DK1-GB'"))
    assert pd.isna(observed_at(sync_database, "DK1", [receipt]).iloc[0])


@pytest.mark.parametrize("area,expected", [("DK1", 130), ("DK2", 0)])
async def test_provider_ingestion_reaches_training_and_forecast_without_future_data(
    database, sync_database, test_settings, area, expected
):
    receipt = datetime(2026, 1, 1, 10, 30, tzinfo=UTC)
    with patch("nordpool_predictor.database.get_engine", return_value=database):
        await ingest_at(receipt, source_records(9) + source_records(10, 2))
    with sync_database.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO price_observations (area, ts, price_dkk_kwh, source)
                SELECT :area, ts, 0.5, 'test'
                FROM generate_series('2025-12-15 00:00Z'::timestamptz,
                    '2026-01-09 00:00Z', INTERVAL '15 minutes') ts
            """),
            {"area": area},
        )
        conn.execute(
            text("""
                INSERT INTO production_forecasts
                    (area, issued_at, ts, wind_mw, solar_mw, source)
                SELECT :area, '2025-12-01 00:00Z', ts, 10, 20, 'test'
                FROM generate_series('2026-01-01 00:00Z'::timestamptz,
                    '2026-01-09 00:00Z', INTERVAL '1 hour') ts
            """),
            {"area": area},
        )
        conn.execute(
            text("""
                INSERT INTO crossborder_forecasts (connection, issued_at, ts, flow_mw, source)
                SELECT 'DK1-DK2', '2025-12-01 00:00Z', ts, 999999, 'test'
                FROM generate_series('2026-01-01 00:00Z'::timestamptz,
                    '2026-01-09 00:00Z', INTERVAL '1 hour') ts
            """)
        )
    issued = datetime(2026, 1, 1, 11, tzinfo=UTC)
    with (
        patch.object(features, "_get_sync_engine", return_value=sync_database),
        patch("nordpool_predictor.ml.asof.get_settings", return_value=test_settings),
    ):
        training = features.build_feature_matrix(
            area, datetime(2026, 1, 1, 12, tzinfo=UTC), datetime(2026, 1, 9, tzinfo=UTC)
        )
        forecast, targets = predict._build_forecast_features(area, issued, 768)
        with sync_database.begin() as conn:
            conn.execute(text("DELETE FROM crossborder_forecasts"))
        without_forecasts, _ = predict._build_forecast_features(area, issued, 768)
        training_without_forecasts = features.build_feature_matrix(
            area, datetime(2026, 1, 1, 12, tzinfo=UTC), datetime(2026, 1, 9, tzinfo=UTC)
        )
    pd.testing.assert_frame_equal(forecast, without_forecasts)
    pd.testing.assert_frame_equal(training, training_without_forecasts)
    assert forecast["observed_net_import_mw"].tolist() == [expected * 2] * len(targets)
    assert forecast["total_renewable_mw"].tolist()[:4] == [30] * 4
    assert "net_import_mw" not in forecast
    assert "residual_load_proxy_mw" not in forecast
    assert "residual_load_proxy_mw" not in training
    assert training["observed_net_import_mw"].notna().any()
    cutoffs = training.index - pd.to_timedelta(training["horizon_step"] * 15, unit="min")
    for cutoff, value in zip(cutoffs, training["observed_net_import_mw"], strict=True):
        if cutoff < receipt or cutoff > datetime(2026, 1, 2, 12, tzinfo=UTC):
            assert np.isnan(value)
        elif cutoff < issued:
            assert value == expected
        else:
            assert value == expected * 2
