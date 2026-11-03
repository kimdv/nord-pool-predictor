from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

from nordpool_predictor.config import get_settings
from nordpool_predictor.database import get_session
from nordpool_predictor.ingestion.eds_client import eds_get

logger = logging.getLogger(__name__)

ACTUALS_DATASET = "ElectricityProdex5MinRealtime"
FORECASTS_DATASET = "Forecasts_Hour"
SOURCE_ACTUALS = "energidataservice_actual"
SOURCE_FORECAST = "energidataservice_forecast_v2"
SOURCE_DAY_AHEAD_HISTORY = "energidataservice_day_ahead_history_v1"

ACTUAL_COLUMNS = "Minutes5UTC,Minutes5DK,OffshoreWindPower,OnshoreWindPower,SolarPower,PriceArea"
FORECAST_COLUMNS = (
    "HourUTC,PriceArea,ForecastType,ForecastCurrent,Forecast1Hour,"
    "Forecast5Hour,ForecastIntraday,ForecastDayAhead,TimestampUTC"
)


def _parse_actual_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rec in records:
        raw_ts = rec.get("Minutes5UTC")
        area_code = rec.get("PriceArea")
        if raw_ts is None or area_code is None:
            continue
        ts = datetime.fromisoformat(str(raw_ts)).replace(tzinfo=UTC)
        wind = [rec.get("OffshoreWindPower"), rec.get("OnshoreWindPower")]
        present_wind = [float(value) for value in wind if value is not None]
        solar = rec.get("SolarPower")
        rows.append(
            {
                "area": area_code,
                "ts": ts,
                "wind_mw": sum(present_wind) if present_wind else None,
                "solar_mw": float(solar) if solar is not None else None,
                "source": SOURCE_ACTUALS,
            }
        )
    return rows


def _latest_forecast_value(rec: dict[str, Any]) -> float | None:
    for key in (
        "ForecastCurrent",
        "Forecast1Hour",
        "Forecast5Hour",
        "ForecastIntraday",
        "ForecastDayAhead",
    ):
        value = rec.get(key)
        if value is not None:
            return float(value)
    return None


def _parse_forecast_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, datetime], dict[datetime, dict[str, float | None]]] = {}
    for rec in records:
        raw_ts = rec.get("HourUTC")
        area_code = rec.get("PriceArea")
        raw_issued = rec.get("TimestampUTC")
        if raw_ts is None or area_code is None or raw_issued is None:
            continue
        ts = datetime.fromisoformat(str(raw_ts)).replace(tzinfo=UTC)
        issued_at = datetime.fromisoformat(str(raw_issued)).replace(tzinfo=UTC)
        forecast_type = str(rec.get("ForecastType") or "").lower()
        if "wind" not in forecast_type and "solar" not in forecast_type:
            continue
        grouped.setdefault((area_code, ts), {}).setdefault(issued_at, {})[forecast_type] = (
            _latest_forecast_value(rec)
        )

    rows = []
    for (area, ts), updates in grouped.items():
        components: dict[str, tuple[float | None, datetime]] = {}
        for issued_at, changes in sorted(updates.items()):
            components.update({kind: (value, issued_at) for kind, value in changes.items()})
            row = {
                "area": area,
                "ts": ts,
                "issued_at": issued_at,
                "oldest_component_issued_at": min(
                    (stamp for value, stamp in components.values() if value is not None),
                    default=issued_at,
                ),
                "source": SOURCE_FORECAST,
                "components_json": {
                    name: {"value": value, "issued_at": stamp.isoformat()}
                    for name, (value, stamp) in components.items()
                },
            }
            for kind, field in [("wind", "wind_mw"), ("solar", "solar_mw")]:
                values = [
                    value
                    for name, (value, _) in components.items()
                    if kind in name and value is not None
                ]
                row[field] = sum(values) if values else None
            rows.append(row)
    return rows


