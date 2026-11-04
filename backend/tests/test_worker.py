from __future__ import annotations

import asyncio
import runpy
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from nordpool_predictor.scheduler.queue import cancel_job, claim_job, enqueue_job, finish_request
from nordpool_predictor.worker import (
    execute_next,
    run_job_process,
    run_with_worker_lock,
    worker_lock,
)


async def test_concurrent_submissions_are_deduplicated_and_claimed_once(database):
    ids = await asyncio.gather(*(enqueue_job("ingest_prices") for _ in range(10)))
    assert sum(job_id is not None for job_id in ids) == 1
    claims = await asyncio.gather(*(claim_job() for _ in range(4)))
    assert sum(row is not None for row in claims) == 1
    assert await enqueue_job("ingest_prices") is None


async def test_only_one_worker_can_own_background_execution(database):
    async with worker_lock():
        with pytest.raises(RuntimeError, match="already running"):
            async with worker_lock():
                pytest.fail("A second worker acquired the lock")
    async with worker_lock():
        pass


async def test_failed_lock_owned_operation_is_rolled_back(database):
    async with worker_lock():

        async def fail(connection):
            await connection.execute(text("SELECT 1 / 0"))

        with pytest.raises(DBAPIError):
            await run_with_worker_lock(fail)

        async def succeed(connection):
            await connection.execute(text("SELECT 1"))

        assert await run_with_worker_lock(succeed)


def test_module_entrypoint_uses_canonical_worker():
    import nordpool_predictor.worker as worker_module

    result = object()
    with (
        patch(
            "nordpool_predictor.worker.run_worker",
            new=MagicMock(return_value=result),
        ) as run_worker,
        patch("asyncio.run") as asyncio_run,
        patch("sys.argv", ["worker"]),
    ):
        runpy.run_path(worker_module.__file__, run_name="__main__")

    run_worker.assert_called_once_with()
    asyncio_run.assert_called_once_with(result)


async def test_worker_stops_in_flight_work_after_lock_session_ends(database):
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    backend_pids = []

    async def hold_worker_lock():
        async with worker_lock() as lock_conn:
            backend_pid = (await lock_conn.execute(text("SELECT pg_backend_pid()"))).scalar()
            backend_pids.append(backend_pid)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    with (
        patch("nordpool_predictor.database.get_engine", return_value=database),
        patch("nordpool_predictor.worker.os._exit") as terminate_process,
    ):
        task = asyncio.create_task(hold_worker_lock())
        await entered.wait()
        async with database.connect() as conn:
            terminated = (
                await conn.execute(
                    text("SELECT pg_terminate_backend(:pid)"), {"pid": backend_pids[0]}
                )
            ).scalar()
            await conn.commit()
        assert terminated
        with pytest.raises(RuntimeError, match="lock session lost"):
            await asyncio.wait_for(task, timeout=5)
        assert cancelled.is_set()
        terminate_process.assert_called_once_with(1)
        async with worker_lock():
            pass


