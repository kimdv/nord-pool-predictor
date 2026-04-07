from __future__ import annotations

import logging
from pathlib import Path

import joblib
from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from nordpool_predictor.api.schemas.models import FeatureImportance, ModelVersionResponse
from nordpool_predictor.config import get_settings
from nordpool_predictor.database import get_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/models", tags=["models"])


@router.get("", response_model=list[ModelVersionResponse])
async def list_models() -> list[ModelVersionResponse]:
    async with get_session() as session:
        result = await session.execute(
            text(
                "SELECT model_version, model_type, area, trained_at, "
                "metrics_json, is_active "
                "FROM model_versions ORDER BY trained_at DESC"
            )
        )
        rows = result.mappings().all()

    return [
        ModelVersionResponse(
            model_version=row["model_version"],
            model_type=row["model_type"],
            area=row["area"],
            trained_at=row["trained_at"],
            metrics_json=row["metrics_json"] or {},
            is_active=row["is_active"],
        )
        for row in rows
    ]


@router.get("/{version}/feature-importance", response_model=list[FeatureImportance])
async def get_feature_importance(version: str) -> list[FeatureImportance]:
    settings = get_settings()

    async with get_session() as session:
        result = await session.execute(
            text("SELECT artifact_path FROM model_versions WHERE model_version = :version"),
            {"version": version},
        )
        row = result.mappings().first()

    if row is None:
        raise HTTPException(status_code=404, detail=f"Model version not found: {version}")

    artifact_path = Path(settings.model_dir) / row["artifact_path"]
    if not artifact_path.exists():
        raise HTTPException(status_code=404, detail="Model artifact file not found on disk")

    try:
        model = joblib.load(artifact_path)
    except Exception:
        logger.exception("Failed to load model artifact: %s", artifact_path)
        raise HTTPException(status_code=500, detail="Failed to load model artifact")

    if not hasattr(model, "feature_importances_"):
        raise HTTPException(status_code=422, detail="Model does not expose feature importances")

    importances = model.feature_importances_
    feature_names: list[str] = (
        model.feature_name_
        if hasattr(model, "feature_name_")
        else [f"feature_{i}" for i in range(len(importances))]
    )

    items = sorted(
        zip(feature_names, importances, strict=False),
        key=lambda x: x[1],
        reverse=True,
    )
    return [FeatureImportance(feature=name, importance=float(imp)) for name, imp in items]
