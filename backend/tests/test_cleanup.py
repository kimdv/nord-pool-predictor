from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from nordpool_predictor.scheduler.jobs import job_cleanup


async def test_cleanup_expires_forecast_runs_as_one_unit():
    session = AsyncMock()
    session.execute.return_value.rowcount = 0

    @asynccontextmanager
    async def get_session():
        yield session

    async def run_job(_name, operation):
        await operation()

    with (
        patch("nordpool_predictor.scheduler.jobs.get_session", get_session),
        patch("nordpool_predictor.scheduler.jobs._run_job", run_job),
    ):
        await job_cleanup()

    statements = [str(call.args[0]) for call in session.execute.call_args_list]
    assert any("DELETE FROM forecast_runs WHERE issued_at" in sql for sql in statements)
    assert not any("DELETE FROM forecast_errors" in sql for sql in statements)
    assert not any("DELETE FROM forecast_values" in sql for sql in statements)
    assert any("DELETE FROM weather_forecasts WHERE issued_at" in sql for sql in statements)
    for call in session.execute.call_args_list:
        assert call.args[1]["cutoff"] < datetime.now(UTC) - timedelta(days=1)
    session.commit.assert_awaited_once()


async def test_cleanup_removes_only_expired_forecast_families(database):
    from sqlalchemy import text

    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO model_versions (model_version, model_type, area, horizon_strategy,
                feature_set_version, trained_at, artifact_path)
            VALUES ('test', 'test', 'DK1', 'direct', 'v1', NOW(), '/tmp/test')
        """)
        )
        await conn.execute(
            text("""
            INSERT INTO forecast_runs
                (run_id, area, model_version, issued_at, horizon_steps, status)
            VALUES ('00000000-0000-0000-0000-000000000001', 'DK1', 'test',
                    NOW() - interval '400 days', 1, 'scored'),
                   ('00000000-0000-0000-0000-000000000002', 'DK1', 'test', NOW(), 1, 'completed')
        """)
        )
        for table, columns, values in [
            ("forecast_values", "ts, horizon_step, predicted_price_dkk_kwh", "NOW(), 1, 0.5"),
            (
                "forecast_errors",
                "ts, actual_price_dkk_kwh, predicted_price_dkk_kwh, "
                "abs_error, signed_error, sq_error",
                "NOW(), 0.5, 0.5, 0, 0, 0",
            ),
            ("forecast_run_metrics", "quality_label", "'good'"),
            ("baseline_metrics", "baseline_name", "'test'"),
        ]:
            await conn.execute(
                text(
                    f"INSERT INTO {table} (run_id, {columns}) "
                    f"SELECT run_id, {values} FROM forecast_runs"
                )
            )

    await job_cleanup()

    async with database.connect() as conn:
        for table in (
            "forecast_runs",
            "forecast_values",
            "forecast_errors",
            "forecast_run_metrics",
            "baseline_metrics",
        ):
            assert (await conn.execute(text(f"SELECT COUNT(*) FROM {table}"))).scalar() == 1
        assert (await conn.execute(text("SELECT status FROM job_runs"))).scalar() == "completed"
