"""Run with python -m nordpool_predictor.worker; one worker owns all background work."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from nordpool_predictor.database import dispose_engine, get_session, run_migrations
from nordpool_predictor.scheduler.jobs import start_scheduler, stop_scheduler
from nordpool_predictor.scheduler.queue import claim_job, enqueue_job, finish_request

logger = logging.getLogger(__name__)
WORKER_LOCK = 73640101
JOB_PROCESS_LOCK = 73640104
LOCK_CHECK_INTERVAL = 1
forecast_origin: ContextVar[datetime | None] = ContextVar("forecast_origin", default=None)
_worker_lock_context: ContextVar[tuple[AsyncConnection, asyncio.Lock, int, int] | None] = (
    ContextVar("worker_lock_context", default=None)
)

_LOCK_OWNERSHIP_QUERY = text("""
    SELECT pg_backend_pid() = :pid AND EXISTS (
        SELECT 1 FROM pg_locks
        WHERE locktype = 'advisory'
          AND pid = CAST(:owner_pid AS integer)
          AND classid = 0::oid
          AND objid = CAST(:key AS oid)
          AND objsubid = 1
          AND granted
    )
""")


async def run_with_worker_lock(operation: Callable[[AsyncConnection], Awaitable[None]]) -> bool:
    context = _worker_lock_context.get()
    if context is None:
        return False

    conn, operation_lock, backend_pid, owner_pid = context
    async with operation_lock:
        owns_lock = (
            await conn.execute(
                _LOCK_OWNERSHIP_QUERY,
                {"pid": backend_pid, "owner_pid": owner_pid, "key": WORKER_LOCK},
            )
        ).scalar()
        if not owns_lock:
            raise RuntimeError("Worker advisory lock session lost")
        try:
            await operation(conn)
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
    return True


@asynccontextmanager
async def worker_lock(owner_pid: int | None = None):
    from nordpool_predictor.database import get_engine

    async with get_engine().connect() as conn:
        if owner_pid is None:
            acquired = (
                await conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": WORKER_LOCK})
            ).scalar()
            await conn.commit()
            if not acquired:
                raise RuntimeError("Another backend worker is already running")
        else:
            # A restarted worker must wait for any surviving old child to exit.
            await conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": JOB_PROCESS_LOCK})
            await conn.commit()
        backend_pid = (await conn.execute(text("SELECT pg_backend_pid()"))).scalar()
        lock_owner_pid = backend_pid if owner_pid is None else owner_pid
        operation_lock = asyncio.Lock()
        lock_context = _worker_lock_context.set(
            (conn, operation_lock, backend_pid, lock_owner_pid)
        )
        owner_task = asyncio.current_task()
        lock_error: list[Exception] = []

        async def monitor_lock() -> None:
            try:
                while True:
                    await asyncio.sleep(LOCK_CHECK_INTERVAL)
                    async with operation_lock:
                        owns_lock = (
                            await conn.execute(
                                _LOCK_OWNERSHIP_QUERY,
                                {
                                    "pid": backend_pid,
                                    "owner_pid": lock_owner_pid,
                                    "key": WORKER_LOCK,
                                },
                            )
                        ).scalar()
                        await conn.commit()
                    if not owns_lock:
                        raise RuntimeError("Worker advisory lock session lost")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                lock_error.append(exc)
                logger.critical("Worker advisory lock session lost; terminating process")
                os._exit(1)
                if owner_task is not None:
                    owner_task.cancel()

        monitor = asyncio.create_task(monitor_lock())
        try:
            try:
                yield conn
            except asyncio.CancelledError:
                if lock_error:
                    raise RuntimeError("Worker advisory lock session lost") from lock_error[0]
                raise
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
            _worker_lock_context.reset(lock_context)
            if not lock_error:
                try:
                    await conn.execute(
                        text("SELECT pg_advisory_unlock(:key)"),
                        {"key": WORKER_LOCK if owner_pid is None else JOB_PROCESS_LOCK},
                    )
                    await conn.commit()
                except Exception:
                    logger.exception("Failed to release worker advisory lock")


async def run_job(job_name: str) -> None:
    if job_name == "bootstrap":
        from nordpool_predictor.scheduler.bootstrap import bootstrap

        await bootstrap()
    else:
        from nordpool_predictor.scheduler import jobs

        await getattr(jobs, f"job_{job_name}")()


async def wait_for_job(process: asyncio.subprocess.Process, job_id) -> None:
    """Reap the process before releasing its queue slot, including native training threads."""
    waiter = asyncio.create_task(process.wait())
    try:
        while not waiter.done():
            async with get_session() as session:
                status = (
                    await session.execute(
                        text("SELECT status FROM job_requests WHERE job_id = :id"), {"id": job_id}
                    )
                ).scalar_one()
            if status == "cancelling":
                break
            await asyncio.wait({waiter}, timeout=LOCK_CHECK_INTERVAL)
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await waiter


async def execute_next() -> bool:
    context = _worker_lock_context.get()
    if context is None:
        raise RuntimeError("Job execution requires the worker lock")
    request = await claim_job()
    if request is None:
        return False
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "nordpool_predictor.worker",
        "--job",
        request["job_name"],
        "--request-id",
        str(request["job_id"]),
        "--owner-pid",
        str(context[3]),
        start_new_session=True,
    )
    await wait_for_job(process, request["job_id"])
    error = None if process.returncode == 0 else f"Job process exited ({process.returncode})"
    await finish_request(request["job_id"], error)
    return True


async def run_job_process(job_name: str, request_id: str, owner_pid: int) -> None:
    try:
        async with worker_lock(owner_pid):

            async def verify_owner(connection):
                pass

            await run_with_worker_lock(verify_owner)
            origin = None
            if job_name == "refresh_forecast":
                async with get_session() as session:
                    origin = (
                        await session.execute(
                            text("SELECT scheduled_at FROM job_requests WHERE job_id = :id"),
                            {"id": request_id},
                        )
                    ).scalar_one()
            origin_token = forecast_origin.set(origin)
            try:
                await run_job(job_name)
            except Exception as exc:
                async with get_session() as session:
                    await session.execute(
                        text("UPDATE job_requests SET error = :error WHERE job_id = :id"),
                        {"id": request_id, "error": str(exc) or type(exc).__name__},
                    )
                    await session.commit()
                raise
            finally:
                forecast_origin.reset(origin_token)
    finally:
        await dispose_engine()


async def run_worker() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await run_migrations()
        async with worker_lock():
            # Wait for any surviving child before settling the previous worker's requests.
            async with get_session() as session:
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"), {"key": JOB_PROCESS_LOCK}
                )
                requests = await session.execute(
                    text(
                        "SELECT job_id FROM job_requests WHERE status IN ('running', 'cancelling')"
                    )
                )
                for job_id in requests.scalars():
                    await finish_request(job_id, "Worker exited before completion")
                await session.execute(
                    text("""
                    UPDATE job_runs SET status = 'failed', finished_at = NOW()
                    WHERE status = 'running'
                """)
                )
                await session.commit()
            await enqueue_job("bootstrap")
            start_scheduler()
            try:
                while not stop.is_set():
                    if not await execute_next():
                        try:
                            await asyncio.wait_for(stop.wait(), timeout=1)
                        except TimeoutError:
                            pass
            finally:
                stop_scheduler()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    import argparse

    from nordpool_predictor.worker import run_job_process, run_worker

    parser = argparse.ArgumentParser()
    parser.add_argument("--job")
    parser.add_argument("--request-id")
    parser.add_argument("--owner-pid", type=int)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.job:
        asyncio.run(run_job_process(args.job, args.request_id, args.owner_pid))
    else:
        asyncio.run(run_worker())
