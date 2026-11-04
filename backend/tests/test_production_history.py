from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest
import time_machine
from sqlalchemy import text

from nordpool_predictor.api.routers import forecasts
from nordpool_predictor.ingestion import production
from nordpool_predictor.ml import train
from nordpool_predictor.ml.asof import load_asof_covariates
from nordpool_predictor.ml.features import TARGET


@pytest.mark.parametrize("source", ["energidataservice_forecast", "energidataservice_forecast_v2"])
async def test_six_months_of_legacy_aggregates_remain_usable(sync_database, source):
    targets = pd.date_range("2026-01-01 12:00Z", periods=180, freq="D")
    with sync_database.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO production_forecasts
                (area, ts, issued_at, created_at, wind_mw, solar_mw, source)
            VALUES ('DK1', :target, :issued, :received, :wind, 0, :source)
        """),
            [
                {
                    "target": ts.to_pydatetime(),
                    "issued": (ts + timedelta(minutes=5)).to_pydatetime(),
                    "received": (ts - timedelta(days=1)).to_pydatetime(),
                    "wind": float(i),
                    "source": source,
                }
                for i, ts in enumerate(targets)
            ],
        )
    cutoffs = pd.Series(targets - timedelta(hours=6), index=targets)
    frame = load_asof_covariates(
        sync_database, pd.DataFrame(index=targets), "DK1", cutoffs, "production"
    )
    assert frame["wind_mw"].tolist() == list(range(180))
    assert frame["solar_mw"].tolist() == [0] * 180
    assert frame.attrs["production_provenance"] == {"legacy_receipt": 180}
    metrics = train.production_training_metrics(frame)
    assert metrics["coverage"]["wind_mw"]["fraction"] == 1
    assert not metrics["baseline_without_production"]
    with sync_database.connect() as conn:
        assert (
            conn.execute(
                text("SELECT COUNT(*) FROM production_forecasts WHERE issued_at > ts")
            ).scalar_one()
            == 180
        )


async def test_receipt_and_observation_boundaries_are_consistent(sync_database):
    target = datetime(2026, 6, 1, 12, tzinfo=UTC)
    received = target - timedelta(hours=6)
    observed = received + timedelta(hours=1)
    with sync_database.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO production_forecasts
                (area, ts, issued_at, created_at, wind_mw, source, components_json)
            VALUES ('DK1', :target, :target, :received, 10, 'energidataservice_forecast', NULL),
                   ('DK1', :target, :observed, :target, 20, 'energidataservice_forecast_v2', '{}')
        """),
            {"target": target, "received": received, "observed": observed},
        )
        conn.execute(
            text("""
            INSERT INTO production_observations (area, ts, wind_mw, source)
            VALUES ('DK1', :target, 999, 'test')
        """),
            {"target": target},
        )
    for cutoff, expected in [
        (received - timedelta(microseconds=1), None),
        (received, 10),
        (observed, 20),
    ]:
        frame = load_asof_covariates(
            sync_database,
            pd.DataFrame(index=pd.DatetimeIndex([target])),
            "DK1",
            cutoff,
            "production",
        )
        if expected is None:
            assert pd.isna(frame["wind_mw"].iloc[0])
        else:
            assert frame["wind_mw"].iloc[0] == expected


@pytest.mark.parametrize(
    "target,published",
    [
        ("2026-03-29T12:00:00", "2026-03-28T17:00:00+00:00"),
        ("2026-03-30T12:00:00", "2026-03-29T16:00:00+00:00"),
        ("2026-10-25T12:00:00", "2026-10-24T16:00:00+00:00"),
        ("2026-10-26T12:00:00", "2026-10-25T17:00:00+00:00"),
        ("2026-01-01T23:00:00", "2026-01-01T17:00:00+00:00"),
    ],
)
def test_day_ahead_publication_uses_copenhagen_day(target, published):
    rows = production._parse_day_ahead_records(
        [
            {
                "HourUTC": target,
                "PriceArea": "DK1",
                "ForecastType": "Solar",
                "ForecastDayAhead": 0,
                "ForecastCurrent": 999,
                "TimestampUTC": "2026-11-03T12:00:00",
            }
        ]
    )
    assert rows[0]["issued_at"] == datetime.fromisoformat(published)
    assert rows[0]["solar_mw"] == 0
    assert rows[0]["source"] == production.SOURCE_DAY_AHEAD_HISTORY


