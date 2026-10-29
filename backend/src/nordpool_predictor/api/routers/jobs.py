from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from nordpool_predictor.api.auth import require_admin
from nordpool_predictor.api.schemas.health import JobRunResponse, JobSummary
from nordpool_predictor.database import get_session
from nordpool_predictor.scheduler.queue import JOB_NAMES, enqueue_job

BATCH_WINDOW_MINUTES = 10

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/jobs", tags=["jobs"])


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


@router.post("/trigger/{job_name}", status_code=202, dependencies=[Depends(require_admin)])
async def trigger_job(job_name: str) -> dict[str, str]:
    if job_name not in JOB_NAMES:
        raise HTTPException(status_code=404, detail=f"Unknown job: {job_name}")
    job_id = await enqueue_job(job_name)
    if job_id is None:
        raise HTTPException(status_code=409, detail=f"Job {job_name} is already queued or running")
    return {"status": "pending", "job": job_name, "job_id": str(job_id)}


@router.get("/requests/{job_id}")
async def get_job_request(job_id: UUID):
    async with get_session() as session:
        result = await session.execute(
            text("SELECT * FROM job_requests WHERE job_id = :id"), {"id": job_id}
        )
        row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Job request not found")
    return dict(row)


@router.get("/running")
async def running_jobs() -> dict[str, list[str]]:
    async with get_session() as session:
        result = await session.execute(
            text(
                "SELECT job_name FROM job_requests WHERE status IN ('pending', 'running') "
                "ORDER BY job_name"
            )
        )
        return {"running": list(result.scalars())}


_SUMMARY_SQL = f"""
WITH activity AS (
    SELECT job_type, status, started_at, finished_at FROM job_runs
    UNION ALL
    SELECT job_name, 'running', COALESCE(started_at, created_at), finished_at
    FROM job_requests WHERE status IN ('pending', 'running')
), latest AS (
    SELECT job_type, MAX(started_at) AS max_started_at
    FROM activity
    WHERE started_at IS NOT NULL
    GROUP BY job_type
),
batch AS (
    SELECT jr.job_type, jr.status, jr.started_at, jr.finished_at
    FROM activity jr
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
