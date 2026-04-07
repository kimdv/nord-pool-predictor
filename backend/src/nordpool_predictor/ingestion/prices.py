from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from nordpool_predictor.config import get_settings
from nordpool_predictor.database import get_session
from nordpool_predictor.ingestion.eds_client import eds_get

logger = logging.getLogger(__name__)

SOURCE = "energidataservice"

DATASET = "DayAheadPrices"
COLUMNS = "TimeUTC,PriceArea,DayAheadPriceDKK"


def _parse_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parse 15-minute DayAheadPrices records (no aggregation)."""
    rows: list[dict[str, Any]] = []
    for rec in records:
        raw_ts = rec.get("TimeUTC")
        price_mwh = rec.get("DayAheadPriceDKK")
        area_code = rec.get("PriceArea")
        if raw_ts is None or price_mwh is None or area_code is None:
            continue
        ts = datetime.fromisoformat(str(raw_ts)).replace(tzinfo=UTC)
        rows.append({
            "area": area_code,
            "ts": ts,
            "price_dkk_kwh": float(price_mwh) / 1000.0,
            "source": SOURCE,
        })
    return rows


async def _upsert_prices(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    stmt = text("""
        INSERT INTO price_observations (area, ts, price_dkk_kwh, source)
        VALUES (:area, :ts, :price_dkk_kwh, :source)
        ON CONFLICT (area, ts) DO UPDATE SET price_dkk_kwh = EXCLUDED.price_dkk_kwh
    """)
    async with get_session() as session:
        await session.execute(stmt, rows)
        await session.commit()
    return len(rows)


async def _fetch_chunk(
    chunk_start: datetime,
    chunk_end: datetime,
    areas: list[str],
) -> list[dict[str, Any]]:
    records = await eds_get(DATASET, {
        "start": chunk_start.strftime("%Y-%m-%dT%H:%M"),
        "end": chunk_end.strftime("%Y-%m-%dT%H:%M"),
        "filter": json.dumps({"PriceArea": areas}),
        "columns": COLUMNS,
        "sort": "TimeUTC asc",
        "limit": "0",
    })
    return _parse_rows(records)


async def ingest_day_ahead(area: str | None = None) -> None:
    """Fetch today's and tomorrow's spot prices and upsert them."""
    settings = get_settings()
    areas = [area] if area else settings.area_codes

    now = datetime.now(UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = today + timedelta(days=2)

    logger.info("Ingesting spot prices (today + tomorrow) for %s", areas)

    rows = await _fetch_chunk(today, end, areas)

    if not rows:
        logger.warning("No spot price records returned")
        return

    count = await _upsert_prices(rows)
    logger.info("Upserted %d spot price rows", count)


async def backfill_prices(days: int = 365) -> None:
    """Fetch historical spot prices for all areas and upsert them."""
    settings = get_settings()
    areas = settings.area_codes

    now = datetime.now(UTC)
    start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

    logger.info("Backfilling prices for %d days across areas %s", days, areas)
    total = 0

    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=90), end)

        rows = await _fetch_chunk(chunk_start, chunk_end, areas)
        count = await _upsert_prices(rows)
        total += count
        logger.info(
            "Backfill prices %s→%s: %d rows",
            chunk_start.date(), chunk_end.date(), count,
        )
        chunk_start = chunk_end

    logger.info("Price backfill complete: %d total rows upserted", total)
