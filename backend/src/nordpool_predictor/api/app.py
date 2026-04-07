from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)

from nordpool_predictor import bootstrap_state  # noqa: E402
from nordpool_predictor.api.routers import (  # noqa: E402
    areas,
    forecasts,
    health,
    homeassistant,
    jobs,
    models,
    prices,
    tariffs,
)
from nordpool_predictor.config import get_settings  # noqa: E402
from nordpool_predictor.database import dispose_engine, run_migrations  # noqa: E402
from nordpool_predictor.scheduler.jobs import start_scheduler, stop_scheduler  # noqa: E402

logger = logging.getLogger(__name__)


async def _days_since_latest(session, table: str, ts_col: str = "ts") -> int | None:
    """Return the number of days since the most recent row, or None if empty."""
    from sqlalchemy import text

    result = await session.execute(text(f"SELECT MAX({ts_col}) FROM {table}"))  # noqa: S608
    latest = result.scalar()
    if latest is None:
        return None
    delta = datetime.now(UTC) - latest
    return max(0, delta.days)


async def _bootstrap() -> None:
    """First-run bootstrap: backfill data and train initial models if needed."""
    from sqlalchemy import text

    from nordpool_predictor.database import get_session

    async with get_session() as session:
        result = await session.execute(
            text("SELECT COUNT(*) FROM model_versions WHERE is_active = TRUE")
        )
        active_count = result.scalar() or 0

    if active_count > 0:
        logger.info("Active models found — skipping bootstrap")
        bootstrap_state.mark_done()
        return

    logger.info("No active models — running bootstrap")

    from nordpool_predictor.ingestion.crossborder import backfill_crossborder
    from nordpool_predictor.ingestion.prices import backfill_prices
    from nordpool_predictor.ingestion.production import backfill_production
    from nordpool_predictor.ingestion.weather import backfill_weather
    from nordpool_predictor.ml.predict import run_forecast
    from nordpool_predictor.ml.train import train_models

    settings = get_settings()
    areas = settings.area_codes
    target_days = settings.training_window_days

    async with get_session() as session:
        prices_gap = await _days_since_latest(session, "price_observations")
        weather_gap = await _days_since_latest(session, "weather_observations")
        production_gap = await _days_since_latest(session, "production_observations")
        crossborder_gap = await _days_since_latest(session, "crossborder_observations")

    def _days(gap: int | None) -> int:
        return target_days if gap is None else min(gap + 1, target_days)

    prices_days = _days(prices_gap)
    weather_days = _days(weather_gap)
    production_days = _days(production_gap)
    crossborder_days = _days(crossborder_gap)

    if prices_days > 0:
        logger.info(
            "Bootstrap 1/6: Backfilling %d days of prices (gap: %s)",
            prices_days,
            prices_gap,
        )
        await backfill_prices(days=prices_days)
    else:
        logger.info("Bootstrap 1/6: Prices up to date — skipping")

    if weather_days > 0:
        logger.info(
            "Bootstrap 2/6: Backfilling %d days of weather (gap: %s)",
            weather_days,
            weather_gap,
        )
        await backfill_weather(days=weather_days)
    else:
        logger.info("Bootstrap 2/6: Weather up to date — skipping")

    if production_days > 0:
        logger.info(
            "Bootstrap 3/6: Backfilling %d days of production (gap: %s)",
            production_days,
            production_gap,
        )
        await backfill_production(days=production_days)
    else:
        logger.info("Bootstrap 3/6: Production up to date — skipping")

    from nordpool_predictor.ingestion.production import (
        ingest_production_forecasts,
    )

    logger.info("Bootstrap 3b/6: Fetching Energinet production forecasts")
    await ingest_production_forecasts()

    if crossborder_days > 0:
        logger.info(
            "Bootstrap 4/6: Backfilling %d days of crossborder (gap: %s)",
            crossborder_days,
            crossborder_gap,
        )
        await backfill_crossborder(days=crossborder_days)
    else:
        logger.info("Bootstrap step 4/6: Crossborder up to date — skipping")

    for i, area in enumerate(areas, 1):
        logger.info(
            "Bootstrap step 5/6: Training models for %s (%d/%d areas)",
            area,
            i,
            len(areas),
        )
        await train_models(area)

    for i, area in enumerate(areas, 1):
        logger.info(
            "Bootstrap step 6/6: Running initial forecast for %s (%d/%d areas)",
            area,
            i,
            len(areas),
        )
        await run_forecast(area)

    bootstrap_state.mark_done()
    logger.info("Bootstrap complete — all data ingested and models trained")


async def _bootstrap_wrapper() -> None:
    """Wrapper that catches exceptions so the background task doesn't crash."""
    try:
        await _bootstrap()
    except Exception:
        logger.exception("Bootstrap failed — data may be incomplete")
        bootstrap_state.mark_done()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("Starting up – running database migrations")
    await run_migrations()

    logger.info("Starting bootstrap in background")
    task = asyncio.create_task(_bootstrap_wrapper())
    bootstrap_state.set_task(task)

    start_scheduler()
    yield
    stop_scheduler()

    if not task.done():
        logger.info("Cancelling in-progress bootstrap")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    logger.info("Shutting down – disposing database engine")
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Nord Pool Predictor API",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(prices.router)
    app.include_router(forecasts.router)
    app.include_router(models.router)
    app.include_router(health.router)
    app.include_router(areas.router)
    app.include_router(jobs.router)
    app.include_router(homeassistant.router)
    app.include_router(tariffs.router)

    return app
