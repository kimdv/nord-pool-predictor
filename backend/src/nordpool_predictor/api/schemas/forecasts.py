from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ForecastPoint(BaseModel):
    model_config = {"from_attributes": True}

    ts: datetime
    predicted_price_dkk_kwh: float
    lower_dkk_kwh: float | None = None
    upper_dkk_kwh: float | None = None


class ForecastRunResponse(BaseModel):
    model_config = {"from_attributes": True}

    run_id: str
    area: str
    model_version: str
    issued_at: datetime
    status: str
    notes: str | None = None
    values: list[ForecastPoint]
    production_forecast_horizon: datetime | None = None


class AccuracyMetrics(BaseModel):
    model_config = {"from_attributes": True}

    run_id: str
    issued_at: datetime
    mae_24h: float | None = None
    rmse_24h: float | None = None
    bias_24h: float | None = None
    quality_label: str | None = None


class BenchmarkEntry(BaseModel):
    model_config = {"from_attributes": True}

    baseline_name: str
    mae: float | None = None
    rmse: float | None = None
    bias: float | None = None


class BenchmarkResponse(BaseModel):
    model_config = {"from_attributes": True}

    run_id: str
    area: str
    ml_mae: float | None = None
    ml_rmse: float | None = None
    baselines: list[BenchmarkEntry]


class SnapshotSummary(BaseModel):
    model_config = {"from_attributes": True}

    run_id: str
    issued_at: datetime
    steps: int
