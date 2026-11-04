from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class SourceFreshness(BaseModel):
    model_config = {"from_attributes": True}

    source: str
    last_updated: datetime | None = None
    is_stale: bool


class HealthResponse(BaseModel):
    model_config = {"from_attributes": True}

    status: str
    sources: list[SourceFreshness]
    degraded: bool
    last_forecast_at: datetime | None = None
    bootstrapping: bool = False


class JobRunResponse(BaseModel):
    model_config = {"from_attributes": True}

    job_id: str
    job_type: str
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None


class JobSummary(BaseModel):
    """Latest request outcome and its attempts, or one historical attempt."""

    model_config = {"from_attributes": True}

    job_type: str
    last_status: str
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    batch_size: int = 0
    failures_in_batch: int = 0


class AreaResponse(BaseModel):
    model_config = {"from_attributes": True}

    code: str
    label: str
    weather_points: list[dict]
