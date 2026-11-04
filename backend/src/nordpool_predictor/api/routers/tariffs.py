from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from datetime import time as dtime

from fastapi import APIRouter, HTTPException, Query
from pydantic import AwareDatetime
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
    CPH_TZ,
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
    history_end: AwareDatetime | None = None,
) -> PriceBreakdownResponse:
    """Today's breakdown, optionally with the preceding 168 hours of actual prices."""
    _validate_gln(gln)
    _validate_code(code)
    settings = get_settings()
    if area not in settings.area_codes:
        raise HTTPException(status_code=404, detail=f"Unknown area: {area}")

    now = datetime.now(UTC)
    if history_end is not None:
        history_end = history_end.astimezone(UTC)
        current_slot = now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0)
        if history_end > current_slot or history_end.timestamp() % 900:
            raise HTTPException(
                status_code=422,
                detail="history_end must be a past or current quarter-hour boundary",
            )

    today_date = now.astimezone(CPH_TZ).date()
    today_local = datetime.combine(today_date, dtime.min, tzinfo=CPH_TZ)
    tomorrow_local = datetime.combine(today_date + timedelta(days=1), dtime.min, tzinfo=CPH_TZ)
    today_start = today_local.astimezone(UTC)
    today_end = tomorrow_local.astimezone(UTC)

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
    spot_sources: dict[str, str] = {}
    for row in rows:
        ts: datetime = row["ts"]
        spot_by_ts[ts.isoformat()] = row["price_dkk_kwh"]
        spot_sources[ts.isoformat()] = "actual"

    expected_slots = int((today_end - today_start).total_seconds() // 900)
    if len(spot_by_ts) < expected_slots:
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
                    key = fc_ts.isoformat()
                    if key not in spot_by_ts:
                        spot_by_ts[key] = fc_row["predicted_price_dkk_kwh"]
                        spot_sources[key] = "forecast"

    try:
        slots = await build_price_breakdown(
            area, gln, code, spot_by_ts, spot_sources, day=today_date
        )
    except Exception:
        logger.exception("Failed to build price breakdown")
        raise HTTPException(status_code=502, detail="Could not build price breakdown")

    historical_slots = []
    if history_end is not None:
        history_start = history_end - timedelta(hours=168)
        async with get_session() as session:
            result = await session.execute(
                text(
                    "SELECT ts, price_dkk_kwh FROM price_observations "
                    "WHERE area = :area AND ts >= :start AND ts < :end ORDER BY ts"
                ),
                {"area": area, "start": history_start, "end": history_end},
            )
            history_rows = result.mappings().all()
        historical_prices = {
            row["ts"].astimezone(UTC).isoformat(): row["price_dkk_kwh"]
            for row in history_rows
        }
        days = sorted({row["ts"].astimezone(CPH_TZ).date() for row in history_rows})
        for day in days:
            try:
                daily_slots = (
                    slots if day == today_date else await build_price_breakdown(
                        area, gln, code, historical_prices, day=day
                    )
                )
            except Exception:
                logger.exception("Could not build historical price breakdown for %s", day)
                continue
            historical_slots.extend(
                SlotBreakdown(**slot) for slot in daily_slots
                if history_start <= datetime.fromisoformat(slot["ts"]) < history_end
                and slot["spot_source"] == "actual"
            )

    return PriceBreakdownResponse(
        area=area,
        grid_company_gln=gln,
        charge_type_code=code,
        slots=[SlotBreakdown(**s) for s in slots],
        historical_slots=historical_slots,
    )
