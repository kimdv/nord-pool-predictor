from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class PricePoint(BaseModel):
    model_config = {"from_attributes": True}

    ts: datetime
    price_dkk_kwh: float


class PriceResponse(BaseModel):
    model_config = {"from_attributes": True}

    area: str
    prices: list[PricePoint]
