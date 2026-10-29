from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text

from nordpool_predictor.config import get_settings
from nordpool_predictor.database import get_session
from nordpool_predictor.ingestion.crossborder import backfill_crossborder
from nordpool_predictor.ingestion.prices import backfill_prices
from nordpool_predictor.ingestion.production import (
    backfill_production,
    ingest_production_forecasts,
)
from nordpool_predictor.ingestion.weather import backfill_weather, ingest_weather_forecasts
from nordpool_predictor.ml.predict import _load_models, run_forecast
from nordpool_predictor.ml.train import train_models

logger = logging.getLogger(__name__)


async def ready_models() -> dict[str, str]:
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    text("""
            SELECT area, model_version, artifact_path FROM model_versions
            WHERE is_active = TRUE AND feature_set_version = 'v2-asof'
            ORDER BY trained_at DESC
        """)
                )
            )
            .mappings()
            .all()
        )
    ready: dict[str, str] = {}
    for row in rows:
        if row["area"] in ready:
            continue
        try:
            await asyncio.to_thread(
                _load_models, row["model_version"], row["artifact_path"], row["area"]
            )
        except Exception:
            logger.exception("Model artifacts unavailable for %s", row["area"])
        else:
            ready[row["area"]] = row["model_version"]
    return ready


async def forecast_ready(area: str, model_version: str) -> bool:
    async with get_session() as session:
        return bool(
            (
                await session.execute(
                    text("""
            SELECT EXISTS (
                SELECT 1 FROM forecast_runs fr JOIN forecast_values fv USING (run_id)
                WHERE fr.area = :area AND fr.model_version = :version
                  AND fr.status IN ('completed', 'scored') AND fv.ts > NOW()
            )
        """),
                    {"area": area, "version": model_version},
                )
            ).scalar()
        )


async def bootstrap() -> None:
    """Reconcile each configured area's usable model and current forecast."""
    settings = get_settings()
    models = await ready_models()
    needs_training = [area for area in settings.area_codes if area not in models]
    if needs_training:
        # Backfills are idempotent. Never infer complete history from another area's newest row.
        for operation in (
            backfill_prices,
            backfill_weather,
            backfill_production,
            backfill_crossborder,
        ):
            await operation(days=settings.training_window_days)

    needs_forecast = set(needs_training)
    for area, version in models.items():
        if area in settings.area_codes and not await forecast_ready(area, version):
            needs_forecast.add(area)
    if needs_forecast:
        await ingest_weather_forecasts()
        await ingest_production_forecasts()

    failures: list[str] = []
    for area in settings.area_codes:
        try:
            if area in needs_training:
                await train_models(area)
            if area in needs_forecast:
                await run_forecast(area)
        except Exception:
            logger.exception("Bootstrap incomplete for %s", area)
            failures.append(area)
    if failures:
        raise RuntimeError(f"Bootstrap incomplete for areas: {', '.join(failures)}")
    logger.info("All configured areas have usable models and forecasts")
