from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from nordpool_predictor.ingestion.prices import _parse_rows
from nordpool_predictor.ingestion.production import (
    _parse_actual_records,
    _parse_forecast_records,
)
from nordpool_predictor.ingestion.weather import _parse_hourly


@pytest.mark.parametrize("operation", ["ingest_weather_forecasts", "backfill_weather"])
async def test_weather_requests_can_be_scoped_to_one_area(test_settings, operation):
    from nordpool_predictor.ingestion import weather

    with (
        patch.object(weather, "get_settings", return_value=test_settings),
        patch.object(weather, "_request_with_retry", AsyncMock(return_value={})) as request,
    ):
        kwargs = {"area": "DK2"}
        if operation == "backfill_weather":
            kwargs["days"] = 1
        await getattr(weather, operation)(**kwargs)
    request.assert_awaited_once()
    assert request.await_args.args[2]["latitude"] == "55.0"


# ---------------------------------------------------------------------------
# Price parsing — DayAheadPrices (native 15-minute rows)
# ---------------------------------------------------------------------------


class TestParseDayAheadRows:
    def test_four_quarters_preserved(self):
        records = [
            {"TimeUTC": "2026-04-06T12:00:00", "PriceArea": "DK1", "DayAheadPriceDKK": 400.0},
            {"TimeUTC": "2026-04-06T12:15:00", "PriceArea": "DK1", "DayAheadPriceDKK": 500.0},
            {"TimeUTC": "2026-04-06T12:30:00", "PriceArea": "DK1", "DayAheadPriceDKK": 600.0},
            {"TimeUTC": "2026-04-06T12:45:00", "PriceArea": "DK1", "DayAheadPriceDKK": 500.0},
        ]
        rows = _parse_rows(records)
        assert len(rows) == 4
        assert rows[0]["ts"] == datetime(2026, 4, 6, 12, 0, tzinfo=UTC)
        assert rows[0]["price_dkk_kwh"] == pytest.approx(0.4)
        assert rows[1]["ts"] == datetime(2026, 4, 6, 12, 15, tzinfo=UTC)
        assert rows[1]["price_dkk_kwh"] == pytest.approx(0.5)
        assert rows[2]["price_dkk_kwh"] == pytest.approx(0.6)
        assert rows[3]["price_dkk_kwh"] == pytest.approx(0.5)

    def test_mwh_to_kwh_conversion(self):
        records = [
            {"TimeUTC": "2025-06-01T00:00:00", "PriceArea": "DK2", "DayAheadPriceDKK": 1000.0},
        ]
        rows = _parse_rows(records)
        assert rows[0]["price_dkk_kwh"] == pytest.approx(1.0)

    def test_multiple_hours_and_areas(self):
        records = [
            {"TimeUTC": "2026-04-06T12:00:00", "PriceArea": "DK1", "DayAheadPriceDKK": 1000.0},
            {"TimeUTC": "2026-04-06T12:15:00", "PriceArea": "DK1", "DayAheadPriceDKK": 1000.0},
            {"TimeUTC": "2026-04-06T12:00:00", "PriceArea": "DK2", "DayAheadPriceDKK": 800.0},
            {"TimeUTC": "2026-04-06T13:00:00", "PriceArea": "DK1", "DayAheadPriceDKK": 600.0},
        ]
        rows = _parse_rows(records)
        assert len(rows) == 4

    def test_missing_fields_skipped(self):
        records = [
            {"TimeUTC": "2026-04-06T12:00:00", "PriceArea": "DK1"},
            {"TimeUTC": "2026-04-06T13:00:00", "DayAheadPriceDKK": 500.0},
            {"PriceArea": "DK1", "DayAheadPriceDKK": 500.0},
        ]
        assert _parse_rows(records) == []

    def test_none_values_skipped(self):
        records = [{"TimeUTC": None, "PriceArea": None, "DayAheadPriceDKK": None}]
        assert _parse_rows(records) == []

    def test_empty_records(self):
        assert _parse_rows([]) == []

    def test_source_field(self):
        records = [
            {"TimeUTC": "2025-06-01T00:00:00", "PriceArea": "DK1", "DayAheadPriceDKK": 250.0},
        ]
        rows = _parse_rows(records)
        assert rows[0]["source"] == "energidataservice"


# ---------------------------------------------------------------------------
# Weather parsing
# ---------------------------------------------------------------------------


