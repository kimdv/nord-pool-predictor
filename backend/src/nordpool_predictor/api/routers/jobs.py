from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from nordpool_predictor.api.schemas.health import JobRunResponse, JobSummary
from nordpool_predictor.database import get_session

BATCH_WINDOW_MINUTES = 10

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/jobs", tags=["jobs"])

_running_triggers: set[str] = set()


@router.get("", response_model=list[JobRunResponse])
async def list_jobs(
    limit: int = Query(20, ge=1, le=200),
) -> list[JobRunResponse]:
    async with get_session() as session:
        result = await session.execute(
            text(
                "SELECT job_id, job_type, status, started_at, finished_at "
                "FROM job_runs ORDER BY created_at DESC LIMIT :limit"
            ),
            {"limit": limit},
        )
        rows = result.mappings().all()

    return [
        JobRunResponse(
            job_id=str(row["job_id"]),
            job_type=row["job_type"],
            status=row["status"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )
        for row in rows
    ]


TRIGGER_MAP: dict[str, str] = {
    "ingest_prices": "nordpool_predictor.scheduler.jobs:job_ingest_prices",
    "ingest_weather": "nordpool_predictor.scheduler.jobs:job_ingest_weather",
    "ingest_production": "nordpool_predictor.scheduler.jobs:job_ingest_production",
    "ingest_crossborder": "nordpool_predictor.scheduler.jobs:job_ingest_crossborder",
    "refresh_forecast": "nordpool_predictor.scheduler.jobs:job_refresh_forecast",
    "score_forecasts": "nordpool_predictor.scheduler.jobs:job_score_forecasts",
    "retrain_model": "nordpool_predictor.scheduler.jobs:job_retrain_model",
}


def _resolve_job(path: str):  # noqa: ANN201
    module_path, func_name = path.rsplit(":", 1)
    import importlib

    module = importlib.import_module(module_path)
    return getattr(module, func_name)


@router.post("/trigger/{job_name}")
async def trigger_job(job_name: str) -> dict[str, str]:
    """Manually trigger a background job by name."""
    if job_name not in TRIGGER_MAP:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown job: {job_name}. Available: {list(TRIGGER_MAP.keys())}",
        )

    if job_name in _running_triggers:
        raise HTTPException(status_code=409, detail=f"Job {job_name} is already running")

    job_fn = _resolve_job(TRIGGER_MAP[job_name])

    async def _run() -> None:
        _running_triggers.add(job_name)
        try:
            await job_fn()
        except Exception:
            logger.exception("Manually triggered job %s failed", job_name)
        finally:
            _running_triggers.discard(job_name)

    asyncio.create_task(_run())
    logger.info("Manually triggered job: %s", job_name)

    return {"status": "triggered", "job": job_name}


@router.get("/running")
async def running_jobs() -> dict[str, list[str]]:
    """Return currently running manually-triggered jobs."""
    return {"running": sorted(_running_triggers)}


_SUMMARY_SQL = f"""
WITH latest AS (
    SELECT job_type, MAX(started_at) AS max_started_at
    FROM job_runs
    WHERE started_at IS NOT NULL
    GROUP BY job_type
),
batch AS (
    SELECT jr.job_type, jr.status, jr.started_at, jr.finished_at
    FROM job_runs jr
    JOIN latest l ON l.job_type = jr.job_type
    WHERE jr.started_at >= l.max_started_at - interval '{BATCH_WINDOW_MINUTES} minutes'
)
SELECT
    job_type,
    MAX(started_at) AS last_started_at,
    MAX(finished_at) FILTER (WHERE finished_at IS NOT NULL) AS last_finished_at,
    CASE
        WHEN bool_or(status = 'running') THEN 'running'
        WHEN bool_or(status = 'failed') THEN 'failed'
        ELSE 'completed'
    END AS last_status,
    COUNT(*)::int AS batch_size,
    COUNT(*) FILTER (WHERE status = 'failed')::int AS failures_in_batch
FROM batch
GROUP BY job_type
ORDER BY job_type
"""


@router.get("/summary", response_model=list[JobSummary])
async def jobs_summary() -> list[JobSummary]:
    """Return one entry per ``job_type`` summarising the most recent batch.

    A "batch" is defined as every ``job_runs`` row whose ``started_at`` lies
    within ``BATCH_WINDOW_MINUTES`` of the latest ``started_at`` for that
    ``job_type``.  This collapses per-area fan-out jobs (``refresh_forecast``,
    ``retrain_model``) into a single row whose status is:

    * ``running``   — any row in the batch is still running
    * ``failed``    — any row in the batch failed (and none are running)
    * ``completed`` — all rows finished successfully
    """
    async with get_session() as session:
        result = await session.execute(text(_SUMMARY_SQL))
        rows = result.mappings().all()

    return [
        JobSummary(
            job_type=row["job_type"],
            last_status=row["last_status"],
            last_started_at=row["last_started_at"],
            last_finished_at=row["last_finished_at"],
            batch_size=row["batch_size"],
            failures_in_batch=row["failures_in_batch"],
        )
        for row in rows
    ]
