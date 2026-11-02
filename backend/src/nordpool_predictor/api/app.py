from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)

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

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("Starting up – running database migrations")
    await run_migrations()

    try:
        yield
    finally:
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
        expose_headers=["X-Next-Cursor"],
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