async def test_monthly_backfill_is_idempotent_and_separate_from_live(
    database, sync_database, test_settings
):
    seen = []

    async def fetch(dataset, params):
        start, end = datetime.fromisoformat(params["start"]), datetime.fromisoformat(params["end"])
        seen.append((start, end))
        assert dataset == "Forecasts_Hour"
        assert params["columns"] == "HourUTC,PriceArea,ForecastType,ForecastDayAhead"
        return [
            {
                "HourUTC": start.isoformat(),
                "PriceArea": area,
                "ForecastType": kind,
                "ForecastDayAhead": value,
                "ForecastCurrent": 999,
            }
            for area in ["DK1", "DK2"]
            for kind, value in [("Onshore Wind", 10), ("Solar", None)]
        ]

    with (
        time_machine.travel("2026-04-02 10:00Z", tick=False),
        patch.object(production, "get_settings", return_value=test_settings),
        patch.object(production, "eds_get", AsyncMock(side_effect=fetch)),
    ):
        await production.backfill_production_forecasts(days=65)
        await production.backfill_production_forecasts(days=65)
    first, second = seen[: len(seen) // 2], seen[len(seen) // 2 :]
    assert first == second
    assert first[0][0] == datetime(2026, 1, 27)
    assert first[-1][1] == datetime(2026, 4, 2)
    assert all(a[1] == b[0] for a, b in zip(first, first[1:]))
    assert all((end - start).days <= 31 for start, end in first)
    async with database.connect() as conn:
        rows = (
            await conn.execute(
                text("""
                    SELECT ts, issued_at, wind_mw, solar_mw, source
                    FROM production_forecasts ORDER BY area, ts
                """)
            )
        ).all()
    assert len(rows) == len(first) * 2
    assert all(row.wind_mw == 10 and row.solar_mw is None for row in rows)
    target = rows[0].ts
    for cutoff, expected in [
        (rows[0].issued_at - timedelta(seconds=1), None),
        (rows[0].issued_at, 10),
    ]:
        frame = load_asof_covariates(
            sync_database,
            pd.DataFrame(index=pd.DatetimeIndex([target])),
            "DK1",
            cutoff,
            "production",
        )
        if expected is None:
            assert pd.isna(frame["wind_mw"].iloc[0])
        else:
            assert frame["wind_mw"].iloc[0] == expected
        assert "_production_basis" not in frame.columns


@pytest.mark.parametrize("days", [0, 366])
async def test_forecast_backfill_rejects_unbounded_history(days):
    with pytest.raises(ValueError, match="between 1 and 365"):
        await production.backfill_production_forecasts(days=days)


async def test_empty_backfill_can_recover_without_rewriting_history(database, test_settings):
    records = [
        {"HourUTC": "2026-04-01T12:00:00", "PriceArea": "DK1", "ForecastType": kind}
        for kind in ["Onshore Wind", "Solar"]
    ]
    responses = [
        [{**r, "ForecastDayAhead": None} for r in records],
        [
            {**r, "ForecastDayAhead": 0 if r["ForecastType"] == "Onshore Wind" else None}
            for r in records
        ],
        [{**r, "ForecastDayAhead": 999} for r in records],
    ]
    with (
        time_machine.travel("2026-04-02 10:00Z", tick=False),
        patch.object(production, "get_settings", return_value=test_settings),
        patch.object(production, "eds_get", AsyncMock(side_effect=responses)),
    ):
        assert await production.backfill_production_forecasts(days=1) == 0
        async with database.connect() as conn:
            assert (
                await conn.execute(text("SELECT COUNT(*) FROM production_forecasts"))
            ).scalar_one() == 0
        await production.backfill_production_forecasts(days=1)
        async with database.connect() as conn:
            original = (await conn.execute(text("SELECT * FROM production_forecasts"))).all()
        await production.backfill_production_forecasts(days=1)
    async with database.connect() as conn:
        final = (await conn.execute(text("SELECT * FROM production_forecasts"))).all()
    assert len(final) == 1
    assert final == original
    assert final[0].wind_mw == 0
    assert final[0].solar_mw is None


def training_frame(wind=np.nan, solar=np.nan):
    frame = pd.DataFrame(
        {TARGET: 0.5, "wind_mw": wind, "solar_mw": solar, "horizon_step": 1},
        index=pd.date_range("2026-01-01", periods=200, freq="15min", tz="UTC"),
    )
    frame.attrs["production_provenance"] = {"legacy_receipt": len(frame)}
    return frame


@pytest.mark.parametrize("wind,solar", [(1, 2), (0, np.nan), (np.nan, 0)])
async def test_partial_production_history_does_not_trigger_backfill(test_settings, wind, solar):
    with (
        patch.object(train, "get_settings", return_value=test_settings),
        patch.object(train, "build_feature_matrix", return_value=training_frame(wind, solar)),
        patch.object(train, "backfill_production_forecasts", AsyncMock()) as backfill,
    ):
        frame = await train.prepare_training_data("DK1")
    backfill.assert_not_awaited()
    assert not train.production_training_metrics(frame)["baseline_without_production"]


@pytest.mark.parametrize("usable", [False, True])
@pytest.mark.parametrize("recovered", [False, True])
async def test_training_preparation_backfills_once_and_preserves_existing(
    test_settings, usable, recovered
):
    first = training_frame()
    second = training_frame(0) if recovered else first
    with (
        patch.object(train, "get_settings", return_value=test_settings),
        patch.object(train, "build_feature_matrix", side_effect=[first, second]) as build,
        patch.object(train, "_has_usable_active_model", AsyncMock(return_value=usable)),
        patch.object(train, "backfill_production_forecasts", AsyncMock()) as backfill,
    ):
        if usable and not recovered:
            with pytest.raises(ValueError, match="active model preserved"):
                await train.prepare_training_data("DK1")
        else:
            frame = await train.prepare_training_data("DK1")
            assert train.production_training_metrics(frame)["baseline_without_production"] == (
                not recovered
            )
    backfill.assert_awaited_once_with(days=365)
    assert build.call_count == 2


@pytest.mark.parametrize("retrieval_fails", [False, True])
async def test_missing_history_cannot_deactivate_active_model(
    database, test_settings, retrieval_fails
):
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO model_versions (model_version, model_type, area, horizon_strategy,
                feature_set_version, trained_at, artifact_path, is_active)
            VALUES ('existing', 'test', 'DK1', 'direct', 'v3-copenhagen', NOW(), '/tmp/test', TRUE)
        """)
        )
    with (
        patch.object(train, "get_settings", return_value=test_settings),
        patch.object(train, "build_feature_matrix", return_value=training_frame()),
        patch("nordpool_predictor.ml.predict._load_models", return_value={"p50": MagicMock()}),
        patch.object(
            train,
            "backfill_production_forecasts",
            AsyncMock(side_effect=RuntimeError("upstream down") if retrieval_fails else None),
        ),
        patch.object(train, "_train_sync") as fit,
    ):
        with pytest.raises((RuntimeError, ValueError), match="active model preserved"):
            await train.train_models("DK1")
    fit.assert_not_called()
    async with database.connect() as conn:
        assert (
            await conn.execute(text("SELECT model_version FROM model_versions WHERE is_active"))
        ).scalars().all() == ["existing"]


async def test_failed_retrieval_keeps_first_install_baseline_explicit(test_settings):
    with (
        patch.object(train, "get_settings", return_value=test_settings),
        patch.object(train, "build_feature_matrix", side_effect=[training_frame()] * 2) as build,
        patch.object(train, "_has_usable_active_model", AsyncMock(return_value=False)),
        patch.object(
            train, "backfill_production_forecasts", AsyncMock(side_effect=RuntimeError("offline"))
        ) as backfill,
    ):
        frame = await train.prepare_training_data("DK1")
    metrics = train.production_training_metrics(frame)
    assert metrics["baseline_without_production"]
    assert metrics["coverage"]["wind_mw"]["non_null_samples"] == 0
    assert metrics["coverage"]["solar_mw"]["fraction"] == 0
    assert build.call_count == 2
    backfill.assert_awaited_once()


def test_sparse_history_reports_values_and_provenance():
    frame = training_frame()
    frame.iloc[0, frame.columns.get_loc("wind_mw")] = 0
    metrics = train.production_training_metrics(frame)
    assert metrics["coverage"]["wind_mw"] == {"non_null_samples": 1, "fraction": 1 / 200}
    assert metrics["coverage"]["solar_mw"]["non_null_samples"] == 0
    assert metrics["provenance"] == {"legacy_receipt": 200}
    assert not metrics["baseline_without_production"]


async def test_backfill_does_not_rewrite_an_existing_runs_reported_horizon(
    database, sync_database, test_settings
):
    cutoff = datetime(2026, 4, 1, 18, tzinfo=UTC)
    live_target = cutoff + timedelta(hours=6)
    backfilled_target = cutoff + timedelta(hours=12)
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO model_versions (model_version, model_type, area, horizon_strategy,
                feature_set_version, trained_at, artifact_path)
            VALUES ('test', 'test', 'DK1', 'direct', 'v2-asof', NOW(), '/tmp/test')
        """)
        )
        await conn.execute(
            text("""
            INSERT INTO forecast_runs (area, model_version, issued_at, horizon_steps, status)
            VALUES ('DK1', 'test', :cutoff, 8, 'completed')
        """),
            {"cutoff": cutoff},
        )
        await conn.execute(
            text("""
            INSERT INTO production_forecasts
                (area, ts, issued_at, created_at, wind_mw, source, components_json)
            VALUES ('DK1', :target, :issued, :issued, 10,
                    'energidataservice_forecast_v2', '{}')
        """),
            {"target": live_target, "issued": cutoff - timedelta(hours=1)},
        )
    with patch.object(forecasts, "get_settings", return_value=test_settings):
        before = await forecasts.get_latest_forecast("DK1")
        async with database.begin() as conn:
            await conn.execute(
                text("""
                INSERT INTO production_forecasts
                    (area, ts, issued_at, created_at, wind_mw, source)
                VALUES ('DK1', :target, :published, :received, 20, :source)
            """),
                {
                    "target": backfilled_target,
                    "published": cutoff - timedelta(hours=2),
                    "received": cutoff + timedelta(hours=1),
                    "source": production.SOURCE_DAY_AHEAD_HISTORY,
                },
            )
        after = await forecasts.get_latest_forecast("DK1")
        assert (
            after.production_forecast_horizon == before.production_forecast_horizon == live_target
        )
        # Reconstructed history remains usable for simulated historical training.
        training = load_asof_covariates(
            sync_database,
            pd.DataFrame(index=pd.DatetimeIndex([backfilled_target])),
            "DK1",
            cutoff,
            "production",
        )
        assert training["wind_mw"].iloc[0] == 20
        async with database.begin() as conn:
            await conn.execute(
                text("""
                INSERT INTO forecast_runs (area, model_version, issued_at, horizon_steps, status)
                VALUES ('DK1', 'test', :cutoff, 8, 'completed')
            """),
                {"cutoff": cutoff + timedelta(hours=1)},
            )
        later = await forecasts.get_latest_forecast("DK1")
    assert later.production_forecast_horizon == backfilled_target