class TestWeatherParseHourly:
    def test_valid_data(self):
        data = {
            "hourly": {
                "time": ["2025-01-15T12:00", "2025-01-15T13:00"],
                "temperature_2m": [5.0, 6.0],
                "wind_speed_10m": [3.5, 4.0],
                "cloud_cover": [80, 90],
                "precipitation": [0.0, 0.5],
                "shortwave_radiation": [100.0, 110.0],
            }
        }
        rows = _parse_hourly(data, "DK1_TEST")

        assert len(rows) == 2
        assert rows[0]["area"] == "DK1_TEST"
        assert rows[0]["temperature_c"] == 5.0
        assert rows[0]["wind_speed_ms"] == 3.5
        assert rows[0]["cloud_cover_pct"] == 80
        assert rows[0]["precipitation_mm"] == 0.0
        assert rows[0]["solar_irradiance_wm2"] == 100.0
        assert rows[0]["source"] == "open_meteo"

    def test_empty_hourly_data(self):
        assert _parse_hourly({"hourly": {"time": []}}, "DK1_TEST") == []

    def test_no_hourly_key(self):
        assert _parse_hourly({}, "DK1_TEST") == []

    def test_partial_arrays_fallback_to_none(self):
        data = {
            "hourly": {
                "time": ["2025-01-15T12:00", "2025-01-15T13:00"],
                "temperature_2m": [5.0],
                "wind_speed_10m": [],
                "cloud_cover": [],
                "precipitation": [],
                "shortwave_radiation": [],
            }
        }
        rows = _parse_hourly(data, "DK1_TEST")

        assert len(rows) == 2
        assert rows[0]["temperature_c"] == 5.0
        assert rows[1]["temperature_c"] is None

    def test_missing_variable_keys(self):
        data = {"hourly": {"time": ["2025-01-15T12:00"]}}
        rows = _parse_hourly(data, "DK1_TEST")

        assert len(rows) == 1
        assert rows[0]["temperature_c"] is None
        assert rows[0]["wind_speed_ms"] is None
        assert rows[0]["cloud_cover_pct"] is None
        assert rows[0]["precipitation_mm"] is None
        assert rows[0]["solar_irradiance_wm2"] is None


# ---------------------------------------------------------------------------
# Production parsing
# ---------------------------------------------------------------------------


class TestProductionForecastParsing:
    def test_forecast_types_aggregated_per_hour(self):
        records = [
            {
                "HourUTC": "2026-05-11T21:00:00",
                "PriceArea": "DK1",
                "ForecastType": "Offshore Wind",
                "ForecastCurrent": 10.0,
                "ForecastDayAhead": 8.0,
            },
            {
                "HourUTC": "2026-05-11T21:00:00",
                "PriceArea": "DK1",
                "ForecastType": "Onshore Wind",
                "ForecastCurrent": 20.0,
                "ForecastDayAhead": 18.0,
            },
            {
                "HourUTC": "2026-05-11T21:00:00",
                "PriceArea": "DK1",
                "ForecastType": "Solar",
                "ForecastCurrent": 5.0,
                "ForecastDayAhead": 4.0,
            },
        ]

        for rec in records:
            rec["TimestampUTC"] = "2026-05-11T20:00:00"
        rows = _parse_forecast_records(records)

        assert rows == [
            {
                "area": "DK1",
                "ts": datetime(2026, 5, 11, 21, 0, tzinfo=UTC),
                "issued_at": datetime(2026, 5, 11, 20, 0, tzinfo=UTC),
                "wind_mw": 30.0,
                "solar_mw": 5.0,
                "source": "energidataservice_forecast_v2",
            }
        ]

    def test_forecast_parser_falls_back_to_day_ahead(self):
        records = [
            {
                "HourUTC": "2026-05-11T21:00:00",
                "PriceArea": "DK2",
                "ForecastType": "Solar",
                "ForecastCurrent": None,
                "ForecastDayAhead": 7.5,
                "TimestampUTC": "2026-05-11T20:00:00",
            }
        ]

        rows = _parse_forecast_records(records)

        assert rows[0]["wind_mw"] is None
        assert rows[0]["solar_mw"] == 7.5

    def test_missing_issuance_is_not_invented(self):
        assert (
            _parse_forecast_records(
                [
                    {
                        "HourUTC": "2026-05-11T21:00:00",
                        "PriceArea": "DK1",
                        "ForecastType": "Solar",
                        "ForecastCurrent": 1,
                    }
                ]
            )
            == []
        )


