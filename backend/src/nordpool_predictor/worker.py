"""Run with python -m nordpool_predictor.worker; one worker owns all background work."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from nordpool_predictor.database import dispose_engine, get_session, run_migrations
from nordpool_predictor.scheduler.jobs import start_scheduler, stop_scheduler
from nordpool_predictor.scheduler.queue import claim_job, enqueue_job, finish_request

logger = logging.getLogger(__name__)
WORKER_LOCK = 73640101
LOCK_CHECK_INTERVAL = 1
_worker_lock_context: ContextVar[tuple[AsyncConnection, asyncio.Lock, int] | None] = ContextVar(
    "worker_lock_context", default=None
)

_LOCK_OWNERSHIP_QUERY = text("""
    SELECT pg_backend_pid() = :pid AND EXISTS (
        SELECT 1 FROM pg_locks
        WHERE locktype = 'advisory'
          AND pid = CAST(:pid AS integer)
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

    conn, operation_lock, backend_pid = context
    async with operation_lock:
        owns_lock = (
            await conn.execute(
                _LOCK_OWNERSHIP_QUERY,
                {"pid": backend_pid, "key": WORKER_LOCK},
            )
        ).scalar()
        if not owns_lock:
            raise RuntimeError("Worker advisory lock session lost")
        await operation(conn)
        await conn.commit()
    return True


@asynccontextmanager
async def worker_lock():
    from nordpool_predictor.database import get_engine

    async with get_engine().connect() as conn:
        acquired = (
            await conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": WORKER_LOCK})
        ).scalar()
        await conn.commit()
        if not acquired:
            raise RuntimeError("Another backend worker is already running")
        backend_pid = (await conn.execute(text("SELECT pg_backend_pid()"))).scalar()
        operation_lock = asyncio.Lock()
        lock_context = _worker_lock_context.set((conn, operation_lock, backend_pid))
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
                                {"pid": backend_pid, "key": WORKER_LOCK},
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
                        text("SELECT pg_advisory_unlock(:key)"), {"key": WORKER_LOCK}
                    )
                    await conn.commit()
                except Exception:
                    logger.exception("Failed to release worker advisory lock")


async def execute_next() -> bool:
    request = await claim_job()
    if request is None:
        return False
    error = None
    try:
        if request["job_name"] == "bootstrap":
            from nordpool_predictor.api.app import _bootstrap

            await _bootstrap()
        else:
            from nordpool_predictor.scheduler import jobs

            await getattr(jobs, f"job_{request['job_name']}")()
    except Exception as exc:
        logger.exception("Job request %s failed", request["job_id"])
        error = str(exc) or type(exc).__name__
    await finish_request(request["job_id"], error)
    return True


async def run_worker() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await run_migrations()
        async with worker_lock():
            # The lock proves that the previous worker has exited. Preserve failure history.
            async with get_session() as session:
                await session.execute(
                    text("""
                    UPDATE job_requests SET status = 'failed', finished_at = NOW(),
                        error = 'Worker exited before completion' WHERE status = 'running'
                """)
                )
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
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker())
