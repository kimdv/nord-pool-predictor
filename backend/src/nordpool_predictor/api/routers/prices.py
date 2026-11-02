from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query
from pydantic import AwareDatetime
from sqlalchemy import text

from nordpool_predictor.api.schemas.prices import PricePoint, PriceResponse
from nordpool_predictor.config import get_settings
from nordpool_predictor.database import get_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/prices", tags=["prices"])


@router.get("/{area}", response_model=PriceResponse)
async def get_prices(
    area: str,
    start: AwareDatetime | None = Query(None, description="Start timestamp (ISO 8601)"),
    end: AwareDatetime | None = Query(None, description="End timestamp (ISO 8601)"),
    limit: int = Query(1000, ge=1, le=5000),
    after: AwareDatetime | None = Query(None, description="Exclusive timestamp cursor"),
) -> PriceResponse:
    settings = get_settings()
    if area not in settings.area_codes:
        raise HTTPException(status_code=404, detail=f"Unknown area: {area}")

    end = end or datetime.now(UTC) + timedelta(days=2)
    start = start or end - timedelta(days=7)
    if start > end or end - start > timedelta(days=366):
        raise HTTPException(
            status_code=422, detail="Expected an ordered range of at most 366 days"
        )
    clauses = ["area = :area"]
    params: dict[str, object] = {"area": area}

    if start is not None:
        clauses.append("ts >= :start")
        params["start"] = start
    if end is not None:
        clauses.append("ts <= :end")
        params["end"] = end

    if after is not None:
        clauses.append("ts > :after")
        params["after"] = after
    params["limit"] = limit + 1
    where = " AND ".join(clauses)
    query = text(
        f"SELECT ts, price_dkk_kwh FROM price_observations "  # noqa: S608
        f"WHERE {where} ORDER BY ts LIMIT :limit"
    )

    async with get_session() as session:
        result = await session.execute(query, params)
        rows = result.mappings().all()

    next_cursor = rows[limit - 1]["ts"] if len(rows) > limit else None
    prices = [PricePoint.model_validate(row) for row in rows[:limit]]
    return PriceResponse(area=area, prices=prices, next_cursor=next_cursor)