@pytest.mark.parametrize(
    "values,wind,solar",
    [
        ({}, None, None),
        ({"OffshoreWindPower": 0, "OnshoreWindPower": 0, "SolarPower": 0}, 0, 0),
        ({"OffshoreWindPower": 3}, 3, None),
        ({"OffshoreWindPower": 3, "OnshoreWindPower": 4, "SolarPower": 2}, 7, 2),
    ],
)
def test_production_actuals_preserve_missing_values_and_zeroes(values, wind, solar):
    row = _parse_actual_records(
        [
            {
                "Minutes5UTC": "2026-05-11T21:00:00",
                "PriceArea": "DK1",
                **values,
            }
        ]
    )[0]
    assert row["wind_mw"] == wind
    assert row["solar_mw"] == solar


def test_production_issuance_is_per_area_target_and_snapshot():
    rows = _parse_forecast_records(
        [
            {
                "HourUTC": target,
                "PriceArea": area,
                "TimestampUTC": issued,
                "ForecastType": kind,
                "ForecastCurrent": value,
            }
            for area, target, issued, kind, value in [
                ("DK1", "2026-05-11T21:00:00", "2026-05-11T20:00:00", "Onshore Wind", 2),
                ("DK2", "2026-05-11T21:00:00", "2026-05-08T20:00:00", "Solar", 3),
                ("DK1", "2026-05-11T22:00:00", "2026-05-08T20:00:00", "Solar", 4),
                ("DK1", "2026-05-11T21:00:00", "2026-05-08T20:00:00", "Offshore Wind", 5),
            ]
        ]
    )
    assert len(rows) == 4
    assert [row["issued_at"].day for row in rows] == [11, 8, 8, 8]
    assert rows[0]["wind_mw"] == 2


@pytest.mark.parametrize("value", [None, 0])
def test_empty_forecast_snapshot_is_preserved_without_inventing_zero(value):
    row = _parse_forecast_records(
        [
            {
                "HourUTC": "2026-05-11T21:00:00",
                "PriceArea": "DK1",
                "TimestampUTC": "2026-05-11T20:00:00",
                "ForecastType": "Solar",
                "ForecastCurrent": value,
            }
        ]
    )[0]
    assert row["solar_mw"] == value
    assert row["wind_mw"] is None


async def test_ingestion_does_not_let_legacy_stamps_mask_stale_area(
    database, sync_database, test_settings
):
    import pandas as pd
    from sqlalchemy import text

    from nordpool_predictor.api.routers import health
    from nordpool_predictor.ingestion import production
    from nordpool_predictor.ml.asof import load_asof_covariates
    from nordpool_predictor.ml.features import _query_production_fc

    now = datetime.now(UTC)
    target = now.replace(minute=0, second=0, microsecond=0)
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO production_forecasts (area, ts, issued_at, wind_mw, source)
            VALUES ('DK2', :ts, :issued, 99, 'energidataservice_forecast')
        """),
            {"ts": target, "issued": now},
        )
    records = [
        {
            "PriceArea": area,
            "HourUTC": (target + timedelta(hours=offset)).isoformat(),
            "TimestampUTC": (now - timedelta(days=days)).isoformat(),
            "ForecastType": "Wind",
            "ForecastCurrent": 7,
        }
        for area, days in [("DK1", 0), ("DK2", 3)]
        for offset in [0, 1]
    ]
    with (
        patch.object(production, "get_settings", return_value=test_settings),
        patch.object(production, "eds_get", AsyncMock(return_value=records)),
    ):
        await production.ingest_production_forecasts()
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO production_observations (area, ts, wind_mw, source)
            VALUES ('DK1', :ts, 1, 'test'), ('DK2', :ts, 1, 'test')
        """),
            {"ts": now},
        )
    with patch.object(health, "get_settings", return_value=test_settings):
        report = await health.health_check()
    assert next(source for source in report.sources if source.source == "production").is_stale
    async with database.connect() as conn:
        rows = (
            await conn.execute(
                text("""
            SELECT area, issued_at FROM production_forecasts
            WHERE source = 'energidataservice_forecast_v2' ORDER BY area, ts
        """)
            )
        ).all()
    assert [row.issued_at for row in rows] == [
        now,
        now,
        now - timedelta(days=3),
        now - timedelta(days=3),
    ]
    values = load_asof_covariates(
        sync_database, pd.DataFrame(index=pd.DatetimeIndex([target])), "DK2", now, "production"
    )
    assert values["wind_mw"].iloc[0] == 7
    assert _query_production_fc(sync_database, "DK2", target, target)["wind_mw"].iloc[0] == 7


