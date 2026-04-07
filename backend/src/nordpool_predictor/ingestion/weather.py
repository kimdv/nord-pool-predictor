from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import text

from nordpool_predictor.config import WeatherPoint, get_settings
from nordpool_predictor.database import get_session

logger = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HOURLY_VARS = "temperature_2m,wind_speed_10m,cloud_cover,precipitation,shortwave_radiation"
SOURCE = "open_meteo"
MAX_RETRIES = 3
BACKOFF_BASE = 2.0


async def _request_with_retry(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = await client.get(url, params=params, timeout=60.0)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            if attempt == MAX_RETRIES:
                logger.error("Request failed after %d attempts: %s", MAX_RETRIES, exc)
                raise
            delay = BACKOFF_BASE**attempt
            logger.warning(
                "Attempt %d/%d failed (%s), retrying in %.1fs",
                attempt,
                MAX_RETRIES,
                exc,
                delay,
            )
            await asyncio.sleep(delay)
    return {}


def _collect_weather_points() -> list[tuple[str, WeatherPoint]]:
    """Return a flat list of (area_code_prefix, WeatherPoint) across all areas."""
    settings = get_settings()
    points: list[tuple[str, WeatherPoint]] = []
    for _code, area_cfg in settings.areas.items():
        for wp in area_cfg.weather_points:
            points.append((_code, wp))
    return points


def _parse_hourly(
    data: dict[str, Any],
    point_id: str,
) -> list[dict[str, Any]]:
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    winds = hourly.get("wind_speed_10m", [])
    clouds = hourly.get("cloud_cover", [])
    precips = hourly.get("precipitation", [])
    rads = hourly.get("shortwave_radiation", [])

    rows: list[dict[str, Any]] = []
    for i, t in enumerate(times):
        ts = datetime.fromisoformat(t).replace(tzinfo=UTC)
        rows.append(
            {
                "area": point_id,
                "ts": ts,
                "temperature_c": temps[i] if i < len(temps) else None,
                "wind_speed_ms": winds[i] if i < len(winds) else None,
                "cloud_cover_pct": clouds[i] if i < len(clouds) else None,
                "precipitation_mm": precips[i] if i < len(precips) else None,
                "solar_irradiance_wm2": rads[i] if i < len(rads) else None,
                "source": SOURCE,
            }
        )
    return rows


async def _upsert_forecasts(rows: list[dict[str, Any]], issued_at: datetime) -> int:
    if not rows:
        return 0
    for r in rows:
        r["issued_at"] = issued_at

    stmt = text("""
        INSERT INTO weather_forecasts
            (area, issued_at, ts, temperature_c, wind_speed_ms,
             cloud_cover_pct, precipitation_mm, solar_irradiance_wm2, source)
        VALUES
            (:area, :issued_at, :ts, :temperature_c, :wind_speed_ms,
             :cloud_cover_pct, :precipitation_mm, :solar_irradiance_wm2, :source)
        ON CONFLICT (area, issued_at, ts, source) DO NOTHING
    """)
    async with get_session() as session:
        await session.execute(stmt, rows)
        await session.commit()
    return len(rows)


async def _upsert_observations(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0

    stmt = text("""
        INSERT INTO weather_observations
            (area, ts, temperature_c, wind_speed_ms,
             cloud_cover_pct, precipitation_mm, solar_irradiance_wm2, source)
        VALUES
            (:area, :ts, :temperature_c, :wind_speed_ms,
             :cloud_cover_pct, :precipitation_mm, :solar_irradiance_wm2, :source)
        ON CONFLICT (area, ts, source) DO NOTHING
    """)
    async with get_session() as session:
        await session.execute(stmt, rows)
        await session.commit()
    return len(rows)


async def ingest_weather_forecasts() -> None:
    """Fetch the latest forecasts for every configured weather point."""
    points = _collect_weather_points()
    if not points:
        logger.warning("No weather points configured; skipping forecast ingestion")
        return

    issued_at = datetime.now(UTC).replace(second=0, microsecond=0)
    total = 0

    async with httpx.AsyncClient() as client:
        for _area_code, wp in points:
            params = {
                "latitude": str(wp.lat),
                "longitude": str(wp.lon),
                "hourly": HOURLY_VARS,
                "timezone": "UTC",
            }
            data = await _request_with_retry(client, FORECAST_URL, params)
            rows = _parse_hourly(data, wp.id)
            if not rows:
                logger.warning("No forecast data returned for %s", wp.id)
                continue
            count = await _upsert_forecasts(rows, issued_at)
            total += count
            logger.info("Ingested %d forecast rows for %s", count, wp.id)

    logger.info("Weather forecast ingestion complete: %d total rows", total)


async def backfill_weather(days: int = 365) -> None:
    """Fetch historical weather observations for every configured weather point."""
    points = _collect_weather_points()
    if not points:
        logger.warning("No weather points configured; skipping weather backfill")
        return

    now = datetime.now(UTC)
    total = 0

    async with httpx.AsyncClient() as client:
        for _area_code, wp in points:
            chunk_start = now - timedelta(days=days)
            while chunk_start < now:
                chunk_end = min(chunk_start + timedelta(days=30), now)
                params = {
                    "latitude": str(wp.lat),
                    "longitude": str(wp.lon),
                    "start_date": chunk_start.strftime("%Y-%m-%d"),
                    "end_date": chunk_end.strftime("%Y-%m-%d"),
                    "hourly": HOURLY_VARS,
                    "timezone": "UTC",
                }
                data = await _request_with_retry(client, ARCHIVE_URL, params)
                rows = _parse_hourly(data, wp.id)
                if not rows:
                    logger.warning(
                        "No archive data for %s (%s→%s)",
                        wp.id,
                        chunk_start.date(),
                        chunk_end.date(),
                    )
                    chunk_start = chunk_end
                    continue
                count = await _upsert_observations(rows)
                total += count
                logger.info(
                    "Backfill %s %s→%s: %d rows",
                    wp.id,
                    chunk_start.date(),
                    chunk_end.date(),
                    count,
                )
                chunk_start = chunk_end

    logger.info("Weather backfill complete: %d total rows", total)
