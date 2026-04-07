from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from nordpool_predictor.api.schemas.prices import PricePoint, PriceResponse
from nordpool_predictor.config import get_settings
from nordpool_predictor.database import get_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/prices", tags=["prices"])


@router.get("/{area}", response_model=PriceResponse)
async def get_prices(
    area: str,
    start: datetime | None = Query(None, description="Start timestamp (ISO 8601)"),
    end: datetime | None = Query(None, description="End timestamp (ISO 8601)"),
) -> PriceResponse:
    settings = get_settings()
    if area not in settings.area_codes:
        raise HTTPException(status_code=404, detail=f"Unknown area: {area}")

    clauses = ["area = :area"]
    params: dict[str, str | datetime] = {"area": area}

    if start is not None:
        clauses.append("ts >= :start")
        params["start"] = start
    if end is not None:
        clauses.append("ts <= :end")
        params["end"] = end

    where = " AND ".join(clauses)
    query = text(
        f"SELECT ts, price_dkk_kwh FROM price_observations "  # noqa: S608
        f"WHERE {where} ORDER BY ts"
    )

    async with get_session() as session:
        result = await session.execute(query, params)
        rows = result.mappings().all()

    prices = [PricePoint.model_validate(row) for row in rows]
    return PriceResponse(area=area, prices=prices)
