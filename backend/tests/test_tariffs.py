"""Unit tests for the Copenhagen-aware tariff breakdown builder.

These tests exist specifically to lock in the timezone and DST behaviour that
powers the "Elpriser i dag" chart on the dashboard.  They monkeypatch the
three EDS tariff lookups and freeze the clock with ``time-machine`` so the
tests are hermetic and don't touch the network."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

import pytest
import time_machine

from nordpool_predictor import tariffs

CPH = ZoneInfo("Europe/Copenhagen")


@pytest.fixture
def stub_tariffs(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Replace EDS-backed tariff lookups with deterministic in-memory values."""

    async def _flat(_code: str, fallback: float) -> float:
        return fallback

    async def _hourly(_gln: str, _code: str) -> list[float]:
        return [0.5] * 24

    async def _nettab(_area: str) -> list[float]:
        return [0.1] * 24

    monkeypatch.setattr(tariffs, "_fetch_flat_tariff", _flat)
    monkeypatch.setattr(tariffs, "_fetch_latest_tariff", _hourly)
    monkeypatch.setattr(tariffs, "_fetch_nettab_tariff", _nettab)
    yield


def _utc_of(date: str, hour: int, minute: int) -> str:
    """Return the UTC ISO key of a Copenhagen-local wall-clock instant."""
    y, m, d = (int(p) for p in date.split("-"))
    return datetime(y, m, d, hour, minute, tzinfo=CPH).astimezone(UTC).isoformat()


class TestBuildPriceBreakdown:
    """End-to-end behaviour of ``build_price_breakdown``.

    We assert:
      1. slot count matches Copenhagen local day length (DST-aware);
      2. first/last slot keys map to the correct UTC timestamps;
      3. ``hour``/``minute`` fields are Copenhagen-local;
      4. the hourly Energinet tariff index follows Copenhagen local hour.
    """

    @pytest.mark.asyncio
    async def test_normal_cest_day_has_96_slots(self, stub_tariffs: None) -> None:
        with time_machine.travel("2026-04-17T06:00:00+00:00"):
            slots = await tariffs.build_price_breakdown(
                area="DK1",
                gln="5790000000000",
                charge_type_code="CD",
                spot_prices={},
            )

        assert len(slots) == 96
        assert slots[0]["hour"] == 0
        assert slots[0]["minute"] == 0
        assert slots[0]["ts"] == _utc_of("2026-04-17", 0, 0)
        assert slots[-1]["hour"] == 23
        assert slots[-1]["minute"] == 45
        assert slots[-1]["ts"] == _utc_of("2026-04-17", 23, 45)

    @pytest.mark.asyncio
    async def test_normal_cet_day_has_96_slots(self, stub_tariffs: None) -> None:
        with time_machine.travel("2026-01-15T08:00:00+00:00"):
            slots = await tariffs.build_price_breakdown(
                area="DK1",
                gln="5790000000000",
                charge_type_code="CD",
                spot_prices={},
            )

        assert len(slots) == 96
        assert slots[0]["ts"] == _utc_of("2026-01-15", 0, 0)
        assert slots[-1]["ts"] == _utc_of("2026-01-15", 23, 45)

    @pytest.mark.asyncio
    async def test_spring_forward_day_has_92_slots(self, stub_tariffs: None) -> None:
        """Last Sunday of March: local clock jumps 02:00 CET → 03:00 CEST.

        The Copenhagen day is 23 hours long, so 92 fifteen-minute slots."""
        with time_machine.travel("2026-03-29T06:00:00+00:00"):
            slots = await tariffs.build_price_breakdown(
                area="DK1",
                gln="5790000000000",
                charge_type_code="CD",
                spot_prices={},
            )

        assert len(slots) == 92
        assert slots[0]["ts"] == _utc_of("2026-03-29", 0, 0)
        assert slots[-1]["ts"] == _utc_of("2026-03-29", 23, 45)

        local_hours = {(s["hour"], s["minute"]) for s in slots}
        assert (1, 45) in local_hours
        assert (3, 0) in local_hours
        assert (2, 0) not in local_hours
        assert (2, 30) not in local_hours

    @pytest.mark.asyncio
    async def test_fall_back_day_has_100_slots(self, stub_tariffs: None) -> None:
        """Last Sunday of October: local clock rolls 03:00 CEST → 02:00 CET.

        The Copenhagen day is 25 hours long, so 100 fifteen-minute slots."""
        with time_machine.travel("2026-10-25T06:00:00+00:00"):
            slots = await tariffs.build_price_breakdown(
                area="DK1",
                gln="5790000000000",
                charge_type_code="CD",
                spot_prices={},
            )

        assert len(slots) == 100
        assert slots[0]["ts"] == _utc_of("2026-10-25", 0, 0)

        ts_values = [s["ts"] for s in slots]
        assert ts_values == sorted(ts_values)
        assert len(set(ts_values)) == len(ts_values)

    @pytest.mark.asyncio
    async def test_spot_prices_aligned_by_utc_key(self, stub_tariffs: None) -> None:
        """Spot prices are matched against the slot's UTC ISO timestamp."""
        with time_machine.travel("2026-04-17T06:00:00+00:00"):
            target_key = _utc_of("2026-04-17", 10, 15)
            spot_prices = {target_key: 1.2345}
            slots = await tariffs.build_price_breakdown(
                area="DK1",
                gln="5790000000000",
                charge_type_code="CD",
                spot_prices=spot_prices,
            )

        matching = [s for s in slots if s["ts"] == target_key]
        assert len(matching) == 1
        assert matching[0]["spot_price"] == pytest.approx(1.2345)
        assert matching[0]["hour"] == 10
        assert matching[0]["minute"] == 15

    @pytest.mark.asyncio
    async def test_hour_index_is_copenhagen_local(self, stub_tariffs: None) -> None:
        """The per-hour grid tariff is indexed by the slot's Copenhagen hour.

        We vary the hourly grid tariff and assert each slot receives the
        value indexed by its local hour."""
        hourly = [0.01 * i for i in range(24)]

        async def _hourly(_gln: str, _code: str) -> list[float]:
            return hourly

        async def _nettab(_area: str) -> list[float]:
            return [0.0] * 24

        async def _flat(_code: str, fallback: float) -> float:
            return fallback

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tariffs, "_fetch_latest_tariff", _hourly)
            mp.setattr(tariffs, "_fetch_nettab_tariff", _nettab)
            mp.setattr(tariffs, "_fetch_flat_tariff", _flat)
            with time_machine.travel("2026-04-17T06:00:00+00:00"):
                slots = await tariffs.build_price_breakdown(
                    area="DK1",
                    gln="5790000000000",
                    charge_type_code="CD",
                    spot_prices={},
                )

        for slot in slots:
            assert slot["grid_tariff"] == pytest.approx(hourly[slot["hour"]])


class TestCphTzExport:
    """``CPH_TZ`` is re-exported for callers that need Copenhagen boundaries."""

    def test_is_copenhagen_zoneinfo(self) -> None:
        assert tariffs.CPH_TZ.key == "Europe/Copenhagen"

    def test_today_roundtrip_is_utc_aligned(self) -> None:
        """Converting Copenhagen midnight to UTC and back is lossless."""
        today = datetime.now(tariffs.CPH_TZ).date()
        local_midnight = datetime.combine(today, dtime.min, tzinfo=tariffs.CPH_TZ)
        round_trip = local_midnight.astimezone(UTC).astimezone(tariffs.CPH_TZ)
        assert round_trip == local_midnight
        assert (round_trip + timedelta(days=1)).date() == today + timedelta(days=1)
