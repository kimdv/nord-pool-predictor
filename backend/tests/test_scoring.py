from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from sqlalchemy import text

from nordpool_predictor.ml.score import score_baselines, score_forecasts
from nordpool_predictor.ml.train import evaluate_holdout, split_holdout


def test_holdout_purges_labels_at_or_after_earliest_simulated_issuance():
    index = pd.date_range("2026-01-01", periods=90 * 96, freq="15min", tz=UTC)
    features = pd.DataFrame({"horizon_step": 1}, index=index)
    split = int(len(features) * 0.8)
    # An issuance later in the holdout can be earlier than the first row's issuance.
    features.iloc[split + 10, 0] = 672
    labels = pd.Series(range(len(index)), index=index)
    development, development_labels, holdout, holdout_labels = split_holdout(features, labels)
    earliest = index[split + 10] - timedelta(days=7)
    assert development.index.max() == earliest - timedelta(minutes=15)
    assert earliest not in development.index
    pd.testing.assert_series_equal(development_labels, labels.loc[development.index])
    pd.testing.assert_frame_equal(holdout, features.iloc[split:])
    pd.testing.assert_series_equal(holdout_labels, labels.iloc[split:])


def test_holdout_rejects_too_little_development_history_after_purging():
    index = pd.date_range("2026-01-01", periods=100, freq="15min", tz=UTC)
    with pytest.raises(ValueError, match="Insufficient development samples"):
        split_holdout(pd.DataFrame({"horizon_step": 672}, index=index), pd.Series(1, index=index))


def test_evaluation_fits_only_development_rows_and_predicts_holdout():
    development = pd.DataFrame({"feature": [1, 2, 3]})
    holdout = pd.DataFrame({"feature": [4, 5]})
    estimator = MagicMock()
    estimator.predict.return_value = [0, 0]
    with patch("nordpool_predictor.ml.train.lgb.LGBMRegressor", return_value=estimator):
        metrics = evaluate_holdout(
            {}, development, pd.Series([1, 2, 3]), holdout, pd.Series([2, 4])
        )
    assert estimator.fit.call_args.args[0] is development
    assert estimator.predict.call_args.args[0] is holdout
    assert metrics["mae"] == 3


async def test_partial_forecast_is_rescored_until_all_horizons_are_complete(database):
    issued = datetime(2026, 1, 1, tzinfo=UTC)
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO model_versions (model_version, model_type, area, horizon_strategy,
                feature_set_version, trained_at, artifact_path)
            VALUES ('test', 'test', 'DK1', 'direct', 'v2-asof', NOW(), '/tmp/test')
        """)
        )
        run_id = (
            await conn.execute(
                text("""
            INSERT INTO forecast_runs (area, model_version, issued_at, horizon_steps, status)
            VALUES ('DK1', 'test', :issued, 672, 'completed') RETURNING run_id
        """),
                {"issued": issued},
            )
        ).scalar_one()
        await conn.execute(
            text("""
            INSERT INTO forecast_values (run_id, ts, horizon_step, predicted_price_dkk_kwh)
            VALUES (:id, :ts, :step, 0.5)
        """),
            [
                {"id": run_id, "ts": issued + timedelta(minutes=15 * step), "step": step}
                for step in range(1, 673)
            ],
        )
        await conn.execute(
            text("""
            INSERT INTO price_observations (area, ts, price_dkk_kwh, source)
            VALUES ('DK1', :ts, 0.6, 'test')
        """),
            [{"ts": issued + timedelta(minutes=15 * step)} for step in range(1, 337)],
        )
    await score_forecasts()
    async with database.connect() as conn:
        row = (await conn.execute(text("SELECT * FROM forecast_run_metrics"))).mappings().one()
        assert row["coverage_168h"] == 0.5
        assert row["mae_168h"] is None
        assert row["mae_24h"] == pytest.approx(0.1)
        assert (
            await conn.execute(text("SELECT status FROM forecast_runs"))
        ).scalar() == "completed"
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO price_observations (area, ts, price_dkk_kwh, source)
            VALUES ('DK1', :ts, 0.7, 'test')
        """),
            [{"ts": issued + timedelta(minutes=15 * step)} for step in range(337, 673)],
        )
    await score_forecasts()
    await score_forecasts()
    async with database.connect() as conn:
        row = (await conn.execute(text("SELECT * FROM forecast_run_metrics"))).mappings().one()
        assert row["coverage_168h"] == 1
        assert row["mae_168h"] == pytest.approx(0.15)
        assert (await conn.execute(text("SELECT status FROM forecast_runs"))).scalar() == "scored"
        assert (await conn.execute(text("SELECT COUNT(*) FROM forecast_errors"))).scalar() == 672


