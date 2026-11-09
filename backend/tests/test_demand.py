from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import numpy as np
import pandas as pd
import pytest
import time_machine
from pydantic import SecretStr
from sqlalchemy import text

from nordpool_predictor.ingestion import demand
from nordpool_predictor.ml import features, predict
from nordpool_predictor.ml.asof import load_asof_covariates
from nordpool_predictor.scheduler import jobs

RECEIVED = datetime(2026, 1, 1, 12, tzinfo=UTC)


def forecast_xml(area="DK1", curve="A01", resolution="PT60M"):
    return f"""<GL_MarketDocument
      xmlns="urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0">
      <type>A65</type><process.processType>A01</process.processType>
      <createdDateTime>2026-01-01T10:00Z</createdDateTime>
      <TimeSeries><businessType>A04</businessType><curveType>{curve}</curveType>
        <outBiddingZone_Domain.mRID>{demand.DOMAINS[area]}</outBiddingZone_Domain.mRID>
        <quantity_Measure_Unit.name>MAW</quantity_Measure_Unit.name>
        <Period><timeInterval><start>2026-01-02T00:00Z</start>
          <end>2026-01-02T03:00Z</end></timeInterval><resolution>{resolution}</resolution>
          <Point><position>1</position><quantity>100</quantity></Point>
          <Point><position>3</position><quantity>200</quantity></Point>
        </Period>
      </TimeSeries></GL_MarketDocument>""".encode()


@pytest.mark.parametrize("area", ["DK1", "DK2"])
def test_hourly_forecasts_expand_to_quarters_without_filling_missing_blocks(area):
    rows = demand._parse_forecast(forecast_xml(area), area, RECEIVED)
    assert len(rows) == 8
    assert [row["load_mw"] for row in rows] == [100] * 4 + [200] * 4
    assert [row["ts"].hour for row in rows] == [0] * 4 + [2] * 4
    assert all(row["issued_at"] == RECEIVED for row in rows)
    assert all(row["source_issued_at"] < RECEIVED for row in rows)
    assert all(row["area"] == area for row in rows)


@pytest.mark.parametrize("resolution,expected", [("PT15M", 2), ("PT30M", 4)])
def test_finer_resolutions_preserve_interval_units(resolution, expected):
    rows = demand._parse_forecast(forecast_xml(resolution=resolution), "DK1", RECEIVED)
    assert len(rows) == expected
    assert sum(row["load_mw"] for row in rows) == 150 * expected


def test_variable_blocks_extend_to_next_position_and_period_end():
    rows = demand._parse_forecast(forecast_xml(curve="A03"), "DK1", RECEIVED)
    assert [row["load_mw"] for row in rows] == [100] * 8 + [200] * 4


@pytest.mark.parametrize(
    "before,after",
    [
        ("<type>A65", "<type>A44"),
        ("<process.processType>A01", "<process.processType>A16"),
        (demand.DOMAINS["DK1"], demand.DOMAINS["DK2"]),
        (">MAW<", ">MWH<"),
        ("<businessType>A04", "<businessType>A01"),
        ("<curveType>A01", "<curveType>A99"),
        ("PT60M", "PT5M"),
        ("<quantity>100", "<quantity>nan"),
        ("<quantity>100", "<quantity>inf"),
        ("<quantity>100", "<quantity>-1"),
        ("<quantity>100", "<quantity>"),
        ("<position>3", "<position>1"),
        ("<position>3", "<position>4"),
        ("2026-01-01T10:00Z", "2026-01-01T13:00Z"),
        ("2026-01-01T10:00Z", "2026-01-01T10:00"),
        ("2026-01-02T00:00Z", "2026-01-02T00:05Z"),
        ("2026-01-02T03:00Z", "2026-01-12T03:00Z"),
    ],
)
def test_unexpected_or_invalid_source_data_is_rejected(before, after):
    with pytest.raises(ValueError):
        demand._parse_forecast(
            forecast_xml().replace(before.encode(), after.encode()), "DK1", RECEIVED
        )


