from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from nordpool_predictor.scheduler.queue import cancel_job, claim_job, enqueue_job, finish_request
from nordpool_predictor.worker import run_worker, worker_lock


async def test_job_process_lock_allows_production_forecast_ingestion(database):
    from nordpool_predictor.ingestion import production

    now = datetime.now(UTC)
    rows = production._parse_forecast_records(
        [
            {
                "HourUTC": now.replace(minute=0, second=0, microsecond=0).isoformat(),
                "PriceArea": "DK1",
                "TimestampUTC": (now - timedelta(hours=1)).isoformat(),
                "ForecastType": "Solar",
                "ForecastCurrent": 9,
            }
        ]
    )
    async with worker_lock() as conn:
        pid = (await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        async with worker_lock(pid):
            assert await asyncio.wait_for(production._upsert_forecasts(rows), timeout=2) == 1
    async with database.connect() as conn:
        assert (
            await conn.execute(text("SELECT solar_mw FROM production_forecasts"))
        ).scalar_one() == 9


@pytest.mark.parametrize("job_name", ["refresh_forecast", "bootstrap"])
@pytest.mark.parametrize("restart", [False, True])
async def test_cancellation_finalizes_only_its_unfinished_forecasts(database, job_name, restart):
    from nordpool_predictor.ml import predict

    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO model_versions
                (model_version, model_type, area, horizon_strategy, feature_set_version,
                 trained_at, artifact_path, is_active)
            VALUES ('cancellation-test', 'lgbm_quantile', 'DK1', 'direct', 'v2-asof',
                    NOW(), '/unused', TRUE)
        """)
        )
        await conn.execute(
            text("""
            INSERT INTO forecast_runs
                (area, model_version, issued_at, horizon_steps, status, created_at, notes)
            VALUES ('DK1', 'cancellation-test', NOW(), 1, 'running',
                    NOW() - interval '1 day', 'older'),
                   ('DK1', 'cancellation-test', NOW(), 1, 'running',
                    NOW() + interval '1 day', 'newer')
        """)
        )
    job_id = await enqueue_job(job_name)
    await claim_job()
    entered = asyncio.Event()

    async def hold_features(*args):
        entered.set()
        await asyncio.Event().wait()

    with (
        patch.object(
            predict, "select_usable_model", AsyncMock(return_value=("cancellation-test", {}))
        ),
        patch.object(predict.asyncio, "to_thread", side_effect=hold_features),
    ):
        task = asyncio.create_task(
            predict.run_forecast(
                "DK1", horizon_steps=1, issued_at=datetime.now(UTC) - timedelta(days=2)
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO forecast_runs
                (area, model_version, issued_at, horizon_steps, status, notes)
            VALUES ('DK1', 'cancellation-test', NOW(), 1, 'completed', 'keep completed')
        """)
        )
        assert (
            await conn.execute(text("SELECT status FROM forecast_runs WHERE notes IS NULL"))
        ).scalar_one() == "running"
    assert await cancel_job(job_name) == "cancelling"
    if restart:
        with (
            patch.object(asyncio.get_running_loop(), "add_signal_handler"),
            patch("nordpool_predictor.worker.start_scheduler"),
            patch("nordpool_predictor.worker.stop_scheduler"),
            patch(
                "nordpool_predictor.worker.execute_next",
                side_effect=RuntimeError("stop test worker"),
            ),
        ):
            with pytest.raises(RuntimeError, match="stop test worker"):
                await run_worker()
    else:
        await finish_request(job_id, "Job process exited (-9)")
    async with database.connect() as conn:
        rows = (
            await conn.execute(text("SELECT status, notes FROM forecast_runs ORDER BY created_at"))
        ).all()
        assert [(row.status, row.notes) for row in rows] == [
            ("running", "older"),
            ("failed", "Cancelled by user"),
            ("completed", "keep completed"),
            ("running", "newer"),
        ]
        assert (
            await conn.execute(
                text("SELECT status FROM job_requests WHERE job_id = :id"), {"id": job_id}
            )
        ).scalar_one() == "cancelled"


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
async def test_request_finalization_recovers_history_without_relabeling_old_runs(database, status):
    from nordpool_predictor.scheduler import jobs

    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO job_runs (job_type, status, created_at)
            VALUES ('refresh_forecast', 'running', NOW() - interval '1 day'),
                   ('refresh_forecast', 'running', NOW() + interval '1 day')
        """)
        )
    job_id = await enqueue_job("refresh_forecast")
    await claim_job()
    # A suppressed history-write failure leaves a successful area's row running.
    with patch.object(jobs, "_finish_job", AsyncMock()):
        await jobs._run_job("refresh_forecast", AsyncMock())
    if status == "cancelled":
        await cancel_job("refresh_forecast")
    await finish_request(job_id, "Job failed" if status == "failed" else None)
    async with database.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT status, finished_at FROM job_runs ORDER BY created_at")
            )
        ).all()
        assert [row.status for row in rows] == ["failed", status, "running"]
        assert rows[0].finished_at is not None
        assert rows[1].finished_at is not None
        assert rows[2].finished_at is None