async def test_worker_persists_failure_and_accepts_a_later_retry(database):
    job_id = await enqueue_job("ingest_prices")
    await claim_job()
    with patch(
        "nordpool_predictor.scheduler.jobs.job_ingest_prices",
        AsyncMock(side_effect=RuntimeError("upstream unavailable")),
    ):
        async with worker_lock() as connection:
            pid = (await connection.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            with pytest.raises(RuntimeError, match="upstream unavailable"):
                await run_job_process("ingest_prices", str(job_id), pid)
    await finish_request(job_id, "Job process exited (1)")
    async with database.connect() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT status, error FROM job_requests WHERE job_id = :id"),
                    {"id": job_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["status"] == "failed"
        assert row["error"] == "upstream unavailable"
    assert await enqueue_job("ingest_prices") is not None


async def test_cancellation_stops_process_and_releases_queue_slot(database):
    from nordpool_predictor.api.routers.jobs import jobs_summary
    from nordpool_predictor.scheduler.jobs import _log_job

    job_id = await enqueue_job("retrain_model")
    started = asyncio.Event()
    processes = []
    spawn = asyncio.create_subprocess_exec

    async def start_process(*args, **kwargs):
        await _log_job("retrain_model", "running")
        process = await spawn(
            sys.executable,
            "-c",
            "import asyncio, time; asyncio.run(asyncio.to_thread(time.sleep, 3600))",
            **kwargs,
        )
        processes.append(process)
        started.set()
        return process

    async with worker_lock():
        with patch("nordpool_predictor.worker.asyncio.create_subprocess_exec", start_process):
            task = asyncio.create_task(execute_next())
            try:
                await asyncio.wait_for(started.wait(), timeout=5)
                assert await cancel_job("retrain_model") == "cancelling"
                assert await cancel_job("retrain_model") == "cancelling"
                assert await enqueue_job("retrain_model") is None
                assert (await jobs_summary())[0].last_status == "cancelling"
                assert await asyncio.wait_for(task, timeout=5)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
    assert processes[0].returncode == -9
    assert (await jobs_summary())[0].last_status == "cancelled"
    async with database.connect() as conn:
        request = (
            (
                await conn.execute(
                    text("SELECT status, finished_at FROM job_requests WHERE job_id = :id"),
                    {"id": job_id},
                )
            )
            .mappings()
            .one()
        )
        assert request["status"] == "cancelled"
        assert request["finished_at"] is not None
        assert (
            await conn.execute(text("SELECT status FROM job_runs"))
        ).scalar_one() == "cancelled"
    assert await enqueue_job("retrain_model") is not None


async def test_cancel_pending_job_does_not_execute_or_overwrite_cancellation(database):
    job_id = await enqueue_job("ingest_weather")
    assert await cancel_job("ingest_weather") == "cancelled"
    assert await claim_job() is None
    await finish_request(job_id)
    async with database.connect() as conn:
        assert (
            await conn.execute(
                text("SELECT status FROM job_requests WHERE job_id = :id"), {"id": job_id}
            )
        ).scalar_one() == "cancelled"
    assert await enqueue_job("ingest_weather") is not None


async def test_cancel_clears_legacy_orphaned_run_history(database):
    from nordpool_predictor.api.routers.jobs import jobs_summary
    from nordpool_predictor.scheduler.jobs import _log_job

    await _log_job("refresh_forecast", "running")
    assert await cancel_job("refresh_forecast") == "cancelled"
    assert (await jobs_summary())[0].last_status == "cancelled"
    assert await cancel_job("refresh_forecast") is None


async def test_completed_job_wins_a_late_cancellation(database):
    job_id = await enqueue_job("ingest_prices")
    await claim_job()
    await finish_request(job_id)
    assert await cancel_job("ingest_prices") is None


async def test_child_rejects_a_worker_without_the_ownership_lock(database):
    async with database.connect() as conn:
        pid = (await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one()
    with patch("nordpool_predictor.worker.run_job", new_callable=AsyncMock) as operation:
        with pytest.raises(RuntimeError, match="lock session lost"):
            await run_job_process("ingest_prices", "unused", pid)
    operation.assert_not_awaited()


async def test_child_stops_when_its_parent_releases_ownership(database):
    entered = asyncio.Event()

    async def child(pid):
        async with worker_lock(pid):
            entered.set()
            await asyncio.Event().wait()

    with patch("nordpool_predictor.worker.os._exit") as terminate_process:
        async with worker_lock() as connection:
            pid = (await connection.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            task = asyncio.create_task(child(pid))
            await asyncio.wait_for(entered.wait(), timeout=5)
        with pytest.raises(RuntimeError, match="lock session lost"):
            await asyncio.wait_for(task, timeout=5)
        terminate_process.assert_called_once_with(1)


async def test_restart_finishes_interrupted_cancellation(database):
    from nordpool_predictor.scheduler.jobs import _log_job
    from nordpool_predictor.worker import run_worker

    job_id = await enqueue_job("retrain_model")
    await claim_job()
    await _log_job("retrain_model", "running")
    await cancel_job("retrain_model")
    with (
        patch.object(asyncio.get_running_loop(), "add_signal_handler"),
        patch("nordpool_predictor.worker.start_scheduler"),
        patch("nordpool_predictor.worker.stop_scheduler"),
        patch(
            "nordpool_predictor.worker.execute_next", side_effect=RuntimeError("stop test worker")
        ),
    ):
        with pytest.raises(RuntimeError, match="stop test worker"):
            await run_worker()
    async with database.connect() as conn:
        assert (
            await conn.execute(
                text("SELECT status FROM job_requests WHERE job_id = :id"), {"id": job_id}
            )
        ).scalar_one() == "cancelled"
        assert (
            await conn.execute(text("SELECT status FROM job_runs"))
        ).scalar_one() == "cancelled"
    assert await enqueue_job("retrain_model") is not None


async def test_bootstrap_is_claimed_before_other_pending_work(database):
    await enqueue_job("retrain_model")
    await enqueue_job("bootstrap")
    assert (await claim_job())["job_name"] == "bootstrap"


async def test_area_failure_does_not_skip_other_areas(database):
    from types import SimpleNamespace

    from nordpool_predictor.scheduler.jobs import _run_area_jobs

    operation = AsyncMock(side_effect=[RuntimeError("DK1 unavailable"), None])
    with patch(
        "nordpool_predictor.scheduler.jobs.get_settings",
        return_value=SimpleNamespace(area_codes=["DK1", "DK2"]),
    ):
        with pytest.raises(RuntimeError, match="DK1"):
            await _run_area_jobs("refresh_forecast", operation)
    assert [call.args[0] for call in operation.await_args_list] == ["DK1", "DK2"]
    async with database.connect() as conn:
        statuses = list(
            (await conn.execute(text("SELECT status FROM job_runs ORDER BY started_at"))).scalars()
        )
    assert statuses == ["failed", "completed"]
