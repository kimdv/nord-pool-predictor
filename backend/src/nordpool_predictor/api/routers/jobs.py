from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from nordpool_predictor.api.schemas.health import JobRunResponse, JobSummary
from nordpool_predictor.database import get_session
from nordpool_predictor.scheduler.queue import JOB_NAMES, cancel_job, enqueue_job

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


@router.post("/trigger/{job_name}", status_code=202)
async def trigger_job(job_name: str) -> dict[str, str]:
    if job_name not in JOB_NAMES | {"bootstrap"}:
        raise HTTPException(status_code=404, detail=f"Unknown job: {job_name}")
    job_id = await enqueue_job(job_name)
    if job_id is None:
        raise HTTPException(status_code=409, detail=f"Job {job_name} is already queued or running")
    return {"status": "pending", "job": job_name, "job_id": str(job_id)}


@router.post("/cancel/{job_name}", status_code=202)
async def cancel_job_request(job_name: str) -> dict[str, str]:
    if job_name not in JOB_NAMES | {"bootstrap"}:
        raise HTTPException(status_code=404, detail=f"Unknown job: {job_name}")
    status = await cancel_job(job_name)
    if status is None:
        raise HTTPException(status_code=409, detail=f"Job {job_name} is no longer active")
    return {"status": status, "job": job_name}


@router.get("/requests/{job_id}")
async def get_job_request(job_id: UUID):
    async with get_session() as session:
        result = await session.execute(
            text(
                "SELECT job_id, job_name, status, created_at, started_at, finished_at, error "
                "FROM job_requests WHERE job_id = :id"
            ),
            {"id": job_id},
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
                "SELECT job_name FROM job_requests "
                "WHERE status IN ('pending', 'running', 'cancelling') "
                "ORDER BY job_name"
            )
        )
        return {"running": list(result.scalars())}


_SUMMARY_SQL = """
WITH latest_requests AS (
    SELECT DISTINCT ON (job_name) * FROM job_requests
    ORDER BY job_name, created_at DESC, job_id DESC
), request_summaries AS (
    SELECT
        request.job_name AS job_type,
        COALESCE(request.started_at, request.created_at) AS last_started_at,
        request.finished_at AS last_finished_at,
        request.status AS last_status,
        CASE WHEN request.job_name = 'bootstrap' THEN 1
             ELSE COUNT(attempt.job_id)::int END AS batch_size,
        CASE WHEN request.job_name = 'bootstrap' THEN (request.status = 'failed')::int
             ELSE COUNT(attempt.job_id) FILTER (WHERE attempt.status = 'failed')::int
        END AS failures_in_batch
    FROM latest_requests request
    LEFT JOIN job_runs attempt ON attempt.job_type = request.job_name
        AND attempt.created_at >= request.started_at
        AND (request.finished_at IS NULL OR attempt.created_at <= request.finished_at)
    GROUP BY request.job_id, request.job_name, request.created_at, request.started_at,
             request.finished_at, request.status
), historical_attempts AS (
    SELECT DISTINCT ON (job_type)
        job_type, started_at AS last_started_at, finished_at AS last_finished_at,
        status AS last_status,
        1 AS batch_size, (status = 'failed')::int AS failures_in_batch
    FROM job_runs attempt
    WHERE NOT EXISTS (
        SELECT 1 FROM latest_requests request WHERE request.job_name = attempt.job_type
    )
    ORDER BY job_type, started_at DESC NULLS LAST, created_at DESC, job_id DESC
)
SELECT
    * FROM request_summaries
UNION ALL
SELECT * FROM historical_attempts
ORDER BY job_type
"""


@router.get("/summary", response_model=list[JobSummary])
async def jobs_summary() -> list[JobSummary]:
    """Summarise the latest durable request, or a single historical attempt."""
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
