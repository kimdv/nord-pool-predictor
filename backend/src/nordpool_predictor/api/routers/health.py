from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter
from sqlalchemy import text

from nordpool_predictor.api.schemas.health import HealthResponse, SourceFreshness
from nordpool_predictor.bootstrap_state import is_bootstrapping
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
    "weather": "SELECT MAX(created_at) AS last_updated FROM weather_observations",
    "production": "SELECT MAX(created_at) AS last_updated FROM production_observations",
    "crossborder": "SELECT MAX(created_at) AS last_updated FROM crossborder_observations",
}


@router.get("", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    sources: list[SourceFreshness] = []
    degraded = False
    now = datetime.now(UTC)

    try:
        async with get_session() as session:
            await session.execute(text("SELECT 1"))

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
                text("SELECT MAX(issued_at) AS last_forecast_at FROM forecast_runs")
            )
            fc_row = forecast_result.mappings().first()
            last_forecast_at = fc_row["last_forecast_at"] if fc_row else None

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
        bootstrapping=is_bootstrapping(),
    )