async def test_actual_ingestion_repairs_previously_fabricated_zeroes(database, test_settings):
    from sqlalchemy import text

    from nordpool_predictor.ingestion import production

    now = datetime.now(UTC)
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO production_observations (area, ts, wind_mw, solar_mw, source)
            VALUES ('DK1', :ts, 0, 0, 'energidataservice_actual')
        """),
            {"ts": now},
        )
    with (
        patch.object(production, "get_settings", return_value=test_settings),
        patch.object(
            production,
            "eds_get",
            AsyncMock(
                return_value=[
                    {
                        "Minutes5UTC": now.isoformat(),
                        "PriceArea": "DK1",
                    }
                ]
            ),
        ),
    ):
        await production.ingest_production_actuals()
    async with database.connect() as conn:
        row = (
            await conn.execute(text("SELECT wind_mw, solar_mw FROM production_observations"))
        ).one()
    assert tuple(row) == (None, None)


# ---------------------------------------------------------------------------
# EDS shared client
# ---------------------------------------------------------------------------


class TestEdsClient:
    @patch("nordpool_predictor.ingestion.eds_client._enforce_rate_limit", new_callable=AsyncMock)
    async def test_returns_records_on_success(self, mock_rate):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "records": [
                {"HourUTC": "2025-01-15T12:00:00", "PriceArea": "DK1", "SpotPriceDKK": 500}
            ]
        }

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()

        from nordpool_predictor.ingestion.eds_client import _request_with_retry

        records = await _request_with_retry(
            mock_client, "https://example.com/api", {}, "TestDataset"
        )
        assert len(records) == 1
        assert records[0]["PriceArea"] == "DK1"

    @patch("nordpool_predictor.ingestion.eds_client._enforce_rate_limit", new_callable=AsyncMock)
    async def test_empty_records_list(self, mock_rate):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"records": []}

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        from nordpool_predictor.ingestion.eds_client import _request_with_retry

        result = await _request_with_retry(
            mock_client, "https://example.com/api", {}, "TestDataset"
        )
        assert result == []

    @patch("nordpool_predictor.ingestion.eds_client._enforce_rate_limit", new_callable=AsyncMock)
    async def test_missing_records_key(self, mock_rate):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {}

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        from nordpool_predictor.ingestion.eds_client import _request_with_retry

        result = await _request_with_retry(
            mock_client, "https://example.com/api", {}, "TestDataset"
        )
        assert result == []

    @patch("nordpool_predictor.ingestion.eds_client._enforce_rate_limit", new_callable=AsyncMock)
    async def test_retries_on_transport_error(self, mock_rate):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"records": [{"ok": True}]}

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=[httpx.TransportError("timeout"), mock_response])

        from nordpool_predictor.ingestion.eds_client import _request_with_retry

        records = await _request_with_retry(
            mock_client, "https://example.com/api", {}, "TestDataset"
        )
        assert len(records) == 1
        assert mock_client.get.call_count == 2

    @patch("nordpool_predictor.ingestion.eds_client._enforce_rate_limit", new_callable=AsyncMock)
    async def test_timezone_utc_injected(self, mock_rate):
        """eds_get should always inject timezone=UTC into params."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"records": []}

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()

        from nordpool_predictor.ingestion.eds_client import eds_get

        await eds_get("TestDataset", {"start": "now-P1D"}, client=mock_client)

        call_kwargs = mock_client.get.call_args
        assert call_kwargs.kwargs["params"]["timezone"] == "UTC"


@pytest.mark.parametrize("status", [429, 503])
async def test_exhausted_http_retries_raise_without_a_final_sleep(status):
    from nordpool_predictor.ingestion.eds_client import MAX_RETRIES, _request_with_retry

    response = httpx.Response(status, request=httpx.Request("GET", "https://example.com/api"))
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = response
    with patch("nordpool_predictor.ingestion.eds_client.asyncio.sleep", AsyncMock()) as sleep:
        with pytest.raises(httpx.HTTPStatusError) as error:
            await _request_with_retry(client, "https://example.com/api", {}, "TestDataset")
    assert error.value.response.status_code == status
    assert client.get.await_count == MAX_RETRIES
    assert sleep.await_count == MAX_RETRIES - 1


async def test_exhausted_upstream_failure_marks_the_ingestion_job_failed(database):
    from sqlalchemy import text

    from nordpool_predictor.scheduler.jobs import job_ingest_prices

    response = httpx.Response(429, request=httpx.Request("GET", "https://example.com/api"))
    with patch(
        "nordpool_predictor.ingestion.prices._fetch_chunk",
        AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "rate limited",
                request=response.request,
                response=response,
            )
        ),
    ):
        with pytest.raises(httpx.HTTPStatusError):
            await job_ingest_prices()
    async with database.connect() as conn:
        assert (await conn.execute(text("SELECT status FROM job_runs"))).scalar_one() == "failed"
