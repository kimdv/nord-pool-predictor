from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class ModelVersionResponse(BaseModel):
    model_config = {"from_attributes": True}

    model_version: str
    model_type: str
    area: str
    trained_at: datetime
    metrics_json: dict[str, Any]
    is_active: bool


class FeatureImportance(BaseModel):
    model_config = {"from_attributes": True}

    feature: str
    importance: float