def test_residual_load_requires_all_inputs_and_allows_negative_and_zero():
    frame = pd.DataFrame(
        {
            "load_mw": [100, 0, np.nan, 100, 100],
            "wind_mw": [120, 0, 10, np.nan, 10],
            "solar_mw": [10, 0, 20, 20, np.nan],
        }
    )
    result = features.add_residual_load_features(frame)
    assert result["residual_load_mw"].iloc[:2].tolist() == [-30, 0]
    assert result["residual_load_mw"].iloc[2:].isna().all()


async def test_no_token_does_not_make_requests(test_settings):
    with (
        patch.object(demand, "get_settings", return_value=test_settings),
        patch.object(demand.httpx, "AsyncClient") as client,
    ):
        await demand.ingest_demand_forecasts()
    client.assert_not_called()


async def test_ingestion_uses_header_auth_and_receipt_time(test_settings, mock_db_session):
    test_settings.entsoe_api_token = SecretStr("test-token")
    factory, session = mock_db_session
    requests = []

    def serve(request):
        requests.append(request)
        root = ET.fromstring(request.content)
        attributes = {
            node.findtext("{*}attribute"): node.findtext("{*}attributeValue")
            for node in root.findall("{*}AttributeInstanceComponent")
        }
        assert attributes["DocumentType"] == "A65"
        assert attributes["ProcessType"] == "A01"
        area = next(
            a
            for a, domain in demand.DOMAINS.items()
            if domain == attributes["OutBiddingZone_Domain"]
        )
        return httpx.Response(200, content=forecast_xml(area))

    client = httpx.AsyncClient(transport=httpx.MockTransport(serve))
    with (
        time_machine.travel(RECEIVED, tick=False),
        patch.object(demand, "get_settings", return_value=test_settings),
        patch.object(demand, "get_session", factory),
        patch.object(demand.httpx, "AsyncClient", return_value=client),
    ):
        await demand.ingest_demand_forecasts()
    assert len(requests) == 2
    assert all(r.headers["SECURITY_TOKEN"] == "test-token" for r in requests)
    assert all("test-token" not in str(r.url) and b"test-token" not in r.content for r in requests)
    assert session.commit.await_count == 2
    for call in session.execute.call_args_list:
        assert all(row["issued_at"] == RECEIVED for row in call.args[1])


async def test_demand_outage_does_not_stop_price_forecasts(test_settings):
    test_settings.entsoe_api_token = SecretStr("test-token")
    operation = AsyncMock(side_effect=RuntimeError("unavailable"))
    forecasts = AsyncMock()
    with (
        patch.object(jobs, "get_settings", return_value=test_settings),
        patch.object(jobs, "_run_job", operation),
        patch.object(jobs, "_run_area_jobs", forecasts),
    ):
        await jobs.job_refresh_forecast()
    forecasts.assert_awaited_once()


async def test_refresh_records_demand_job_before_collecting(database, test_settings):
    test_settings.entsoe_api_token = SecretStr("test-token")
    collect = AsyncMock()
    with (
        patch.object(jobs, "get_settings", return_value=test_settings),
        patch.object(demand, "ingest_demand_forecasts", collect),
        patch.object(jobs, "_run_area_jobs", AsyncMock()),
    ):
        await jobs.job_refresh_forecast()
    collect.assert_awaited_once()
    async with database.connect() as conn:
        status = (
            await conn.execute(
                text("SELECT status FROM job_runs WHERE job_type = 'ingest_demand'")
            )
        ).scalar_one()
    assert status == "completed"


async def test_ingestion_preserves_earlier_vintages(database, test_settings):
    test_settings.entsoe_api_token = SecretStr("test-token")
    test_settings.areas.pop("DK2")
    for hours, value in [(0, 100), (6, 150)]:

        def serve(request):
            return httpx.Response(
                200,
                content=forecast_xml().replace(b"<quantity>100", f"<quantity>{value}".encode()),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(serve))
        with (
            time_machine.travel(RECEIVED + pd.Timedelta(hours=hours), tick=False),
            patch.object(demand, "get_settings", return_value=test_settings),
            patch("nordpool_predictor.database.get_engine", return_value=database),
            patch.object(demand.httpx, "AsyncClient", return_value=client),
        ):
            await demand.ingest_demand_forecasts()
    async with database.connect() as conn:
        values = (
            (
                await conn.execute(
                    text("""
            SELECT load_mw FROM demand_forecasts
            WHERE area = 'DK1' AND ts = '2026-01-02 00:00Z' ORDER BY issued_at
        """)
                )
            )
            .scalars()
            .all()
        )
    assert values == [100, 150]


