"""Fetch tariff data from Energi Data Service DatahubPricelist and combine
with spot prices to produce a full hourly price breakdown.

All Energinet tariffs are fetched dynamically from DatahubPricelist using
their official ChargeTypeCodes.  Fallback constants are kept only for the
case where the API is unreachable.

Reference: https://www.energidataservice.dk/tso-electricity/datahubpricelist
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from nordpool_predictor.ingestion.eds_client import eds_get

logger = logging.getLogger(__name__)

_tariff_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 3600  # 1 hour


def _cache_get(key: str) -> Any | None:
    entry = _tariff_cache.get(key)
    if entry is None:
        return None
    ts, value = entry
    if time.monotonic() - ts > _CACHE_TTL:
        del _tariff_cache[key]
        return None
    return value


def _cache_set(key: str, value: Any) -> None:
    _tariff_cache[key] = (time.monotonic(), value)

DATAHUB_DATASET = "DatahubPricelist"

# Global Location Number (GLN) for Energinet, the Danish TSO.
# Used to filter DatahubPricelist for Energinet's own tariffs
# (system tariff, transmission tariff, electricity tax, grid loss).
ENERGINET_GLN = "5790000432752"

# Energinet ChargeTypeCodes (D03 tariffs)
SYSTEM_TARIFF_CODE = "41000"  # Systemtarif
TRANSMISSION_TARIFF_CODE = "40000"  # Transmissions nettarif
ELAFGIFT_CODE = "EA-001"  # Elafgift (electricity tax)
NETTAB_CODES: dict[str, str] = {"DK1": "40021", "DK2": "40023"}

# Fallback values if the API is unreachable (DKK/kWh excl. VAT, from Jan 2026)
_FALLBACK_SYSTEM_TARIFF = 0.072
_FALLBACK_TRANSMISSION_TARIFF = 0.043
_FALLBACK_ELAFGIFT = 0.008

VAT_RATE = 0.25


def _extract_hourly_prices(record: dict[str, Any]) -> list[float]:
    """Extract Price1..Price24 from a DatahubPricelist record.

    If PriceN is null, the flat rate from Price1 applies."""
    p1 = record.get("Price1") or 0.0
    prices: list[float] = []
    for i in range(1, 25):
        val = record.get(f"Price{i}")
        prices.append(val if val is not None else p1)
    return prices


async def fetch_grid_companies() -> list[dict[str, str]]:
    """Return distinct grid companies that have D03 (tariff) entries."""
    cached = _cache_get("grid_companies")
    if cached is not None:
        return cached

    records = await eds_get(DATAHUB_DATASET, {
        "start": "StartOfMonth-P6M",
        "end": "now",
        "filter": '{"ChargeType":["D03"]}',
        "columns": "ChargeOwner,GLN_Number",
        "sort": "ChargeOwner ASC",
        "limit": "0",
    }, respect_rate_limit=False)

    seen: dict[str, str] = {}
    for r in records:
        gln = r["GLN_Number"]
        if gln not in seen and gln != ENERGINET_GLN:
            seen[gln] = r["ChargeOwner"]

    result = sorted(
        [{"gln": gln, "name": name} for gln, name in seen.items()],
        key=lambda x: x["name"],
    )
    _cache_set("grid_companies", result)
    return result


async def fetch_grid_tariff_codes(gln: str) -> list[dict[str, str]]:
    """Return available D03 tariff codes for a grid company."""
    cache_key = f"tariff_codes:{gln}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    records = await eds_get(DATAHUB_DATASET, {
        "end": "now",
        "start": "StartOfMonth-P6M",
        "filter": f'{{"GLN_Number":["{gln}"],"ChargeType":["D03"]}}',
        "columns": "ChargeTypeCode,Note,Description",
        "sort": "ValidFrom DESC",
        "limit": "200",
    }, respect_rate_limit=False)

    seen: dict[str, dict[str, str]] = {}
    for r in records:
        code = r["ChargeTypeCode"]
        if code not in seen:
            seen[code] = {
                "code": code,
                "note": r.get("Note") or "",
                "description": r.get("Description") or "",
            }

    result = sorted(seen.values(), key=lambda x: x["code"])
    _cache_set(cache_key, result)
    return result


async def _fetch_latest_tariff(
    gln: str,
    charge_type_code: str,
) -> list[float]:
    """Return the 24 hourly prices for the latest valid tariff entry."""
    cache_key = f"tariff:{gln}:{charge_type_code}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    records = await eds_get(DATAHUB_DATASET, {
        "end": "now",
        "filter": f'{{"GLN_Number":["{gln}"],"ChargeTypeCode":["{charge_type_code}"]}}',
        "sort": "ValidFrom DESC",
        "limit": "1",
    }, respect_rate_limit=False)
    if not records:
        return [0.0] * 24
    result = _extract_hourly_prices(records[0])
    _cache_set(cache_key, result)
    return result


async def _fetch_flat_tariff(code: str, fallback: float) -> float:
    """Fetch a flat (non-time-of-use) Energinet tariff, with fallback."""
    prices = await _fetch_latest_tariff(ENERGINET_GLN, code)
    api_value = prices[0] if prices else 0.0
    if api_value != 0.0:
        return api_value
    logger.warning(
        "Using fallback for Energinet %s: %.4f DKK/kWh", code, fallback
    )
    return fallback


async def _fetch_nettab_tariff(area: str) -> list[float]:
    """Fetch the Energinet nettabstarif (grid loss) for today or latest available."""
    code = NETTAB_CODES.get(area, NETTAB_CODES["DK1"])
    return await _fetch_latest_tariff(ENERGINET_GLN, code)


async def build_price_breakdown(
    area: str,
    gln: str,
    charge_type_code: str,
    spot_prices: dict[str, float],
) -> list[dict[str, Any]]:
    """Build a 96-slot (15-min) price breakdown for today.

    ``spot_prices`` maps ISO timestamp string -> DKK/kWh (excl. VAT).
    Tariffs from Energinet are hourly and repeat across the 4 quarter-hour
    slots within each hour.  All returned values are DKK/kWh."""
    grid_tariff, nettab, system_tariff, transmission_tariff, elafgift = (
        await asyncio.gather(
            _fetch_latest_tariff(gln, charge_type_code),
            _fetch_nettab_tariff(area),
            _fetch_flat_tariff(SYSTEM_TARIFF_CODE, _FALLBACK_SYSTEM_TARIFF),
            _fetch_flat_tariff(TRANSMISSION_TARIFF_CODE, _FALLBACK_TRANSMISSION_TARIFF),
            _fetch_flat_tariff(ELAFGIFT_CODE, _FALLBACK_ELAFGIFT),
        )
    )

    now = datetime.now(UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    slots: list[dict[str, Any]] = []
    for slot in range(96):
        ts = today + timedelta(minutes=slot * 15)
        hour_index = slot // 4
        ts_key = ts.isoformat()
        spot = spot_prices.get(ts_key, 0.0)
        grid = grid_tariff[hour_index]
        loss = nettab[hour_index]
        transport = system_tariff + transmission_tariff + loss

        total_ex_vat = spot + grid + transport + elafgift
        vat = total_ex_vat * VAT_RATE

        slots.append({
            "hour": hour_index,
            "minute": ts.minute,
            "ts": ts_key,
            "spot_price": round(spot, 6),
            "grid_tariff": round(grid, 6),
            "system_tariff": round(system_tariff, 6),
            "transmission_tariff": round(transmission_tariff, 6),
            "grid_loss_tariff": round(loss, 6),
            "electricity_tax": round(elafgift, 6),
            "total_ex_vat": round(total_ex_vat, 6),
            "vat": round(vat, 6),
            "total_incl_vat": round(total_ex_vat + vat, 6),
        })

    return slots
