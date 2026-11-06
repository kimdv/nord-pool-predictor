from __future__ import annotations

import asyncio
import os
import sys
import textwrap
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest
import time_machine
from sqlalchemy import text

from nordpool_predictor.scheduler import jobs
from nordpool_predictor.scheduler.queue import claim_job, enqueue_job
from nordpool_predictor.worker import worker_lock


async def test_only_scheduler_wrapper_assigns_a_frozen_origin():
    issued = datetime(2026, 3, 29, 4, 5, 1, tzinfo=UTC)  # 06:05 after Danish spring DST.
    with (
        time_machine.travel(issued, tick=False),
        patch("nordpool_predictor.scheduler.queue.enqueue_job", AsyncMock()) as enqueue,
    ):
        await jobs._enqueue_scheduled_forecast()
    enqueue.assert_awaited_once()
    assert enqueue.await_args.args == ("refresh_forecast",)
    assert enqueue.await_args.kwargs["scheduled_at"].astimezone(UTC) == issued.replace(second=0)


@pytest.mark.parametrize("scheduled", [False, True])
async def test_origin_survives_real_worker_child_boundary(database, scheduled):
    origin = datetime(2026, 1, 1, 5, 5, tzinfo=UTC)
    request_id = await enqueue_job("refresh_forecast", scheduled_at=origin if scheduled else None)
    await claim_job()
    async with database.connect() as connection:
        schema = (await connection.execute(text("SELECT current_schema()"))).scalar_one()
    controller = textwrap.dedent("""
        import asyncio, os, sys
        from unittest.mock import patch
        from sqlalchemy.ext.asyncio import create_async_engine
        from nordpool_predictor import worker

        async def main():
            engine = create_async_engine(os.environ['TEST_DATABASE_URL'],
                connect_args={'server_settings': {'search_path': sys.argv[2]}})
            async def inspect(job_name):
                value = worker.forecast_origin.get()
                print(value.isoformat() if value else 'manual', flush=True)
            with (patch('nordpool_predictor.database.get_engine', return_value=engine),
                  patch.object(worker, 'run_job', inspect)):
                await worker.run_job_process('refresh_forecast', sys.argv[1], int(sys.argv[3]))
        asyncio.run(main())
    """)
    async with worker_lock() as connection:
        owner = (await connection.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            controller,
            str(request_id),
            schema,
            str(owner),
            env=os.environ.copy(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
            assert process.returncode == 0, stderr.decode()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    if scheduled:
        assert pd.Timestamp(stdout.decode().strip()) == pd.Timestamp(origin)
    else:
        assert stdout.decode().strip() == "manual"
