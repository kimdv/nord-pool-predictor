from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

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
from nordpool_predictor.ml.predict import PRICE_HISTORY_DAYS, _load_models, run_forecast
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


async def verify_price_history(area: str) -> None:
    """Require the persisted quarter-hour grid consumed by prediction for this area."""
    now = datetime.now(UTC)
    end = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    if end < now:
        end += timedelta(minutes=15)
    async with get_session() as session:
        missing = (
            await session.execute(
                text("""
                    SELECT COUNT(*) FROM generate_series(
                        CAST(:start AS timestamptz),
                        CAST(:end AS timestamptz) - INTERVAL '15 minutes',
                        INTERVAL '15 minutes'
                    ) AS expected(ts)
                    LEFT JOIN price_observations p ON p.area = :area AND p.ts = expected.ts
                    WHERE p.price_dkk_kwh IS NULL
                """),
                {"area": area, "start": end - timedelta(days=PRICE_HISTORY_DAYS), "end": end},
            )
        ).scalar_one()
    if missing:
        raise RuntimeError(f"Incomplete recent price history for {area}: {missing} missing slots")


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
    price_window_fetched_until: datetime | None = None
    if needs_training:
        # These sources fetch both areas together. A failure affects new training,
        # but must not block forecast recovery for already usable models.
        for label, operation in (
            ("price backfill", backfill_prices),
            ("production backfill", backfill_production),
            ("crossborder backfill", backfill_crossborder),
        ):
            started_at = datetime.now(UTC)
            succeeded = await attempt(label, operation, days=settings.training_window_days)
            shared_history_ready = shared_history_ready and succeeded
            if (
                operation is backfill_prices
                and succeeded
                and settings.training_window_days >= PRICE_HISTORY_DAYS
            ):
                # backfill_prices ends at the midnight after its request date.
                # That range can become stale while another area is training.
                price_window_fetched_until = started_at.replace(
                    hour=0, minute=0, second=0, microsecond=0
                ) + timedelta(days=1)

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

    # Training/backfills can take hours. Check reused forecasts after that work
    # so a forecast that expired during training is included in recovery.
    needs_forecast = set(needs_training)
    for area, version in models.items():
        if area in settings.area_codes and not await forecast_ready(area, version):
            needs_forecast.add(area)

    forecast_areas = [
        area for area in settings.area_codes if area in needs_forecast and area in usable_areas
    ]
    if forecast_areas:
        # Reused models also need the 192-hour price window consumed by prediction.
        # Retry a failed full-window price backfill with this smaller recovery window.
        if price_window_fetched_until is None or datetime.now(UTC) >= price_window_fetched_until:
            await attempt("recent price backfill", backfill_prices, days=PRICE_HISTORY_DAYS)
        # Training is independent of live ingestion. Weather is fetched per area
        # so an upstream failure for one area cannot prevent another's recovery.
        production_ready = await attempt("production forecasts", ingest_production_forecasts)
        for area in forecast_areas:
            # A successful request can still return no rows or omit an area.
            # Cached usable history can also survive an upstream fetch failure.
            if not await attempt(f"{area} price history", verify_price_history, area):
                continue
            weather_ready = await attempt(
                f"{area} weather forecasts", ingest_weather_forecasts, area=area
            )
            if weather_ready and production_ready:
                await attempt(f"{area} forecast", run_forecast, area)

    if failures:
        raise RuntimeError(f"Bootstrap incomplete: {', '.join(failures)}")
    logger.info("All configured areas have usable models and forecasts")
