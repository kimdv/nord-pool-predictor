from __future__ import annotations

import uuid
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import lightgbm as lgb
import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from nordpool_predictor import database as db
from nordpool_predictor.ml import artifacts, predict, train
from nordpool_predictor.ml.features import TARGET
from nordpool_predictor.ml.horizon import DEFAULT_HORIZON_STEPS
from nordpool_predictor.scheduler import bootstrap


async def test_upgrade_preserves_legacy_data_and_rejects_v1_models(database):
    schema = f"legacy_{uuid.uuid4().hex}"
    async with database.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        database.url, connect_args={"server_settings": {"search_path": schema}}
    )
    try:
        migrations = Path(__file__).resolve().parents[1] / "migrations"
        async with engine.connect() as conn:
            raw = (await conn.get_raw_connection()).driver_connection
            async with raw.transaction():
                await raw.execute(
                    "CREATE TABLE _migrations_applied "
                    "(filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT NOW())"
                )
                for migration in sorted(migrations.glob("*.sql"))[:3]:
                    await raw.execute(migration.read_text())
                    await raw.execute(
                        "INSERT INTO _migrations_applied (filename) VALUES ($1)", migration.name
                    )
                await raw.execute("""
                    INSERT INTO model_versions (model_version, model_type, area,
                        horizon_strategy, feature_set_version, trained_at,
                        artifact_path, is_active)
                    VALUES ('old', 'test', 'DK1', 'direct', 'v1', NOW(), '/missing', TRUE);
                    INSERT INTO price_observations (area, ts, price_dkk_kwh, source)
                    VALUES ('DK1', '2026-06-01 12:00Z', 0, 'legacy');
                    INSERT INTO production_forecasts
                        (area, issued_at, ts, created_at, wind_mw, solar_mw, source)
                    VALUES ('DK1', '2026-06-01 06:00Z', '2026-06-01 12:00Z',
                        '2026-06-01 07:00Z', 100, 0, 'energidataservice_forecast');
                """)
        with patch.object(db, "get_engine", return_value=engine):
            await db.run_migrations()
            await db.run_migrations()  # A second startup must be idempotent.
            assert await bootstrap.ready_models() == {}
        async with engine.connect() as conn:
            assert (
                await conn.execute(text("SELECT is_active FROM model_versions"))
            ).scalar() is False
            assert (
                await conn.execute(text("SELECT COUNT(*) FROM price_observations"))
            ).scalar() == 1
            row = (
                (await conn.execute(text("SELECT * FROM production_feature_snapshots")))
                .mappings()
                .one()
            )
            assert row["wind_mw"] == 100
            assert row["availability_basis"] == "legacy_receipt"
            assert row["available_at"] == row["created_at"]
    finally:
        await engine.dispose()
        async with database.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


@pytest.mark.parametrize("weather_failure", [None, "DK2"])
async def test_bootstrap_trains_registers_and_persists_forecasts(
    database, test_settings, tmp_path, weather_failure
):
    """Real LightGBM artifacts and DB writes; synthetic inputs replace remote services."""
    test_settings.model_dir = str(tmp_path)
    index = pd.date_range(datetime.now(UTC) - timedelta(days=40), periods=3840, freq="15min")
    signal = np.sin(np.arange(len(index)) / 96)
    frame = pd.DataFrame(
        {
            TARGET: 0.5 + signal / 10,
            "signal": signal,
            "horizon_step": 672,
            "wind_mw": 100.0,
            "solar_mw": 0.0,
        },
        index=index,
    )
    estimator = lgb.LGBMRegressor

    def small_model(**kwargs):
        return estimator(**{**kwargs, "n_estimators": 5, "n_jobs": 1})

    def forecast_features(area, issued_at, steps):
        targets = pd.date_range(
            pd.Timestamp(issued_at).floor("15min") + timedelta(minutes=15),
            periods=steps,
            freq="15min",
        )
        features = pd.DataFrame(
            {
                "signal": 0.0,
                "horizon_step": np.arange(1, steps + 1),
                "wind_mw": 100.0,
                "solar_mw": 0.0,
            },
            index=targets,
        )
        return features, targets

    async def weather(*, area):
        if area == weather_failure:
            raise RuntimeError("weather unavailable")

    artifacts._load_quantile.cache_clear()
    try:
        with ExitStack() as stack:
            for module in (bootstrap, train, artifacts):
                stack.enter_context(
                    patch.object(module, "get_settings", return_value=test_settings)
                )
            stack.enter_context(
                patch.object(train, "build_feature_matrix", side_effect=lambda *a: frame.copy())
            )
            stack.enter_context(patch.object(train, "N_OPTUNA_TRIALS", 1))
            stack.enter_context(patch.object(train, "N_CV_SPLITS", 2))
            stack.enter_context(patch.object(train.lgb, "LGBMRegressor", side_effect=small_model))
            stack.enter_context(
                patch.object(predict, "_build_forecast_features", side_effect=forecast_features)
            )
            for name in (
                "backfill_prices",
                "backfill_production",
                "backfill_crossborder",
                "ingest_production_forecasts",
                "ingest_production_actuals",
                "ingest_crossborder_flows",
                "verify_price_history",
            ):
                stack.enter_context(patch.object(bootstrap, name, AsyncMock()))
            stack.enter_context(
                patch.object(bootstrap, "ingest_weather_forecasts", side_effect=weather)
            )
            if weather_failure:
                with pytest.raises(RuntimeError, match="DK2 weather forecasts"):
                    await bootstrap.bootstrap()
            else:
                await bootstrap.bootstrap()
        async with database.connect() as conn:
            models = (
                (await conn.execute(text("SELECT * FROM model_versions WHERE is_active")))
                .mappings()
                .all()
            )
            assert {m["area"] for m in models} == {"DK1", "DK2"}
            for model in models:
                assert model["feature_set_version"] == "v4-crossborder-history"
                assert (tmp_path / model["artifact_path"]).is_file()
                assert (
                    model["metrics_json"]["production_data"]["coverage"]["wind_mw"]["fraction"]
                    == 1
                )
            runs = (
                (
                    await conn.execute(
                        text("""
                SELECT area, status, COUNT(*) AS slots FROM forecast_runs
                JOIN forecast_values USING (run_id) GROUP BY area, status
            """)
                    )
                )
                .mappings()
                .all()
            )
            assert {r["area"] for r in runs} == ({"DK1"} if weather_failure else {"DK1", "DK2"})
            assert all(
                r["status"] == "completed" and r["slots"] == DEFAULT_HORIZON_STEPS for r in runs
            )
    finally:
        artifacts._load_quantile.cache_clear()
