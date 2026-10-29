from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import text

from nordpool_predictor.api.pagination import decode_cursor, encode_cursor
from nordpool_predictor.api.schemas.models import FeatureImportance, ModelVersionResponse
from nordpool_predictor.database import get_session
from nordpool_predictor.ml.artifacts import load_feature_importance

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/models", tags=["models"])


@router.get("", response_model=list[ModelVersionResponse])
async def list_models(
    response: Response,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None, max_length=512),
) -> list[ModelVersionResponse]:
    where = ""
    params: dict = {"limit": limit + 1}
    if cursor is not None:
        timestamp, identifier = decode_cursor(cursor)
        where = "WHERE (trained_at, model_version) < (:time, :version) "
        params.update(time=timestamp, version=identifier)
    async with get_session() as session:
        result = await session.execute(
            text(
                "SELECT model_version, model_type, area, trained_at, "
                "metrics_json, is_active "
                f"FROM model_versions {where} "
                "ORDER BY trained_at DESC, model_version DESC LIMIT :limit"
            ),
            params,
        )
        rows = result.mappings().all()
    if len(rows) > limit:
        rows = rows[:limit]
        response.headers["X-Next-Cursor"] = encode_cursor(
            rows[-1]["trained_at"], rows[-1]["model_version"]
        )

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
    async with get_session() as session:
        result = await session.execute(
            text("SELECT artifact_path, area FROM model_versions WHERE model_version = :version"),
            {"version": version},
        )
        row = result.mappings().first()

    if row is None:
        raise HTTPException(status_code=404, detail=f"Model version not found: {version}")

    try:
        items = await asyncio.to_thread(
            load_feature_importance, version, row["artifact_path"], row["area"]
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=404, detail="Model artifact file not found on disk"
        ) from None
    except AttributeError:
        raise HTTPException(
            status_code=422, detail="Model does not expose feature importances"
        ) from None
    except Exception:
        logger.exception("Failed to read model artifact for %s", version)
        raise HTTPException(status_code=500, detail="Failed to load model artifact") from None
    return [FeatureImportance.model_validate(item) for item in items]
