from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text

from nordpool_predictor.config import get_settings
from nordpool_predictor.database import get_session

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


async def _log_job(
    job_type: str,
    status: str,
    details: dict[str, Any] | None = None,
) -> str:
    job_id = str(uuid.uuid4())
    try:
        async with get_session() as session:
            await session.execute(
                text(
                    "INSERT INTO job_runs "
                    "(job_id, job_type, status, started_at, details_json) "
                    "VALUES (CAST(:job_id AS uuid), :job_type, :status, "
                    ":started_at, CAST(:details AS jsonb))"
                ),
                {
                    "job_id": job_id,
                    "job_type": job_type,
                    "status": status,
                    "started_at": datetime.now(UTC),
                    "details": json.dumps(details or {}),
                },
            )
            await session.commit()
    except Exception:
        logger.exception(
            "Failed to record job_runs start row for %s (id=%s)",
            job_type,
            job_id,
        )
        raise
    return job_id


async def _finish_job(
    job_id: str,
    status: str,
    details: dict[str, Any] | None = None,
) -> None:
    try:
        async with get_session() as session:
            await session.execute(
                text(
                    "UPDATE job_runs SET status = :status, "
                    "finished_at = :finished_at, "
                    "details_json = CAST(:details AS jsonb) "
                    "WHERE job_id = CAST(:job_id AS uuid)"
                ),
                {
                    "job_id": job_id,
                    "status": status,
                    "finished_at": datetime.now(UTC),
                    "details": json.dumps(details or {}),
                },
            )
            await session.commit()
    except Exception:
        # Don't let a logging-table failure mask the original job error.
        logger.exception(
            "Failed to update job_runs row id=%s to status=%s",
            job_id,
            status,
        )


async def _run_job(job_type: str, coro_fn: Any, *args: Any) -> None:
    job_id = await _log_job(job_type, "running")
    args_repr = ", ".join(repr(a) for a in args) if args else ""
    args_suffix = f", args=[{args_repr}]" if args_repr else ""
    logger.info("Job %s starting (id=%s%s)", job_type, job_id, args_suffix)
    try:
        await coro_fn(*args)
        await _finish_job(job_id, "completed")
        logger.info("Job %s completed (id=%s%s)", job_type, job_id, args_suffix)
    except Exception as exc:
        logger.exception(
            "Job %s failed (id=%s%s): %s",
            job_type,
            job_id,
            args_suffix,
            exc,
        )
        await _finish_job(job_id, "failed", {"error": str(exc)})


async def job_ingest_prices() -> None:
    from nordpool_predictor.ingestion.prices import ingest_day_ahead

    await _run_job("ingest_prices", ingest_day_ahead)


async def job_ingest_weather() -> None:
    from nordpool_predictor.ingestion.weather import ingest_weather_forecasts

    await _run_job("ingest_weather", ingest_weather_forecasts)


async def job_ingest_production() -> None:
    from nordpool_predictor.ingestion.production import (
        ingest_production_actuals,
        ingest_production_forecasts,
    )

    async def _ingest_all_production() -> None:
        await ingest_production_actuals()
        await ingest_production_forecasts()

    await _run_job("ingest_production", _ingest_all_production)


async def job_ingest_crossborder() -> None:
    from nordpool_predictor.ingestion.crossborder import (
        ingest_crossborder_flows,
    )

    await _run_job("ingest_crossborder", ingest_crossborder_flows)


async def job_refresh_forecast() -> None:
    from nordpool_predictor.ml.predict import run_forecast

    settings = get_settings()
    areas = settings.area_codes
    for i, area in enumerate(areas, 1):
        logger.info("Refreshing forecast for area %s (%d/%d)", area, i, len(areas))
        await _run_job("refresh_forecast", run_forecast, area)


async def job_score_forecasts() -> None:
    from nordpool_predictor.ml.score import score_forecasts

    await _run_job("score_forecasts", score_forecasts)


async def job_retrain_model() -> None:
    from nordpool_predictor.ml.train import train_models

    settings = get_settings()
    areas = settings.area_codes
    for i, area in enumerate(areas, 1):
        logger.info("Retraining model for area %s (%d/%d)", area, i, len(areas))
        await _run_job("retrain_model", train_models, area)


async def job_cleanup() -> None:
    settings = get_settings()

    async def _do_cleanup() -> None:
        retention = [
            ("weather_observations", settings.retention_weather_obs_days),
            ("weather_forecasts", settings.retention_weather_fc_days),
            ("production_observations", settings.retention_production_days),
            ("production_forecasts", settings.retention_production_days),
            ("crossborder_observations", settings.retention_crossborder_days),
            ("crossborder_forecasts", settings.retention_crossborder_days),
            ("forecast_values", settings.retention_forecast_days),
            ("forecast_errors", settings.retention_forecast_days),
        ]
        async with get_session() as session:
            for table, days in retention:
                cutoff = datetime.now(UTC) - timedelta(days=days)
                ts_col = "created_at" if "forecast_" in table else "ts"
                result = await session.execute(
                    text(
                        f"DELETE FROM {table} "  # noqa: S608
                        f"WHERE {ts_col} < :cutoff"
                    ),
                    {"cutoff": cutoff},
                )
                logger.info(
                    "Cleanup %s: removed %d rows (> %d days)",
                    table,
                    result.rowcount,
                    days,
                )
            await session.commit()

    await _run_job("cleanup", _do_cleanup)


def start_scheduler() -> AsyncIOScheduler:
    global _scheduler
    tz = "Europe/Copenhagen"

    _scheduler = AsyncIOScheduler(timezone=tz)

    cron = CronTrigger
    _scheduler.add_job(
        job_ingest_prices,
        cron(hour=13, minute=15, timezone=tz),
        id="ingest_prices",
    )
    _scheduler.add_job(
        job_ingest_weather,
        cron(hour=13, minute=20, timezone=tz),
        id="ingest_weather",
    )
    _scheduler.add_job(
        job_ingest_production,
        cron(hour=13, minute=25, timezone=tz),
        id="ingest_production",
    )
    _scheduler.add_job(
        job_ingest_crossborder,
        cron(hour=13, minute=30, timezone=tz),
        id="ingest_crossborder",
    )
    _scheduler.add_job(
        job_refresh_forecast,
        cron(hour=15, minute=0, timezone=tz),
        id="refresh_forecast",
    )
    _scheduler.add_job(
        job_score_forecasts,
        cron(hour=6, minute=0, timezone=tz),
        id="score_forecasts",
    )
    _scheduler.add_job(
        job_retrain_model,
        cron(day_of_week="sun", hour=4, minute=0, timezone=tz),
        id="retrain_model",
    )
    _scheduler.add_job(
        job_cleanup,
        cron(day_of_week="sun", hour=3, minute=0, timezone=tz),
        id="cleanup",
    )

    _scheduler.start()
    logger.info(
        "Scheduler started with %d jobs",
        len(_scheduler.get_jobs()),
    )
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler stopped")
