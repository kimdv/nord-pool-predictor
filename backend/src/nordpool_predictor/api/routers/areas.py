from __future__ import annotations

import logging

from fastapi import APIRouter

from nordpool_predictor.api.schemas.health import AreaResponse
from nordpool_predictor.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/areas", tags=["areas"])


@router.get("", response_model=list[AreaResponse])
async def list_areas() -> list[AreaResponse]:
    settings = get_settings()
    return [
        AreaResponse(
            code=cfg.code,
            label=cfg.label,
            weather_points=[
                {"id": wp.id, "name": wp.name, "lat": wp.lat, "lon": wp.lon}
                for wp in cfg.weather_points
            ],
        )
        for cfg in settings.areas.values()
    ]
