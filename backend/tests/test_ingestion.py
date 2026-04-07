from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from nordpool_predictor.ingestion.prices import _parse_rows
from nordpool_predictor.ingestion.weather import _parse_hourly

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
        mock_client.get = AsyncMock(
            side_effect=[httpx.TransportError("timeout"), mock_response]
        )

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
