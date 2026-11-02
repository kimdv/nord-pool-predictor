from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Response
from sqlalchemy import text

from nordpool_predictor.api.schemas.health import HealthResponse, SourceFreshness
from nordpool_predictor.config import get_settings
from nordpool_predictor.database import get_session
from nordpool_predictor.freshness import FORECAST_MAX_AGE
from nordpool_predictor.ingestion.crossborder import EXCHANGE_COLUMNS

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/health", tags=["health"])

_STALENESS_THRESHOLDS: dict[str, timedelta] = {
    "prices": timedelta(hours=26),
    "weather": timedelta(hours=26),
    "production": timedelta(hours=26),
    "crossborder": timedelta(hours=26),
}

_SOURCE_QUERIES: dict[str, str] = {
    "prices": """
        SELECT (SELECT MAX(ts) FROM price_observations
                WHERE area = owner AND ts <= :now) AS last_updated
        FROM unnest(CAST(:owners AS text[])) AS expected(owner)
    """,
    "weather": """
        SELECT (SELECT CASE WHEN temperature_c IS NOT NULL OR wind_speed_ms IS NOT NULL
                    OR cloud_cover_pct IS NOT NULL OR precipitation_mm IS NOT NULL
                    OR solar_irradiance_wm2 IS NOT NULL THEN issued_at END
                FROM weather_forecasts
                WHERE area = owner AND ts = :target_hour AND issued_at <= :now
                ORDER BY issued_at DESC LIMIT 1) AS last_updated
        FROM unnest(CAST(:owners AS text[])) AS expected(owner)
    """,
    "production": """
        SELECT (SELECT MAX(ts) FROM production_observations
                WHERE area = owner AND ts <= :now
                  AND (wind_mw IS NOT NULL OR solar_mw IS NOT NULL)) AS last_updated
        FROM unnest(CAST(:owners AS text[])) AS expected(owner)
        UNION ALL
        SELECT (SELECT CASE WHEN wind_mw IS NOT NULL OR solar_mw IS NOT NULL
                    THEN issued_at END FROM production_forecasts
                WHERE area = owner AND ts = :target_hour AND issued_at <= :now
                ORDER BY issued_at DESC LIMIT 1) AS last_updated
        FROM unnest(CAST(:owners AS text[])) AS expected(owner)
    """,
    "crossborder": """
        SELECT (SELECT MAX(ts) FROM crossborder_observations
                WHERE connection = owner AND ts <= :now AND flow_mw IS NOT NULL) AS last_updated
        FROM unnest(CAST(:owners AS text[])) AS expected(owner)
    """,
}


@router.get("", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    sources: list[SourceFreshness] = []
    degraded = False
    now = datetime.now(UTC)
    # Prediction selects hourly snapshots for its first quarter-hour target.
    next_target = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    target_hour = (next_target + timedelta(minutes=15)).replace(minute=0)
    bootstrapping = False

    try:
        settings = get_settings()
        owners = {
            "prices": settings.area_codes,
            "weather": [
                point.id for area in settings.areas.values() for point in area.weather_points
            ],
            "production": settings.area_codes,
            "crossborder": sorted(
                {
                    connection
                    for mapping in EXCHANGE_COLUMNS.values()
                    for connection in mapping.values()
                    if any(area in connection.split("-") for area in settings.area_codes)
                }
            ),
        }
        async with get_session() as session:
            bootstrap_result = await session.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM job_requests WHERE job_name = 'bootstrap' "
                    "AND status IN ('pending', 'running'))"
                )
            )
            bootstrapping = bool(bootstrap_result.scalar())

            for source_name, query in _SOURCE_QUERIES.items():
                if not owners[source_name]:
                    continue  # No applicable inputs configured for this source.
                result = await session.execute(
                    text(query),
                    {"owners": owners[source_name], "now": now, "target_hour": target_hour},
                )
                timestamps = [row["last_updated"] for row in result.mappings().all()]
                last_updated = (
                    min(timestamps)
                    if timestamps and all(ts is not None for ts in timestamps)
                    else None
                )

                threshold = _STALENESS_THRESHOLDS[source_name]
                is_stale = last_updated is None or (now - last_updated) > threshold
                if is_stale:
                    degraded = True

                sources.append(
                    SourceFreshness(
                        source=source_name,
                        last_updated=last_updated,
                        is_stale=is_stale,
                    )
                )

            forecast_result = await session.execute(
                text("""
                SELECT a.area, fr.issued_at, EXISTS (
                    SELECT 1 FROM forecast_values fv WHERE fv.run_id = fr.run_id AND fv.ts > :now
                ) AS has_future
                FROM unnest(CAST(:areas AS text[])) AS a(area)
                LEFT JOIN LATERAL (
                    SELECT run_id, issued_at FROM forecast_runs
                    WHERE area = a.area AND status IN ('completed', 'scored')
                    ORDER BY issued_at DESC LIMIT 1
                ) fr ON TRUE
            """),
                {"areas": settings.area_codes, "now": now},
            )
            forecasts = forecast_result.mappings().all()
            issued = [row["issued_at"] for row in forecasts if row["issued_at"] is not None]
            last_forecast_at = min(issued) if issued else None
            if not forecasts or any(
                row["issued_at"] is None
                or not row["has_future"]
                or not timedelta(0) <= now - row["issued_at"] <= FORECAST_MAX_AGE
                for row in forecasts
            ):
                degraded = True

        status = "degraded" if degraded else "ok"

    except Exception:
        logger.exception("Health check failed")
        status = "error"
        degraded = True
        last_forecast_at = None

    return HealthResponse(
        status=status,
        sources=sources,
        degraded=degraded,
        last_forecast_at=last_forecast_at,
        bootstrapping=bootstrapping,
    )


@router.get("/live")
async def liveness() -> dict[str, str]:
    """Process responsiveness only; no database or data freshness queries."""
    return {"status": "ok"}


@router.get("/ready", response_model=HealthResponse)
async def readiness(response: Response) -> HealthResponse:
    health = await health_check()
    if health.status != "ok" or health.bootstrapping:
        response.status_code = 503
    return health
