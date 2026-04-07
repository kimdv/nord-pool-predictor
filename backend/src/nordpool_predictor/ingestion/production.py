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

ACTUALS_DATASET = "ElectricityProdex5MinRealtime"
FORECASTS_DATASET = "Forecasts_Hour"
SOURCE_ACTUALS = "energidataservice_actual"
SOURCE_FORECAST = "energidataservice_forecast"

ACTUAL_COLUMNS = "Minutes5UTC,Minutes5DK,OffshoreWindPower,OnshoreWindPower,SolarPower,PriceArea"
FORECAST_COLUMNS = "HourUTC,PriceArea,ForecastDayAhead"


def _parse_actual_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rec in records:
        raw_ts = rec.get("Minutes5UTC")
        area_code = rec.get("PriceArea")
        if raw_ts is None or area_code is None:
            continue
        ts = datetime.fromisoformat(str(raw_ts)).replace(tzinfo=UTC)
        offshore = float(rec.get("OffshoreWindPower") or 0)
        onshore = float(rec.get("OnshoreWindPower") or 0)
        solar = float(rec.get("SolarPower") or 0)
        rows.append(
            {
                "area": area_code,
                "ts": ts,
                "wind_mw": offshore + onshore,
                "solar_mw": solar,
                "source": SOURCE_ACTUALS,
            }
        )
    return rows


def _parse_forecast_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rec in records:
        raw_ts = rec.get("HourUTC")
        area_code = rec.get("PriceArea")
        if raw_ts is None or area_code is None:
            continue
        ts = datetime.fromisoformat(str(raw_ts)).replace(tzinfo=UTC)
        wind_fc = rec.get("ForecastDayAhead")
        rows.append(
            {
                "area": area_code,
                "ts": ts,
                "wind_mw": float(wind_fc) if wind_fc is not None else None,
                "solar_mw": None,
                "source": SOURCE_FORECAST,
            }
        )
    return rows


async def _upsert_observations(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    stmt = text("""
        INSERT INTO production_observations (area, ts, wind_mw, solar_mw, source)
        VALUES (:area, :ts, :wind_mw, :solar_mw, :source)
        ON CONFLICT (area, ts, source) DO NOTHING
    """)
    async with get_session() as session:
        await session.execute(stmt, rows)
        await session.commit()
    return len(rows)


async def _upsert_forecasts(rows: list[dict[str, Any]], issued_at: datetime) -> int:
    if not rows:
        return 0
    for r in rows:
        r["issued_at"] = issued_at

    stmt = text("""
        INSERT INTO production_forecasts (area, issued_at, ts, wind_mw, solar_mw, source)
        VALUES (:area, :issued_at, :ts, :wind_mw, :solar_mw, :source)
        ON CONFLICT (area, issued_at, ts, source) DO NOTHING
    """)
    async with get_session() as session:
        await session.execute(stmt, rows)
        await session.commit()
    return len(rows)


async def ingest_production_actuals(days: int = 7) -> None:
    """Fetch recent actual wind/solar production data (both areas in one call)."""
    settings = get_settings()
    areas = settings.area_codes
    now = datetime.now(UTC)
    start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)

    logger.info("Ingesting production actuals for %d days, areas %s", days, areas)

    records = await eds_get(
        ACTUALS_DATASET,
        {
            "start": start.strftime("%Y-%m-%dT%H:%M"),
            "end": now.strftime("%Y-%m-%dT%H:%M"),
            "filter": json.dumps({"PriceArea": areas}),
            "columns": ACTUAL_COLUMNS,
            "sort": "Minutes5UTC asc",
            "limit": "0",
        },
    )

    if not records:
        logger.warning("No actual production records returned")
        return

    rows = _parse_actual_records(records)
    count = await _upsert_observations(rows)
    logger.info("Production actuals ingestion complete: %d rows", count)


async def ingest_production_forecasts() -> None:
    """Fetch the latest day-ahead production forecasts (both areas in one call)."""
    settings = get_settings()
    areas = settings.area_codes
    now = datetime.now(UTC)
    issued_at = now.replace(second=0, microsecond=0)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = today + timedelta(days=2)

    logger.info("Ingesting production forecasts for areas %s (%s → %s)", areas, today, end)

    records = await eds_get(
        FORECASTS_DATASET,
        {
            "start": today.strftime("%Y-%m-%dT%H:%M"),
            "end": end.strftime("%Y-%m-%dT%H:%M"),
            "filter": json.dumps({"PriceArea": areas}),
            "columns": FORECAST_COLUMNS,
            "sort": "HourUTC asc",
            "limit": "0",
        },
    )

    if not records:
        logger.warning("No production forecast records returned")
        return

    rows = _parse_forecast_records(records)
    count = await _upsert_forecasts(rows, issued_at)
    logger.info("Production forecast ingestion complete: %d rows", count)


async def backfill_production(days: int = 365) -> None:
    """Backfill historical actual production data (both areas per chunk)."""
    settings = get_settings()
    areas = settings.area_codes
    now = datetime.now(UTC)
    start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

    logger.info("Backfilling production for %d days across areas %s", days, areas)
    total = 0

    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=30), end)

        records = await eds_get(
            ACTUALS_DATASET,
            {
                "start": chunk_start.strftime("%Y-%m-%dT%H:%M"),
                "end": chunk_end.strftime("%Y-%m-%dT%H:%M"),
                "filter": json.dumps({"PriceArea": areas}),
                "columns": ACTUAL_COLUMNS,
                "sort": "Minutes5UTC asc",
                "limit": "0",
            },
        )

        rows = _parse_actual_records(records)
        count = await _upsert_observations(rows)
        total += count
        logger.info(
            "Backfill production %s→%s: %d rows",
            chunk_start.date(),
            chunk_end.date(),
            count,
        )
        chunk_start = chunk_end

    logger.info("Production backfill complete: %d total rows upserted", total)
