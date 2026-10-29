from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class GridCompany(BaseModel):
    gln: str
    name: str


class TariffCode(BaseModel):
    code: str
    note: str
    description: str


class SlotBreakdown(BaseModel):
    hour: int
    minute: int
    ts: str
    spot_price: float | None
    spot_source: Literal["actual", "forecast", "missing"]
    grid_tariff: float
    system_tariff: float
    transmission_tariff: float
    grid_loss_tariff: float
    electricity_tax: float
    total_ex_vat: float | None
    vat: float | None
    total_incl_vat: float | None


class PriceBreakdownResponse(BaseModel):
    area: str
    grid_company_gln: str
    charge_type_code: str
    slots: list[SlotBreakdown]
