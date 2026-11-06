from __future__ import annotations

import asyncio
import threading
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from nordpool_predictor.worker import JOB_PROCESS_LOCK, worker_lock


@pytest.fixture
def cli():
    spec = spec_from_file_location(
        "short_term_cli", Path(__file__).parents[1] / "scripts/short_term_experiment.py"
    )
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("command", ["train", "capture", "reproduce", "report", "approve-runtime"])
async def test_cli_refuses_to_overlap_a_live_worker(database, cli, command):
    with patch.object(cli, "_run_command", AsyncMock()) as command_body:
        async with worker_lock():
            with pytest.raises(RuntimeError, match="already running"):
                await cli.main(SimpleNamespace(command=command, area="DK1"))
        command_body.assert_not_awaited()


async def test_cli_refuses_a_surviving_child_even_after_parent_exits(database, cli):
    async with database.connect() as child:
        await child.execute(text("SELECT pg_advisory_lock(:key)"), {"key": JOB_PROCESS_LOCK})
        await child.commit()
        try:
            with patch.object(cli, "_run_command", AsyncMock()) as command_body:
                with pytest.raises(RuntimeError, match="worker job is still running"):
                    await cli.main(SimpleNamespace(command="train", area="DK1"))
                command_body.assert_not_awaited()
        finally:
            await child.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": JOB_PROCESS_LOCK})
            await child.commit()


async def test_cli_holds_both_existing_locks_until_the_command_finishes(database, cli):
    async def verify_locks(args):
        async with database.connect() as conn:
            assert not (
                await conn.execute(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": JOB_PROCESS_LOCK}
                )
            ).scalar_one()
        with pytest.raises(RuntimeError, match="already running"):
            async with worker_lock():
                pytest.fail("CLI lost exclusive ownership")

    with patch.object(cli, "_run_command", side_effect=verify_locks) as command_body:
        await cli.main(SimpleNamespace(command="reproduce", area="DK1"))
    command_body.assert_awaited_once()
    async with worker_lock():
        pass


async def test_cancellation_keeps_locks_until_native_fitting_has_stopped(database, cli):
    started, release = threading.Event(), threading.Event()

    def fit():
        started.set()
        release.wait(timeout=5)

    async def native_command(args):
        await asyncio.to_thread(fit)

    with patch.object(cli, "_run_command", side_effect=native_command):
        task = asyncio.create_task(cli.main(SimpleNamespace(command="train", area="DK1")))
        try:
            while not started.is_set():
                await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.sleep(0.05)
            assert not task.done()
            with pytest.raises(RuntimeError, match="already running"):
                async with worker_lock():
                    pytest.fail("Native fitting was still alive after locks were released")
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    async with worker_lock():
        pass
