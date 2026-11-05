from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from unittest.mock import AsyncMock, patch

from sqlalchemy import text

from nordpool_predictor.ingestion import eds_client


async def test_rate_limit_persists_requests_and_keeps_datasets_independent(database):
    with patch.object(eds_client.asyncio, "sleep", AsyncMock()) as sleep:
        await eds_client._enforce_rate_limit("prices")
        await eds_client._enforce_rate_limit("weather")
        sleep.assert_not_awaited()
        await eds_client._enforce_rate_limit("prices")
    sleep.assert_awaited_once()
    assert 0 < sleep.await_args.args[0] <= 61


async def test_concurrent_requests_serialize_only_the_same_dataset(database):
    await eds_client._enforce_rate_limit("prices")
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(seconds):
        entered.set()
        await release.wait()

    tasks = []
    with patch.object(eds_client.asyncio, "sleep", side_effect=hold) as sleep:
        try:
            tasks.append(asyncio.create_task(eds_client._enforce_rate_limit("prices")))
            await asyncio.wait_for(entered.wait(), timeout=5)
            tasks.append(asyncio.create_task(eds_client._enforce_rate_limit("prices")))
            await asyncio.wait_for(eds_client._enforce_rate_limit("weather"), timeout=5)
            assert not tasks[1].done()
            sleep.assert_awaited_once()
            # A killed/cancelled caller must release the row lock for the next job.
            tasks[0].cancel()
            await asyncio.gather(tasks[0], return_exceptions=True)
            release.set()
            await asyncio.wait_for(tasks[1], timeout=5)
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_fresh_job_process_observes_the_previous_process_request(database):
    async with database.connect() as connection:
        schema = (await connection.execute(text("SELECT current_schema()"))).scalar_one()
    controller = textwrap.dedent("""
        import asyncio, json, os, sys
        from unittest.mock import AsyncMock, patch
        from sqlalchemy.ext.asyncio import create_async_engine
        from nordpool_predictor.ingestion import eds_client

        async def main():
            engine = create_async_engine(os.environ['TEST_DATABASE_URL'],
                connect_args={'server_settings': {'search_path': sys.argv[1]}})
            try:
                with (
                    patch('nordpool_predictor.database.get_engine', return_value=engine),
                    patch.object(eds_client.asyncio, 'sleep', AsyncMock()) as sleep,
                ):
                    await eds_client._enforce_rate_limit('prices')
                print(json.dumps([call.args[0] for call in sleep.await_args_list]))
            finally:
                await engine.dispose()
        asyncio.run(main())
    """)

    async def run_process():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            controller,
            schema,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
            assert process.returncode == 0, stderr.decode()
            return json.loads(stdout)
        finally:
            if process.returncode is None:
                process.kill()
                await process.communicate()

    assert await run_process() == []
    [wait] = await run_process()
    assert 0 < wait <= 61


async def test_explicit_rate_limit_bypass_does_not_access_database():
    import httpx

    response = httpx.Response(
        200, json={"records": []}, request=httpx.Request("GET", "https://example.com/tariffs")
    )
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = response
    with patch.object(eds_client, "_enforce_rate_limit", AsyncMock()) as rate:
        assert (
            await eds_client.eds_get("tariffs", {}, client=client, respect_rate_limit=False) == []
        )
    rate.assert_not_awaited()
