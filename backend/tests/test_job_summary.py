from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from nordpool_predictor.api.routers.jobs import jobs_summary


async def test_long_request_retains_earlier_area_failure(database):
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO job_requests (job_name, status, created_at, started_at, finished_at)
            VALUES ('refresh_forecast', 'failed', '2026-11-01 03:59Z',
                    '2026-11-01 04:00Z', '2026-11-01 04:31Z');
        """)
        )
        await conn.execute(
            text("""
            INSERT INTO job_runs (job_type, status, created_at, started_at, finished_at)
            VALUES ('refresh_forecast', 'failed', '2026-11-01 04:00Z', '2026-11-01 04:00Z',
                    '2026-11-01 04:15Z'),
                   ('refresh_forecast', 'completed', '2026-11-01 04:15Z', '2026-11-01 04:15Z',
                    '2026-11-01 04:30Z')
        """)
        )
    [summary] = await jobs_summary()
    assert summary.last_status == "failed"
    assert summary.batch_size == 2
    assert summary.failures_in_batch == 1
    assert summary.last_started_at == datetime(2026, 11, 1, 4, tzinfo=UTC)
    assert summary.last_finished_at == datetime(2026, 11, 1, 4, 31, tzinfo=UTC)
    assert await jobs_summary() == [summary]


@pytest.mark.parametrize("status", ["pending", "running", "completed", "failed"])
async def test_latest_request_owns_status_and_timestamps(database, status):
    started = None if status == "pending" else datetime(2026, 11, 1, 4, tzinfo=UTC)
    finished = datetime(2026, 11, 1, 5, tzinfo=UTC) if status in {"completed", "failed"} else None
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO job_requests (job_name, status, created_at, started_at, finished_at)
            VALUES ('refresh_forecast', 'failed', '2026-11-01 02:00Z',
                    '2026-11-01 02:00Z', '2026-11-01 03:00Z'),
                   ('refresh_forecast', :status, '2026-11-01 03:59Z', :started, :finished)
        """),
            {"status": status, "started": started, "finished": finished},
        )
        await conn.execute(
            text("""
            INSERT INTO job_runs (job_type, status, created_at, started_at, finished_at)
            VALUES ('refresh_forecast', 'failed', '2026-11-01 02:00Z', '2026-11-01 02:00Z',
                    '2026-11-01 03:00Z'),
                   ('refresh_forecast', 'completed', '2026-11-01 04:15Z', '2026-11-01 04:15Z',
                    '2026-11-01 04:30Z')
        """)
        )
    [summary] = await jobs_summary()
    assert summary.last_status == ("running" if status == "pending" else status)
    assert summary.last_started_at == (started or datetime(2026, 11, 1, 3, 59, tzinfo=UTC))
    assert summary.last_finished_at == finished
    assert summary.batch_size == (0 if status == "pending" else 1)
    assert summary.failures_in_batch == 0


async def test_request_window_excludes_bootstrap_and_old_attempts(database):
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO job_requests (job_name, status, created_at, started_at, finished_at)
            VALUES ('refresh_forecast', 'completed', '2026-11-01 04:00Z',
                    '2026-11-01 04:30Z', '2026-11-01 05:00Z'),
                   ('bootstrap', 'failed', '2026-11-01 03:50Z',
                    '2026-11-01 03:50Z', '2026-11-01 04:30Z')
        """)
        )
        await conn.execute(
            text("""
            INSERT INTO job_runs (job_type, status, created_at, started_at, finished_at)
            VALUES ('refresh_forecast', 'failed', '2026-11-01 03:00Z', '2026-11-01 03:00Z',
                    '2026-11-01 03:30Z'),
                   ('refresh_forecast', 'failed', '2026-11-01 04:10Z', '2026-11-01 04:10Z',
                    '2026-11-01 04:20Z'),
                   ('refresh_forecast', 'completed', '2026-11-01 04:35Z', '2026-11-01 04:35Z',
                    '2026-11-01 04:45Z'),
                   ('refresh_forecast', 'failed', '2026-11-01 05:10Z', '2026-11-01 05:10Z',
                    '2026-11-01 05:20Z')
        """)
        )
    summaries = {row.job_type: row for row in await jobs_summary()}
    assert summaries["refresh_forecast"].last_status == "completed"
    assert summaries["refresh_forecast"].batch_size == 1
    assert summaries["refresh_forecast"].failures_in_batch == 0
    assert summaries["bootstrap"].last_status == "failed"


async def test_worker_restart_failure_does_not_reuse_previous_success(database):
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO job_requests (job_name, status, created_at, started_at, finished_at, error)
            VALUES ('refresh_forecast', 'completed', '2026-11-01 02:00Z',
                    '2026-11-01 02:00Z', '2026-11-01 03:00Z', NULL),
                   ('refresh_forecast', 'failed', '2026-11-01 04:00Z',
                    '2026-11-01 04:00Z', '2026-11-01 05:00Z', 'Worker exited before completion'),
                   ('bootstrap', 'running', '2026-11-01 05:00Z', '2026-11-01 05:00Z', NULL, NULL)
        """)
        )
        await conn.execute(
            text("""
            INSERT INTO job_runs (job_type, status, created_at, started_at, finished_at)
            VALUES ('refresh_forecast', 'completed', '2026-11-01 02:00Z', '2026-11-01 02:00Z',
                    '2026-11-01 03:00Z'),
                   ('refresh_forecast', 'failed', '2026-11-01 04:00Z', '2026-11-01 04:00Z',
                    '2026-11-01 05:00Z')
        """)
        )
    summaries = {row.job_type: row for row in await jobs_summary()}
    assert summaries["refresh_forecast"].last_status == "failed"
    assert summaries["refresh_forecast"].batch_size == 1
    assert summaries["refresh_forecast"].failures_in_batch == 1
    assert summaries["bootstrap"].last_status == "running"


@pytest.mark.parametrize("clock_skew_seconds", [-5, 5])
async def test_request_attempt_counts_ignore_worker_clock_skew(database, clock_skew_seconds):
    worker_start = datetime(2026, 11, 1, 4, tzinfo=UTC) + timedelta(seconds=clock_skew_seconds)
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO job_requests (job_name, status, created_at, started_at, finished_at)
            VALUES ('refresh_forecast', 'failed', '2026-11-01 03:59Z',
                    '2026-11-01 04:00Z', '2026-11-01 04:00:01Z')
        """)
        )
        await conn.execute(
            text("""
            INSERT INTO job_runs (job_type, status, created_at, started_at, finished_at)
            VALUES ('refresh_forecast', 'failed', '2026-11-01 04:00:00.5Z',
                    :worker_start, :worker_finish)
        """),
            {"worker_start": worker_start, "worker_finish": worker_start + timedelta(seconds=1)},
        )
    [summary] = await jobs_summary()
    assert summary.last_status == "failed"
    assert summary.batch_size == 1
    assert summary.failures_in_batch == 1


async def test_unrequested_history_reports_only_the_latest_attempt(database):
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO job_runs (job_type, status, started_at, finished_at)
            VALUES ('refresh_forecast', 'failed', '2026-11-01 04:00Z', '2026-11-01 04:15Z'),
                   ('refresh_forecast', 'completed', '2026-11-01 04:15Z', '2026-11-01 04:30Z')
        """)
        )
    [summary] = await jobs_summary()
    assert summary.last_status == "completed"
    assert summary.batch_size == 1
    assert summary.failures_in_batch == 0