async def _seed_run(conn, issued, *, status="completed", steps=96):
    await conn.execute(
        text("""
        INSERT INTO model_versions (model_version, model_type, area, horizon_strategy,
            feature_set_version, trained_at, artifact_path)
        VALUES ('test', 'test', 'DK1', 'direct', 'v2-asof', NOW(), '/tmp/test')
        ON CONFLICT DO NOTHING
    """)
    )
    run_id = (
        await conn.execute(
            text("""
        INSERT INTO forecast_runs (area, model_version, issued_at, horizon_steps, status)
        VALUES ('DK1', 'test', :issued, :steps, :status) RETURNING run_id
    """),
            {"issued": issued, "steps": steps, "status": status},
        )
    ).scalar_one()
    await conn.execute(
        text("""
        INSERT INTO forecast_values (run_id, ts, horizon_step, predicted_price_dkk_kwh)
        VALUES (:id, :ts, :step, 0.5)
    """),
        [
            {"id": run_id, "ts": issued + timedelta(minutes=15 * step), "step": step}
            for step in range(1, steps + 1)
        ],
    )
    return run_id


async def test_short_run_cannot_claim_complete_named_horizons(database):
    issued = datetime(2026, 1, 1, tzinfo=UTC)
    async with database.begin() as conn:
        await _seed_run(conn, issued, steps=2)
        await conn.execute(
            text("""
            INSERT INTO price_observations (area, ts, price_dkk_kwh, source)
            SELECT 'DK1', ts, 0.6, 'test' FROM forecast_values
        """)
        )
    await score_forecasts()
    async with database.connect() as conn:
        row = (await conn.execute(text("SELECT * FROM forecast_run_metrics"))).mappings().one()
        for label, steps in [("24h", 96), ("72h", 288), ("168h", 672)]:
            assert row[f"coverage_{label}"] == pytest.approx(2 / steps)
            assert row[f"mae_{label}"] is None
            assert row[f"rmse_{label}"] is None
            assert row[f"bias_{label}"] is None
        assert row["quality_label"] is None
        assert (await conn.execute(text("SELECT status FROM forecast_runs"))).scalar() == "scored"
        assert (await conn.execute(text("SELECT COUNT(*) FROM baseline_metrics"))).scalar() == 0


