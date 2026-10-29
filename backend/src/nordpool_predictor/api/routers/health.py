from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Response
from sqlalchemy import text

from nordpool_predictor.api.schemas.health import HealthResponse, SourceFreshness
from nordpool_predictor.config import get_settings
from nordpool_predictor.database import get_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/health", tags=["health"])

_STALENESS_THRESHOLDS: dict[str, timedelta] = {
    "prices": timedelta(hours=26),
    "weather": timedelta(hours=26),
    "production": timedelta(hours=26),
    "crossborder": timedelta(hours=26),
}

_SOURCE_QUERIES: dict[str, str] = {
    "prices": "SELECT MAX(created_at) AS last_updated FROM price_observations",
    "weather": "SELECT MAX(created_at) AS last_updated FROM weather_forecasts",
    "production": "SELECT MAX(created_at) AS last_updated FROM production_observations",
    "crossborder": "SELECT MAX(created_at) AS last_updated FROM crossborder_observations",
}


@router.get("", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    sources: list[SourceFreshness] = []
    degraded = False
    now = datetime.now(UTC)
    bootstrapping = False

    try:
        async with get_session() as session:
            bootstrap_result = await session.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM job_requests WHERE job_name = 'bootstrap' "
                    "AND status IN ('pending', 'running'))"
                )
            )
            bootstrapping = bool(bootstrap_result.scalar())

            for source_name, query in _SOURCE_QUERIES.items():
                result = await session.execute(text(query))
                row = result.mappings().first()
                last_updated = row["last_updated"] if row else None

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
                    SELECT 1 FROM forecast_values fv WHERE fv.run_id = fr.run_id AND fv.ts > NOW()
                ) AS has_future
                FROM unnest(CAST(:areas AS text[])) AS a(area)
                LEFT JOIN LATERAL (
                    SELECT run_id, issued_at FROM forecast_runs
                    WHERE area = a.area AND status IN ('completed', 'scored')
                    ORDER BY issued_at DESC LIMIT 1
                ) fr ON TRUE
            """),
                {"areas": get_settings().area_codes},
            )
            forecasts = forecast_result.mappings().all()
            issued = [row["issued_at"] for row in forecasts if row["issued_at"] is not None]
            last_forecast_at = min(issued) if issued else None
            if not forecasts or any(
                row["issued_at"] is None
                or not row["has_future"]
                or now - row["issued_at"] > timedelta(hours=26)
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
