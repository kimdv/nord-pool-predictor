from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from nordpool_predictor.config import get_settings
from nordpool_predictor.database import get_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ha", tags=["homeassistant"])


@router.get("/{area}")
async def ha_sensor(area: str) -> dict[str, Any]:
    """Flat JSON payload designed for Home Assistant REST sensor integration."""
    settings = get_settings()
    if area not in settings.area_codes:
        raise HTTPException(status_code=404, detail=f"Unknown area: {area}")

    now = datetime.now(UTC)
    current_slot = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    next_slot = current_slot + timedelta(minutes=15)
    today_start = current_slot.replace(hour=0, minute=0)
    today_end = today_start + timedelta(days=1)

    async with get_session() as session:
        run_result = await session.execute(
            text("SELECT run_id, model_version FROM latest_forecast_runs WHERE area = :area"),
            {"area": area},
        )
        run = run_result.mappings().first()
        if run is None:
            raise HTTPException(status_code=404, detail=f"No forecast available for area {area}")

        run_id = run["run_id"]
        model_version = run["model_version"]

        actual_result = await session.execute(
            text(
                "SELECT ts, price_dkk_kwh FROM price_observations "
                "WHERE area = :area AND ts IN (:current_slot, :next_slot) "
                "ORDER BY ts"
            ),
            {"area": area, "current_slot": current_slot, "next_slot": next_slot},
        )
        actuals = {row["ts"]: row["price_dkk_kwh"] for row in actual_result.mappings().all()}

        fc_current_result = await session.execute(
            text(
                "SELECT ts, predicted_price_dkk_kwh FROM forecast_values "
                "WHERE run_id = :run_id AND ts IN (:current_slot, :next_slot) "
                "ORDER BY ts"
            ),
            {"run_id": run_id, "current_slot": current_slot, "next_slot": next_slot},
        )
        fc_prices = {
            row["ts"]: row["predicted_price_dkk_kwh"] for row in fc_current_result.mappings().all()
        }

        current_price = actuals.get(current_slot) or fc_prices.get(current_slot)
        next_slot_price = actuals.get(next_slot) or fc_prices.get(next_slot)

        today_result = await session.execute(
            text(
                "SELECT ts, price_dkk_kwh FROM price_observations "
                "WHERE area = :area AND ts >= :today_start AND ts < :today_end "
                "ORDER BY ts"
            ),
            {"area": area, "today_start": today_start, "today_end": today_end},
        )
        today_prices_map: dict[datetime, float] = {
            row["ts"]: row["price_dkk_kwh"] for row in today_result.mappings().all()
        }

        fc_today_result = await session.execute(
            text(
                "SELECT ts, predicted_price_dkk_kwh FROM forecast_values "
                "WHERE run_id = :run_id AND ts >= :today_start AND ts < :today_end "
                "ORDER BY ts"
            ),
            {"run_id": run_id, "today_start": today_start, "today_end": today_end},
        )
        for row in fc_today_result.mappings().all():
            today_prices_map.setdefault(row["ts"], row["predicted_price_dkk_kwh"])

        today_values = list(today_prices_map.values())
        today_min = min(today_values) if today_values else None
        today_max = max(today_values) if today_values else None
        today_average = round(sum(today_values) / len(today_values), 4) if today_values else None

        forecast_result = await session.execute(
            text(
                "SELECT ts, predicted_price_dkk_kwh FROM forecast_values "
                "WHERE run_id = :run_id AND ts >= :now ORDER BY ts"
            ),
            {"run_id": run_id, "now": current_slot},
        )
        forecast_list = [
            {"start": row["ts"].isoformat(), "price": row["predicted_price_dkk_kwh"]}
            for row in forecast_result.mappings().all()
        ]

        quality_result = await session.execute(
            text("SELECT quality_label, mae_24h FROM forecast_run_metrics WHERE run_id = :run_id"),
            {"run_id": run_id},
        )
        quality_row = quality_result.mappings().first()

    forecast_quality = quality_row["quality_label"] if quality_row else None
    mae_24h = quality_row["mae_24h"] if quality_row else None

    state = current_price if current_price is not None else 0.0

    return {
        "state": state,
        "attributes": {
            "unit_of_measurement": "DKK/kWh",
            "area": area,
            "current_price": current_price,
            "next_slot_price": next_slot_price,
            "today_min": today_min,
            "today_max": today_max,
            "today_average": today_average,
            "forecast": forecast_list,
            "model_version": model_version,
            "forecast_quality": forecast_quality,
            "mae_24h": mae_24h,
        },
    }
