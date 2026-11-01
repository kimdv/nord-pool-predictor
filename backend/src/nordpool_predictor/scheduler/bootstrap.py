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
    failures: list[str] = []

    async def attempt(label, operation, *args, **kwargs) -> bool:
        try:
            await operation(*args, **kwargs)
        except Exception:
            logger.exception("Bootstrap preparation or recovery failed: %s", label)
            failures.append(label)
            return False
        return True

    shared_history_ready = True
    price_history_ready = False
    if needs_training:
        # These sources fetch both areas together. A failure affects new training,
        # but must not block forecast recovery for already usable models.
        for label, operation in (
            ("price backfill", backfill_prices),
            ("production backfill", backfill_production),
            ("crossborder backfill", backfill_crossborder),
        ):
            succeeded = await attempt(label, operation, days=settings.training_window_days)
            shared_history_ready = shared_history_ready and succeeded
            if operation is backfill_prices:
                price_history_ready = succeeded

    needs_forecast = set(needs_training)
    for area, version in models.items():
        if area in settings.area_codes and not await forecast_ready(area, version):
            needs_forecast.add(area)

    usable_areas = set(models)
    for area in needs_training:
        history_ready = await attempt(
            f"{area} weather backfill",
            backfill_weather,
            days=settings.training_window_days,
            area=area,
        )
        if shared_history_ready and history_ready:
            if await attempt(f"{area} training", train_models, area):
                usable_areas.add(area)

    forecast_areas = [
        area for area in settings.area_codes if area in needs_forecast and area in usable_areas
    ]
    if forecast_areas:
        # Reused models also need the 192-hour price window consumed by prediction.
        # Retry a failed full-window price backfill with this smaller recovery window.
        if not price_history_ready:
            price_history_ready = await attempt("recent price backfill", backfill_prices, days=8)
        # Training is independent of live ingestion. Weather is fetched per area
        # so an upstream failure for one area cannot prevent another's recovery.
        production_ready = await attempt("production forecasts", ingest_production_forecasts)
        for area in forecast_areas:
            weather_ready = await attempt(
                f"{area} weather forecasts", ingest_weather_forecasts, area=area
            )
            if price_history_ready and weather_ready and production_ready:
                await attempt(f"{area} forecast", run_forecast, area)

    if failures:
        raise RuntimeError(f"Bootstrap incomplete: {', '.join(failures)}")
    logger.info("All configured areas have usable models and forecasts")
