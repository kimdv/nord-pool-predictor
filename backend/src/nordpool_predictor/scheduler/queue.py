"""Persistent requests shared by HTTP handlers and the worker's scheduler."""

from __future__ import annotations

from datetime import datetime
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


async def enqueue_job(job_name: str, *, scheduled_at: datetime | None = None) -> UUID | None:
    if job_name not in JOB_NAMES | {"bootstrap"}:
        raise ValueError(f"Unknown job: {job_name}")
    async with get_session() as session:
        result = await session.execute(
            text("""
            INSERT INTO job_requests (job_name, scheduled_at) VALUES (:name, :scheduled_at)
            ON CONFLICT (job_name) WHERE status IN ('pending', 'running', 'cancelling') DO NOTHING
            RETURNING job_id
        """),
            {"name": job_name, "scheduled_at": scheduled_at},
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
        result = await session.execute(
            text("""
            UPDATE job_requests
            SET status = CASE WHEN status = 'cancelling' THEN 'cancelled' ELSE :status END,
                error = CASE WHEN status = 'cancelling' THEN NULL ELSE COALESCE(error, :error) END,
                finished_at = NOW()
            WHERE job_id = :id AND status IN ('running', 'cancelling')
            RETURNING job_name, status, started_at, finished_at
        """),
            {"id": job_id, "status": "failed" if error else "completed", "error": error},
        )
        request = result.mappings().first()
        if request is not None:
            if request["job_name"] in {"refresh_forecast", "bootstrap"}:
                await session.execute(
                    text("""
                        UPDATE forecast_runs
                        SET status = 'failed', notes = :notes, updated_at = NOW()
                        WHERE status = 'running'
                          AND created_at BETWEEN :started_at AND :finished_at
                    """),
                    {
                        "started_at": request["started_at"],
                        "finished_at": request["finished_at"],
                        "notes": (
                            "Cancelled by user"
                            if request["status"] == "cancelled"
                            else "Worker job exited before forecast completion"
                        ),
                    },
                )
            await session.execute(
                text("""
                    UPDATE job_runs
                    SET status = CASE WHEN created_at >= :started_at
                                      THEN :status ELSE 'failed' END,
                        finished_at = NOW()
                    WHERE status = 'running' AND created_at <= :finished_at AND (
                        job_type = :name OR (:name = 'bootstrap' AND created_at >= :started_at)
                    )
                """),
                {
                    "name": request["job_name"],
                    "started_at": request["started_at"],
                    "finished_at": request["finished_at"],
                    "status": request["status"],
                },
            )
        await session.commit()


async def cancel_job(job_name: str) -> str | None:
    async with get_session() as session:
        result = await session.execute(
            text("""
                UPDATE job_requests
                SET status = CASE WHEN status = 'pending' THEN 'cancelled' ELSE 'cancelling' END,
                    finished_at = CASE WHEN status = 'pending' THEN NOW() ELSE NULL END
                WHERE job_name = :name AND status IN ('pending', 'running', 'cancelling')
                RETURNING status
            """),
            {"name": job_name},
        )
        status = result.scalar_one_or_none()
        if status is None or status == "cancelled":
            # Clear stale history only when there is no process still executing this job.
            result = await session.execute(
                text("""
                    UPDATE job_runs SET status = 'cancelled', finished_at = NOW()
                    WHERE job_type = :name AND status = 'running' AND created_at <= NOW()
                    RETURNING job_id
                """),
                {"name": job_name},
            )
            if result.first() is not None:
                status = "cancelled"
        await session.commit()
        return status
