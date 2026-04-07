from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from nordpool_predictor.api.schemas.tariffs import (
    GridCompany,
    PriceBreakdownResponse,
    SlotBreakdown,
    TariffCode,
)
from nordpool_predictor.config import get_settings
from nordpool_predictor.database import get_session
from nordpool_predictor.tariffs import (
    build_price_breakdown,
    fetch_grid_companies,
    fetch_grid_tariff_codes,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/tariffs", tags=["tariffs"])

_GLN_RE = re.compile(r"^\d{13}$")
_CODE_RE = re.compile(r"^[A-Za-z0-9_<>. -]{1,50}$")


def _validate_gln(gln: str) -> None:
    if not _GLN_RE.match(gln):
        raise HTTPException(status_code=400, detail="GLN must be exactly 13 digits")


def _validate_code(code: str) -> None:
    if not _CODE_RE.match(code):
        raise HTTPException(status_code=400, detail="Invalid tariff code format")


@router.get("/grid-companies", response_model=list[GridCompany])
async def list_grid_companies() -> list[GridCompany]:
    """List all Danish grid companies that have tariff data."""
    try:
        companies = await fetch_grid_companies()
        return [GridCompany(**c) for c in companies]
    except Exception:
        logger.exception("Failed to fetch grid companies")
        raise HTTPException(status_code=502, detail="Could not fetch grid company list")


@router.get("/grid-companies/{gln}/codes", response_model=list[TariffCode])
async def list_tariff_codes(gln: str) -> list[TariffCode]:
    """List available tariff codes for a grid company."""
    _validate_gln(gln)
    try:
        codes = await fetch_grid_tariff_codes(gln)
        return [TariffCode(**c) for c in codes]
    except Exception:
        logger.exception("Failed to fetch tariff codes for GLN %s", gln)
        raise HTTPException(status_code=502, detail="Could not fetch tariff codes")


@router.get("/breakdown/{area}", response_model=PriceBreakdownResponse)
async def price_breakdown(
    area: str,
    gln: str = Query(..., description="Grid company GLN number"),
    code: str = Query(..., description="Tariff ChargeTypeCode (e.g. CD)"),
) -> PriceBreakdownResponse:
    """Hourly price breakdown for today, combining spot price with all tariffs."""
    _validate_gln(gln)
    _validate_code(code)
    settings = get_settings()
    if area not in settings.area_codes:
        raise HTTPException(status_code=404, detail=f"Unknown area: {area}")

    now = datetime.now(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)

    async with get_session() as session:
        result = await session.execute(
            text(
                "SELECT ts, price_dkk_kwh FROM price_observations "
                "WHERE area = :area AND ts >= :start AND ts < :end ORDER BY ts"
            ),
            {"area": area, "start": today_start, "end": today_end},
        )
        rows = result.mappings().all()

    spot_by_ts: dict[str, float] = {}
    for row in rows:
        ts: datetime = row["ts"]
        spot_by_ts[ts.isoformat()] = row["price_dkk_kwh"]

    if len(spot_by_ts) < 96:
        async with get_session() as session:
            run_result = await session.execute(
                text("SELECT run_id FROM latest_forecast_runs WHERE area = :area"),
                {"area": area},
            )
            run = run_result.mappings().first()
            if run:
                fc_result = await session.execute(
                    text(
                        "SELECT ts, predicted_price_dkk_kwh FROM forecast_values "
                        "WHERE run_id = :run_id AND ts >= :start AND ts < :end ORDER BY ts"
                    ),
                    {
                        "run_id": run["run_id"],
                        "start": today_start,
                        "end": today_end,
                    },
                )
                for fc_row in fc_result.mappings().all():
                    fc_ts: datetime = fc_row["ts"]
                    spot_by_ts.setdefault(fc_ts.isoformat(), fc_row["predicted_price_dkk_kwh"])

    try:
        slots = await build_price_breakdown(area, gln, code, spot_by_ts)
    except Exception:
        logger.exception("Failed to build price breakdown")
        raise HTTPException(status_code=502, detail="Could not build price breakdown")

    return PriceBreakdownResponse(
        area=area,
        grid_company_gln=gln,
        charge_type_code=code,
        slots=[SlotBreakdown(**s) for s in slots],
    )
