"""Persistent requests shared by HTTP handlers and the worker's scheduler."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

from nordpool_predictor.database import get_session

JOB_NAMES = frozenset(
    {
        "ingest_prices",
        "ingest_weather",
        "ingest_production",
        "ingest_crossborder",
        "refresh_forecast",
        "score_forecasts",
        "retrain_model",
        "cleanup",
    }
)


async def enqueue_job(job_name: str) -> UUID | None:
    if job_name not in JOB_NAMES | {"bootstrap"}:
        raise ValueError(f"Unknown job: {job_name}")
    async with get_session() as session:
        result = await session.execute(
            text("""
            INSERT INTO job_requests (job_name) VALUES (:name)
            ON CONFLICT (job_name) WHERE status IN ('pending', 'running') DO NOTHING
            RETURNING job_id
        """),
            {"name": job_name},
        )
        job_id = result.scalar_one_or_none()
        await session.commit()
        return job_id


async def claim_job():
    async with get_session() as session:
        result = await session.execute(
            text("""
            UPDATE job_requests SET status = 'running', started_at = NOW()
            WHERE job_id = (
                SELECT job_id FROM job_requests WHERE status = 'pending'
                ORDER BY (job_name = 'bootstrap') DESC, created_at, job_id
                FOR UPDATE SKIP LOCKED LIMIT 1
            ) RETURNING job_id, job_name
        """)
        )
        row = result.mappings().first()
        await session.commit()
        return row


async def finish_request(job_id: UUID, error: str | None = None) -> None:
    async with get_session() as session:
        await session.execute(
            text("""
            UPDATE job_requests SET status = :status, error = :error, finished_at = NOW()
            WHERE job_id = :id
        """),
            {"id": job_id, "status": "failed" if error else "completed", "error": error},
        )
        await session.commit()