async def _upsert_observations(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    stmt = text("""
        INSERT INTO production_observations (area, ts, wind_mw, solar_mw, source)
        VALUES (:area, :ts, :wind_mw, :solar_mw, :source)
        ON CONFLICT (area, ts, source) DO UPDATE
        SET wind_mw = EXCLUDED.wind_mw, solar_mw = EXCLUDED.solar_mw
    """)
    async with get_session() as session:
        await session.execute(stmt, rows)
        await session.commit()
    return len(rows)


async def _upsert_forecasts(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    stmt = text("""
        INSERT INTO production_forecasts
            (area, issued_at, ts, wind_mw, solar_mw, source, oldest_component_issued_at,
             components_json)
        VALUES (:area, :issued_at, :ts, :wind_mw, :solar_mw, :source, :oldest_component_issued_at,
                CAST(:components_json AS jsonb))
    """)
    grouped: dict[tuple[str, datetime], list[dict]] = {}
    for row in rows:
        grouped.setdefault((row["area"], row["ts"]), []).append(row)
    count = 0
    async with get_session() as session:
        # Serialize read/merge/append across ingestion workers. Existing snapshots
        # are immutable; late arrivals become available only when observed.
        await session.execute(text("SELECT pg_advisory_xact_lock(73640103)"))
        for (area, ts), updates in sorted(grouped.items()):
            result = await session.execute(
                text("""
                SELECT issued_at, components_json FROM production_forecasts
                WHERE area = :area AND ts = :ts AND source = :source
                  AND components_json IS NOT NULL
                ORDER BY issued_at DESC LIMIT 1
                """),
                {"area": area, "ts": ts, "source": SOURCE_FORECAST},
            )
            previous = result.mappings().first()
            components = dict(previous["components_json"]) if previous else {}
            now = datetime.now(UTC)
            for update in sorted(updates, key=lambda row: row["issued_at"]):
                for kind, component in update["components_json"].items():
                    stamp = datetime.fromisoformat(component["issued_at"])
                    before = components.get(kind)
                    if stamp <= now and (
                        before is None or stamp >= datetime.fromisoformat(before["issued_at"])
                    ):
                        components[kind] = component
            if not components or (previous and components == previous["components_json"]):
                continue
            available_at = (
                max(now, previous["issued_at"] + timedelta(microseconds=1)) if previous else now
            )
            snapshot = {
                "area": area,
                "ts": ts,
                "source": SOURCE_FORECAST,
                "issued_at": available_at,
                "oldest_component_issued_at": min(
                    (
                        datetime.fromisoformat(part["issued_at"])
                        for part in components.values()
                        if part["value"] is not None
                    ),
                    default=available_at,
                ),
                "components_json": json.dumps(components),
            }
            for kind, field in [("wind", "wind_mw"), ("solar", "solar_mw")]:
                values = [
                    part["value"]
                    for name, part in components.items()
                    if kind in name and part["value"] is not None
                ]
                snapshot[field] = sum(values) if values else None
            await session.execute(stmt, snapshot)
            count += 1
        await session.commit()
    return count


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
    count = await _upsert_forecasts(rows)
    logger.info("Production forecast ingestion complete: %d rows", count)


def _parse_day_ahead_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Historical day-ahead values published at 18:00 Copenhagen the preceding day.

    ForecastCurrent and TimestampUTC describe a different vintage and are deliberately
    ignored. Availability is reconstructed from the provider's publication schedule.
    """
    inputs = []
    zone = ZoneInfo("Europe/Copenhagen")
    for record in records:
        raw_target = record.get("HourUTC")
        if raw_target is None:
            continue
        target = datetime.fromisoformat(str(raw_target)).replace(tzinfo=UTC)
        publication_day = target.astimezone(zone).date() - timedelta(days=1)
        published = (
            datetime.combine(publication_day, datetime.min.time(), tzinfo=zone)
            .replace(hour=18)
            .astimezone(UTC)
        )
        inputs.append(
            {
                "HourUTC": raw_target,
                "PriceArea": record.get("PriceArea"),
                "ForecastType": record.get("ForecastType"),
                "ForecastCurrent": record.get("ForecastDayAhead"),
                "TimestampUTC": published.isoformat(),
            }
        )
    rows = _parse_forecast_records(inputs)
    for row in rows:
        row["source"] = SOURCE_DAY_AHEAD_HISTORY
    return rows


async def backfill_production_forecasts(days: int = 365) -> int:
    """Idempotent historical forecasts, separate from live observation-time snapshots."""
    if not 1 <= days <= 365:
        raise ValueError("Production forecast backfill must be between 1 and 365 days")
    settings = get_settings()
    end = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    total = 0
    while start < end:
        next_month = (start.replace(day=1) + timedelta(days=32)).replace(day=1)
        chunk_end = min(next_month, end)
        records = await eds_get(
            FORECASTS_DATASET,
            {
                "start": start.strftime("%Y-%m-%dT%H:%M"),
                "end": chunk_end.strftime("%Y-%m-%dT%H:%M"),
                "filter": json.dumps({"PriceArea": settings.area_codes}),
                "columns": "HourUTC,PriceArea,ForecastType,ForecastDayAhead",
                "sort": "HourUTC asc",
                "limit": "0",
            },
        )
        # Reject out-of-range/foreign records rather than importing unrelated values.
        # Empty responses must not occupy immutable historical keys: a later
        # attempt may receive the previously unavailable forecast.
        rows = [
            row
            for row in _parse_day_ahead_records(records)
            if start <= row["ts"] < chunk_end
            and row["area"] in settings.area_codes
            and (row["wind_mw"] is not None or row["solar_mw"] is not None)
        ]
        if rows:
            async with get_session() as session:
                await session.execute(
                    text("""
                    INSERT INTO production_forecasts (area, ts, issued_at, wind_mw, solar_mw,
                        source, oldest_component_issued_at, components_json)
                    VALUES (:area, :ts, :issued_at, :wind_mw, :solar_mw, :source,
                        :oldest_component_issued_at, CAST(:components_json AS jsonb))
                    ON CONFLICT (area, issued_at, ts, source) DO NOTHING
                """),
                    [
                        {**row, "components_json": json.dumps(row["components_json"])}
                        for row in rows
                    ],
                )
                await session.commit()
            total += len(rows)
        logger.info(
            "Historical day-ahead production %s→%s: %d snapshots", start, chunk_end, len(rows)
        )
        start = chunk_end
    return total


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