@pytest.mark.parametrize("retrieval_fails", [False, True])
async def test_retraining_shares_one_backfill_attempt_per_job(test_settings, retrieval_fails):
    from nordpool_predictor.scheduler import jobs

    seen = []

    async def prepare_only(area, **kwargs):
        seen.append(area)
        await train.prepare_training_data(area, **kwargs)

    async def run_job(job_type, operation, area):
        await operation(area)

    with (
        patch.object(jobs, "get_settings", return_value=test_settings),
        patch.object(jobs, "_run_job", side_effect=run_job),
        patch.object(train, "get_settings", return_value=test_settings),
        patch.object(train, "train_models", side_effect=prepare_only),
        patch.object(
            train, "build_feature_matrix", side_effect=lambda *args: training_frame()
        ) as build,
        patch.object(train, "_has_usable_active_model", AsyncMock(return_value=True)),
        patch.object(
            train,
            "backfill_production_forecasts",
            AsyncMock(side_effect=RuntimeError("offline") if retrieval_fails else None),
        ) as backfill,
    ):
        for attempt in [1, 2]:
            with pytest.raises(RuntimeError, match="DK1, DK2"):
                await jobs.job_retrain_model()
            assert backfill.await_count == attempt
        assert seen == ["DK1", "DK2", "DK1", "DK2"]
        assert build.call_count == (4 if retrieval_fails else 8)


async def test_shared_backfill_can_recover_one_area_without_refetching_for_the_other(
    test_settings,
):
    recovered = False

    async def retrieve(**kwargs):
        nonlocal recovered
        recovered = True

    def build(area, *args):
        return training_frame(0 if recovered and area == "DK1" else np.nan)

    shared = train.ProductionHistoryBackfill()
    with (
        patch.object(train, "get_settings", return_value=test_settings),
        patch.object(train, "build_feature_matrix", side_effect=build),
        patch.object(train, "_has_usable_active_model", AsyncMock(return_value=True)),
        patch.object(train, "backfill_production_forecasts", side_effect=retrieve) as backfill,
    ):
        first = await train.prepare_training_data("DK1", production_backfill=shared)
        assert first["wind_mw"].tolist() == [0] * 200
        with pytest.raises(ValueError, match="active model preserved"):
            await train.prepare_training_data("DK2", production_backfill=shared)
    backfill.assert_awaited_once_with(days=365)