async def test_receipt_cutoffs_and_quarter_alignment(sync_database):
    with sync_database.begin() as conn:
        for receipt, value in [("2026-01-01 12:00Z", 100), ("2026-01-01 18:00Z", 200)]:
            conn.execute(
                text("""
                INSERT INTO demand_forecasts
                    (area, ts, issued_at, source_issued_at, load_mw, source)
                VALUES ('DK1', '2026-01-02 00:15Z', :receipt, '2026-01-01 10:00Z', :value, 'test')
            """),
                {"receipt": receipt, "value": value},
            )
    index = pd.date_range("2026-01-02 00:00Z", periods=3, freq="15min")
    frame = pd.DataFrame(index=index)
    before = load_asof_covariates(
        sync_database, frame, "DK1", RECEIVED - pd.Timedelta(seconds=1), "demand"
    )
    assert before["load_mw"].isna().all()
    first = load_asof_covariates(sync_database, frame, "DK1", RECEIVED, "demand")
    assert first["load_mw"].iloc[1] == 100
    assert first["load_mw"].iloc[[0, 2]].isna().all()
    later = load_asof_covariates(
        sync_database, frame, "DK1", RECEIVED + pd.Timedelta(hours=6), "demand"
    )
    assert later["load_mw"].iloc[1] == 200
    other = load_asof_covariates(sync_database, frame, "DK2", RECEIVED, "demand")
    assert other["load_mw"].isna().all()


async def test_demand_reaches_training_and_forecast_with_identical_cutoffs(
    sync_database, test_settings
):
    with sync_database.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO price_observations (area, ts, price_dkk_kwh, source)
            SELECT 'DK1', ts, 0.5, 'test' FROM generate_series(
                '2025-12-15 00:00Z'::timestamptz, '2026-01-03 00:00Z', INTERVAL '15 minutes') ts
        """)
        )
        conn.execute(
            text("""
            INSERT INTO production_forecasts (area, ts, issued_at, wind_mw, solar_mw, source)
            SELECT 'DK1', ts, '2026-01-01 00:00Z', 10, 20, 'test' FROM generate_series(
                '2026-01-02 00:00Z'::timestamptz, '2026-01-03 00:00Z', INTERVAL '1 hour') ts
        """)
        )
        conn.execute(
            text("""
            INSERT INTO demand_forecasts (area, ts, issued_at, source_issued_at, load_mw, source)
            SELECT 'DK1', ts, '2026-01-01 12:00Z', '2026-01-01 10:00Z', 100, 'test'
            FROM generate_series('2026-01-02 00:00Z'::timestamptz,
                '2026-01-02 23:45Z', INTERVAL '15 minutes') ts
        """)
        )
    with (
        patch.object(features, "_get_sync_engine", return_value=sync_database),
        patch("nordpool_predictor.ml.asof.get_settings", return_value=test_settings),
    ):
        training = features.build_feature_matrix(
            "DK1", datetime(2026, 1, 2, tzinfo=UTC), datetime(2026, 1, 3, tzinfo=UTC)
        )
        forecast, _ = predict._build_forecast_features("DK1", RECEIVED, 192)
    cutoffs = training.index - pd.to_timedelta(training["horizon_step"] * 15, unit="min")
    assert training["load_mw"].notna().any()
    for cutoff, row in zip(cutoffs, training.itertuples(), strict=True):
        if cutoff < RECEIVED:
            assert np.isnan(row.load_mw) and np.isnan(row.residual_load_mw)
        else:
            assert row.load_mw == 100 and row.residual_load_mw == 70
    known = forecast.loc["2026-01-02"]
    assert known["load_mw"].tolist() == [100] * 96
    assert known["residual_load_mw"].tolist() == [70] * 96
    assert forecast.loc[forecast.index < known.index.min(), "load_mw"].isna().all()
