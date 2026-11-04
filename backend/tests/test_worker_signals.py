from __future__ import annotations

import asyncio
import os
import signal
import sys
import textwrap


async def test_terminal_interrupt_waits_for_active_job():
    # Keep the simulated terminal group separate from pytest and the user's shell.
    controller = textwrap.dedent("""
        import asyncio
        import signal
        import sys
        from unittest.mock import AsyncMock, patch
        from nordpool_predictor import worker

        spawn = asyncio.create_subprocess_exec
        async def start_job(*args, **kwargs):
            return await spawn(sys.executable, '-u', '-c',
                "print('job-started', flush=True); input(); print('job-finished', flush=True)",
                **kwargs)
        async def wait_for_job(process, job_id):
            await process.wait()
        async def main():
            asyncio.get_running_loop().add_signal_handler(
                signal.SIGINT, lambda: print('worker-stopping', flush=True)
            )
            worker._worker_lock_context.set((None, None, 1, 1))
            finish = AsyncMock()
            with (
                patch.object(worker, 'claim_job', AsyncMock(return_value={
                    'job_id': 'test-request', 'job_name': 'cleanup'
                })),
                patch.object(worker.asyncio, 'create_subprocess_exec', start_job),
                patch.object(worker, 'wait_for_job', wait_for_job),
                patch.object(worker, 'finish_request', finish),
            ):
                assert await worker.execute_next()
            finish.assert_awaited_once_with('test-request', None)
        asyncio.run(main())
    """)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-c",
        controller,
        start_new_session=True,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert await asyncio.wait_for(process.stdout.readline(), timeout=10) == b"job-started\n"
        os.killpg(process.pid, signal.SIGINT)
        assert await asyncio.wait_for(process.stdout.readline(), timeout=5) == b"worker-stopping\n"
        process.stdin.write(b"finish\n")
        await process.stdin.drain()
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
        assert process.returncode == 0, stderr.decode()
        assert stdout == b"job-finished\n"
    finally:
        if process.returncode is None:
            process.stdin.close()
            process.kill()
            await asyncio.wait_for(process.communicate(), timeout=5)