async def test_migration_invalidates_and_rescores_partial_and_full_legacy_runs(database):
    from nordpool_predictor.database import run_migrations

    issued = datetime(2026, 1, 1, tzinfo=UTC)
    async with database.begin() as conn:
        full = await _seed_run(conn, issued, status="scored")
        partial = await _seed_run(conn, issued + timedelta(days=2), status="scored")
        for run_id, actual_count in [(full, 96), (partial, 48)]:
            await conn.execute(
                text("""
                INSERT INTO forecast_errors (run_id, ts, actual_price_dkk_kwh,
                    predicted_price_dkk_kwh, abs_error, signed_error, sq_error)
                SELECT run_id, ts, 0.6, 0.5, 0.1, -0.1, 0.01 FROM forecast_values
                WHERE run_id = :id AND horizon_step <= :count
            """),
                {"id": run_id, "count": actual_count},
            )
            await conn.execute(
                text("""
                INSERT INTO price_observations (area, ts, price_dkk_kwh, source)
                SELECT 'DK1', ts, 0.6, 'test' FROM forecast_values
                WHERE run_id = :id AND horizon_step <= :count
            """),
                {"id": run_id, "count": actual_count},
            )
            await conn.execute(
                text("""
                INSERT INTO forecast_run_metrics (run_id, mae_24h) VALUES (:id, 9)
            """),
                {"id": run_id},
            )
            await conn.execute(
                text("""
                INSERT INTO baseline_metrics (run_id, baseline_name, mae)
                VALUES (:id, 'yesterday_same_slot', 9)
            """),
                {"id": run_id},
            )
        # Restore the schema to its pre-005 shape and apply the real migration.
        await conn.execute(
            text("""
            ALTER TABLE forecast_run_metrics DROP COLUMN coverage_24h,
                DROP COLUMN coverage_72h, DROP COLUMN coverage_168h
        """)
        )
        await conn.execute(
            text("DELETE FROM _migrations_applied WHERE filename = '005_incremental_scoring.sql'")
        )
    await run_migrations()
    async with database.connect() as conn:
        assert set((await conn.execute(text("SELECT status FROM forecast_runs"))).scalars()) == {
            "completed"
        }
        for table in ["forecast_run_metrics", "baseline_metrics"]:
            assert (await conn.execute(text(f"SELECT COUNT(*) FROM {table}"))).scalar() == 0
    await score_forecasts()
    async with database.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text("""
            SELECT fr.run_id, fr.status, fm.coverage_24h, fm.mae_24h
            FROM forecast_runs fr JOIN forecast_run_metrics fm USING (run_id)
        """)
                )
            )
            .mappings()
            .all()
        )
    results = {row["run_id"]: row for row in rows}
    assert results[full]["status"] == "scored"
    assert results[full]["coverage_24h"] == 1
    assert results[full]["mae_24h"] == pytest.approx(0.1)
    assert results[partial]["status"] == "completed"
    assert results[partial]["coverage_24h"] == 0.5
    assert results[partial]["mae_24h"] is None


@pytest.mark.parametrize("history", ["none", "incomplete", "complete"])
async def test_baseline_rescoring_replaces_stale_rows(database, history):
    issued = datetime(2026, 1, 1, tzinfo=UTC)
    targets = pd.date_range(issued + timedelta(minutes=15), periods=96, freq="15min")
    async with database.begin() as conn:
        run_id = await _seed_run(conn, issued)
        await conn.execute(
            text("""
            INSERT INTO baseline_metrics (run_id, baseline_name, mae)
            VALUES (:id, 'yesterday_same_slot', 9), (:id, 'last_week_same_slot', 9)
        """),
            {"id": run_id},
        )
        if history != "none":
            selected = targets if history == "complete" else targets[:48]
            await conn.execute(
                text("""
                INSERT INTO price_observations (area, ts, price_dkk_kwh, source)
                VALUES ('DK1', :ts, 0.4, 'test')
            """),
                [{"ts": ts.to_pydatetime() - timedelta(days=1)} for ts in selected],
            )
    await score_baselines(
        str(run_id),
        "DK1",
        pd.DataFrame({"ts": targets, "horizon_step": range(1, 97)}),
        pd.DataFrame({"ts": targets, "actual": 0.6}),
        issued,
    )
    async with database.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT baseline_name, mae FROM baseline_metrics WHERE run_id = :id"),
                {"id": run_id},
            )
        ).all()
    if history == "complete":
        assert len(rows) == 1
        assert rows[0][0] == "yesterday_same_slot"
        assert rows[0][1] == pytest.approx(0.2)
    else:
        assert rows == []


async def test_benchmarks_skip_newer_run_without_complete_coverage(database, test_settings):
    from nordpool_predictor.api.routers import forecasts

    issued = datetime(2026, 1, 1, tzinfo=UTC)
    async with database.begin() as conn:
        complete = await _seed_run(conn, issued)
        legacy = await _seed_run(conn, issued + timedelta(days=1))
        partial = await _seed_run(conn, issued + timedelta(days=2))
        await conn.execute(
            text("""
            INSERT INTO forecast_run_metrics (run_id, mae_24h, coverage_24h)
            VALUES (:complete, 0.1, 1), (:legacy, 0.2, NULL), (:partial, 0.3, 0.5)
        """),
            {"complete": complete, "legacy": legacy, "partial": partial},
        )
    with patch.object(forecasts, "get_settings", return_value=test_settings):
        result = await forecasts.get_benchmarks("DK1")
    assert result.run_id == str(complete)
    assert result.ml_mae == pytest.approx(0.1)
