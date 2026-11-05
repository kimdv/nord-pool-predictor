from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from nordpool_predictor.config import get_settings
from nordpool_predictor.database import get_session
from nordpool_predictor.freshness import FORECAST_MAX_AGE
from nordpool_predictor.ingestion.crossborder import backfill_crossborder, ingest_crossborder_flows
from nordpool_predictor.ingestion.prices import backfill_prices
from nordpool_predictor.ingestion.production import (
    backfill_production,
    ingest_production_actuals,
    ingest_production_forecasts,
)
from nordpool_predictor.ingestion.weather import ingest_weather_forecasts
from nordpool_predictor.ml.predict import (
    PRICE_HISTORY_DAYS,
    ModelUnavailableError,
    run_forecast,
    select_usable_model,
)
from nordpool_predictor.ml.train import ProductionHistoryBackfill, train_models

logger = logging.getLogger(__name__)


async def ready_models() -> dict[str, str]:
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    text("""
            SELECT area, model_version, artifact_path FROM model_versions
            WHERE is_active = TRUE AND feature_set_version = 'v4-crossborder-history'
            ORDER BY trained_at DESC, model_version DESC
        """)
                )
            )
            .mappings()
            .all()
        )
    candidates: dict[str, list] = {}
    for row in rows:
        candidates.setdefault(row["area"], []).append(row)
    ready: dict[str, str] = {}
    for area, area_candidates in candidates.items():
        try:
            version, _models = await select_usable_model(area, area_candidates)
        except ModelUnavailableError:
            logger.exception("Model artifacts unavailable for %s", area)
        else:
            ready[area] = version
    return ready


async def forecast_ready(area: str, model_version: str, *, as_of: datetime | None = None) -> bool:
    now = as_of if as_of is not None else datetime.now(UTC)
    async with get_session() as session:
        return bool(
            (
                await session.execute(
                    text("""
            SELECT EXISTS (
                SELECT 1 FROM (
                    SELECT run_id, issued_at FROM forecast_runs
                    WHERE area = :area AND model_version = :version
                      AND status IN ('completed', 'scored')
                    ORDER BY issued_at DESC LIMIT 1
                ) fr
                WHERE fr.issued_at BETWEEN :fresh_since AND :as_of
                  AND EXISTS (
                    SELECT 1 FROM forecast_values fv
                    WHERE fv.run_id = fr.run_id AND fv.ts > :as_of
                  )
            )
        """),
                    {
                        "area": area,
                        "version": model_version,
                        "as_of": now,
                        "fresh_since": now - FORECAST_MAX_AGE,
                    },
                )
            ).scalar()
        )


async def verify_price_history(area: str, *, issued_at: datetime | None = None) -> None:
    """Require the persisted quarter-hour grid consumed by prediction for this area."""
    now = issued_at if issued_at is not None else datetime.now(UTC)
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
    models = dict(await ready_models())
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
    price_history_ready = True
    price_window_fetched_until: datetime | None = None
    production_backfill = ProductionHistoryBackfill()
    if needs_training:
        # Only price history is required for training; optional feed failures
        # still trigger live-source recovery and remain visible in the outcome.
        for label, operation in (
            ("price backfill", backfill_prices),
            ("production backfill", backfill_production),
            ("crossborder backfill", backfill_crossborder),
        ):
            started_at = datetime.now(UTC)
            succeeded = await attempt(label, operation, days=settings.training_window_days)
            shared_history_ready = shared_history_ready and succeeded
            if operation is backfill_prices:
                price_history_ready = succeeded
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

    async def train_area(area: str) -> None:
        models[area] = await train_models(area, production_backfill=production_backfill)

    if price_history_ready:
        for area in needs_training:
            await attempt(f"{area} training", train_area, area)

    # Existing models skip historical backfills. Refresh observation feeds too,
    # so a restart after downtime can restore source readiness without retraining.
    if not needs_training or not shared_history_ready:
        await attempt("production actuals", ingest_production_actuals)
        await attempt("crossborder flows", ingest_crossborder_flows)

    attempted_areas: set[str] = set()
    refreshed_areas: set[str] = set()
    failed_areas: set[str] = set()
    while True:
        # Reconcile again after recovery: previously-ready forecasts can expire
        # while another area is fetching data or generating its forecast.
        checked_at = datetime.now(UTC)
        forecast_areas = []
        source_areas = []
        for area in settings.area_codes:
            if area in failed_areas:
                continue
            ready = False
            if area in models:
                try:
                    ready = await forecast_ready(area, models[area], as_of=checked_at)
                except Exception:
                    logger.exception("Bootstrap forecast readiness check failed for %s", area)
                    failures.append(f"{area} forecast readiness")
                    failed_areas.add(area)
                    continue
            if area not in models or ready:
                if area not in refreshed_areas:
                    source_areas.append(area)
                continue
            if area in attempted_areas:
                # Each area gets one recovery attempt. Never spin indefinitely
                # or report success if a generated forecast is already expired.
                failures.append(f"{area} forecast readiness after recovery")
                failed_areas.add(area)
            else:
                forecast_areas.append(area)
        if not forecast_areas and not source_areas:
            break

        production_ready = await attempt("production forecasts", ingest_production_forecasts)
        # A valid price forecast can outlive the inputs required by readiness.
        # Refresh sources once even when no new forecast needs to be generated.
        # Recover missing forecasts first, then reconcile again after all awaits.
        for area in [*forecast_areas, *source_areas]:
            refreshed_areas.add(area)
            if area in forecast_areas:
                attempted_areas.add(area)
            weather_ready = await attempt(
                f"{area} weather forecasts", ingest_weather_forecasts, area=area
            )
            # Capture issuance after live covariates arrive. Price preparation,
            # coverage validation and prediction must all use this same cutoff,
            # even if their awaits cross a quarter-hour or UTC-day boundary.
            issued_at = datetime.now(UTC)
            if price_window_fetched_until is None or issued_at >= price_window_fetched_until:
                await attempt("recent price backfill", backfill_prices, days=PRICE_HISTORY_DAYS)
                # Avoid repeating a failed shared fetch for every area. Persisted
                # coverage below still decides whether cached prices are usable.
                price_window_fetched_until = issued_at.replace(
                    hour=0, minute=0, second=0, microsecond=0
                ) + timedelta(days=1)
            # Prices recover independently of weather and production availability.
            if not weather_ready or not production_ready:
                failed_areas.add(area)
                continue
            if area not in forecast_areas:
                continue
            if not await attempt(
                f"{area} price history", verify_price_history, area, issued_at=issued_at
            ):
                failed_areas.add(area)
                continue
            if not await attempt(
                f"{area} forecast",
                run_forecast,
                area,
                issued_at=issued_at,
                model_version=models[area],
            ):
                failed_areas.add(area)

    if failures:
        raise RuntimeError(f"Bootstrap incomplete: {', '.join(failures)}")
    logger.info("All configured areas have usable models and forecasts")
