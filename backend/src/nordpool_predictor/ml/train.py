from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import joblib
import lightgbm as lgb
import numpy as np
import optuna
import pandas as pd
from sklearn.model_selection import TimeSeriesSplit
from sqlalchemy import text

from nordpool_predictor.config import get_settings
from nordpool_predictor.database import get_session
from nordpool_predictor.ingestion.production import backfill_production_forecasts
from nordpool_predictor.ml.artifacts import write_manifest
from nordpool_predictor.ml.features import (
    HORIZON_STEP_FEATURE,
    TARGET,
    build_feature_matrix,
)

logger = logging.getLogger(__name__)

N_OPTUNA_TRIALS = 50
N_CV_SPLITS = 5
EARLY_STOPPING_ROUNDS = 50
QUANTILES: dict[str, float] = {"p10": 0.1, "p50": 0.5, "p90": 0.9}


class ProductionHistoryBackfill:
    """Share one historical retrieval across the sequential areas in a job."""

    def __init__(self) -> None:
        self._attempted = False
        self._error: Exception | None = None

    async def run(self) -> None:
        if not self._attempted:
            self._attempted = True
            try:
                await backfill_production_forecasts(days=365)
            except Exception as exc:
                self._error = exc
        if self._error is not None:
            raise self._error


# ---------------------------------------------------------------------------
# Optuna objective
# ---------------------------------------------------------------------------


def _objective(
    trial: optuna.Trial,
    X: pd.DataFrame,
    y: pd.Series,
    tscv: TimeSeriesSplit,
) -> float:
    """Single Optuna trial: 5-fold time-series CV with quantile LightGBM."""
    params: dict = {
        "objective": "quantile",
        "alpha": 0.5,
        "verbosity": -1,
        "n_jobs": 1,
        "num_leaves": trial.suggest_int("num_leaves", 20, 200),
        "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.3, log=True),
        "n_estimators": trial.suggest_int("n_estimators", 100, 1000),
        "min_child_samples": trial.suggest_int("min_child_samples", 5, 100),
        "subsample": trial.suggest_float("subsample", 0.5, 1.0),
        "colsample_bytree": trial.suggest_float("colsample_bytree", 0.5, 1.0),
        "reg_alpha": trial.suggest_float("reg_alpha", 1e-8, 10.0, log=True),
        "reg_lambda": trial.suggest_float("reg_lambda", 1e-8, 10.0, log=True),
    }

    fold_maes: list[float] = []
    for fold, (train_idx, val_idx) in enumerate(tscv.split(X), start=1):
        X_val = X.iloc[val_idx]
        issuances = X_val.index - pd.to_timedelta(
            X_val[HORIZON_STEP_FEATURE].to_numpy() * 15, unit="min"
        )
        train_idx = train_idx[X.index[train_idx] < issuances.min()]
        if len(train_idx) < 100:
            raise ValueError(
                f"Insufficient training samples in tuning fold {fold} after purging "
                f"validation issuance overlap: {len(train_idx)}"
            )
        X_tr = X.iloc[train_idx]
        y_tr, y_val = y.iloc[train_idx], y.iloc[val_idx]

        model = lgb.LGBMRegressor(**params)
        model.fit(
            X_tr,
            y_tr,
            eval_set=[(X_val, y_val)],
            callbacks=[lgb.early_stopping(EARLY_STOPPING_ROUNDS, verbose=False)],
        )
        preds = model.predict(X_val)
        fold_maes.append(float(np.mean(np.abs(y_val - preds))))

    return float(np.mean(fold_maes))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _train_sync(
    area: str,
    settings_training_window_days: int,
    model_dir_str: str,
    training_data: pd.DataFrame | None = None,
) -> tuple[
    str,
    str,
    dict,
    dict[str, lgb.LGBMRegressor],
    datetime,
]:
    """CPU-bound training work; runs in a thread to avoid blocking the event loop."""
    now = datetime.now(UTC)
    start = now - timedelta(days=settings_training_window_days)

    logger.info("Building feature matrix for %s [%s → %s]", area, start, now)
    df = training_data if training_data is not None else build_feature_matrix(area, start, now)

    if df.empty:
        raise ValueError(f"No training data available for area {area}")

    df = df.dropna(subset=[TARGET])
    if len(df) < 100:
        raise ValueError(f"Insufficient training samples for {area}: {len(df)}")

    y = df[TARGET]
    X = df.drop(columns=[TARGET])
    logger.info("Training set: %d samples × %d features for %s", len(X), X.shape[1], area)

    # Reserve the newest 20% before tuning; the final production model is refit later.
    X_development, y_development, X_holdout, y_holdout = split_holdout(X, y)
    tscv = TimeSeriesSplit(n_splits=N_CV_SPLITS)

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    study = optuna.create_study(direction="minimize")

    def _optuna_callback(study: optuna.Study, trial: optuna.trial.FrozenTrial) -> None:
        if trial.number % 10 == 0 or trial.number == N_OPTUNA_TRIALS - 1:
            try:
                best = study.best_value
            except ValueError:
                best = float("nan")
            logger.info(
                "Optuna %s: trial %d/%d — best MAE so far: %.4f",
                area,
                trial.number + 1,
                N_OPTUNA_TRIALS,
                best,
            )

    study.optimize(
        lambda trial: _objective(trial, X_development, y_development, tscv),
        n_trials=N_OPTUNA_TRIALS,
        callbacks=[_optuna_callback],
    )

    best_params = study.best_params
    logger.info(
        "Optuna best for %s: MAE=%.4f params=%s",
        area,
        study.best_value,
        best_params,
    )

    time_str = f"{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex}"
    model_version = f"lgbm-{area}-{time_str}"
    model_dir = Path(model_dir_str)
    model_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Training final quantile models (p10/p50/p90) for %s", area)
    trained_models: dict[str, lgb.LGBMRegressor] = {}
    artifact_paths: dict[str, str] = {}
    for q_name, q_val in QUANTILES.items():
        final_params: dict = {
            "objective": "quantile",
            "alpha": q_val,
            "verbosity": -1,
            "n_jobs": -1,
            **best_params,
        }
        model = lgb.LGBMRegressor(**final_params)
        model.fit(X, y)

        artifact = model_dir / f"lgbm-{area}-{q_name}-{time_str}.joblib"
        temporary = artifact.with_suffix(".joblib.tmp")
        joblib.dump(model, temporary)
        temporary.replace(artifact)
        artifact_paths[q_name] = artifact.name
        trained_models[q_name] = model
        logger.info("Saved %s → %s", q_name, artifact)

    evaluation = evaluate_holdout(best_params, X_development, y_development, X_holdout, y_holdout)
    mae, rmse, bias = evaluation["mae"], evaluation["rmse"], evaluation["bias"]

    metrics = {
        "production_data": production_training_metrics(df),
        "mae": mae,
        "rmse": rmse,
        "bias": bias,
        "best_optuna_value": float(study.best_value),
        "n_trials": len(study.trials),
        "n_samples": len(X),
        "n_features": X.shape[1],
        "feature_names": list(X.columns),
        "evaluation": "chronological_holdout",
        "n_development_samples": len(X_development),
        "n_holdout_samples": len(X_holdout),
        "holdout_start": str(X_holdout.index.min()),
        "development_end": str(X_development.index.max()),
        "n_purged_samples": len(X) - len(X_development) - len(X_holdout),
    }

    manifest = write_manifest(
        model_dir, model_version, area, artifact_paths, trained_models["p50"]
    )
    return model_version, manifest, metrics, trained_models, now


async def train_models(
    area: str, *, production_backfill: ProductionHistoryBackfill | None = None
) -> str:
    """Train p10 / p50 / p90 quantile LightGBM models for *area*.

    Returns the ``model_version`` string (e.g. ``lgbm-DK1-20260406-134500-<id>``).
    """
    import asyncio

    settings = get_settings()
    logger.info(
        "Starting training for %s (window=%d days)",
        area,
        settings.training_window_days,
    )

    training_data = await prepare_training_data(area, production_backfill=production_backfill)
    model_version, artifact_path, metrics, _models, now = await asyncio.to_thread(
        _train_sync,
        area,
        settings.training_window_days,
        settings.model_dir,
        training_data,
    )

    async def register_model(connection) -> None:
        await connection.execute(
            text(
                "UPDATE model_versions SET is_active = FALSE "
                "WHERE area = :area AND is_active = TRUE"
            ),
            {"area": area},
        )
        await connection.execute(
            text(
                "INSERT INTO model_versions "
                "  (model_version, model_type, area,"
                "   horizon_strategy, feature_set_version,"
                "   trained_at, metrics_json,"
                "   artifact_path, is_active) "
                "VALUES "
                "  (:model_version, :model_type, :area,"
                "   :horizon_strategy, :feature_set_version,"
                "   :trained_at, CAST(:metrics_json AS jsonb),"
                "   :artifact_path, TRUE)"
            ),
            {
                "model_version": model_version,
                "model_type": "lgbm_quantile",
                "area": area,
                "horizon_strategy": "direct",
                "feature_set_version": "v4-crossborder-history",
                "trained_at": now,
                "metrics_json": json.dumps(metrics),
                "artifact_path": artifact_path,
            },
        )

    try:
        from nordpool_predictor.worker import run_with_worker_lock

        if not await run_with_worker_lock(register_model):
            async with get_session() as session:
                await register_model(session)
                await session.commit()
    except Exception:
        logger.exception("Failed to register model %s for %s", model_version, area)
        raise

    logger.info(
        "Registered %s — MAE=%.4f  RMSE=%.4f  bias=%.4f",
        model_version,
        metrics["mae"],
        metrics["rmse"],
        metrics["bias"],
    )
    return model_version


def production_training_metrics(frame: pd.DataFrame) -> dict:
    coverage = {}
    for column in ("wind_mw", "solar_mw"):
        present = int(frame[column].notna().sum()) if column in frame else 0
        coverage[column] = {
            "non_null_samples": present,
            "fraction": present / len(frame) if len(frame) else 0.0,
        }
    return {
        "coverage": coverage,
        "provenance": frame.attrs.get("production_provenance", {}),
        "baseline_without_production": not any(v["non_null_samples"] for v in coverage.values()),
    }


async def _has_usable_active_model(area: str) -> bool:
    from nordpool_predictor.ml.predict import ModelUnavailableError, select_usable_model

    async with get_session() as session:
        candidates = (
            (
                await session.execute(
                    text("""
            SELECT model_version, artifact_path FROM model_versions
            WHERE area = :area AND is_active = TRUE
              AND feature_set_version = 'v4-crossborder-history'
            ORDER BY trained_at DESC, model_version DESC
        """),
                    {"area": area},
                )
            )
            .mappings()
            .all()
        )
    try:
        await select_usable_model(area, candidates)
    except ModelUnavailableError:
        return False
    return True


async def prepare_training_data(
    area: str, *, production_backfill: ProductionHistoryBackfill | None = None
) -> pd.DataFrame:
    """Recover missing production history once, before tuning or writing artifacts."""
    import asyncio

    settings = get_settings()

    async def build() -> pd.DataFrame:
        now = datetime.now(UTC)
        frame = await asyncio.to_thread(
            build_feature_matrix, area, now - timedelta(days=settings.training_window_days), now
        )
        if frame.empty or TARGET not in frame:
            raise ValueError(f"No training data available for area {area}")
        frame = frame.dropna(subset=[TARGET])
        if len(frame) < 100:
            raise ValueError(f"Insufficient training samples for {area}: {len(frame)}")
        return frame

    frame = await build()
    metrics = production_training_metrics(frame)
    if metrics["baseline_without_production"]:
        preserve_existing = await _has_usable_active_model(area)
        try:
            await (production_backfill or ProductionHistoryBackfill()).run()
        except Exception as exc:
            if preserve_existing:
                raise RuntimeError(
                    "Production history backfill failed; active model preserved"
                ) from exc
            logger.exception("Production backfill failed; checking first-install baseline inputs")
        frame = await build()
        metrics = production_training_metrics(frame)
        if metrics["baseline_without_production"]:
            if preserve_existing:
                raise ValueError("No usable wind/solar training history; active model preserved")
            logger.warning(
                "Training first-install baseline for %s without production inputs", area
            )
    logger.info("Production training data for %s: %s", area, metrics)
    return frame


def split_holdout(X: pd.DataFrame, y: pd.Series):
    """Reserve the newest targets and purge labels unavailable at their issuances."""
    split = int(len(X) * 0.8)
    X_holdout, y_holdout = X.iloc[split:], y.iloc[split:]
    issuances = X_holdout.index - pd.to_timedelta(
        X_holdout[HORIZON_STEP_FEATURE].to_numpy() * 15, unit="min"
    )
    development = X.iloc[:split]
    development = development.loc[development.index < issuances.min()]
    if len(development) < 100:
        raise ValueError("Insufficient development samples after purging holdout issuance overlap")
    return development, y.loc[development.index], X_holdout, y_holdout


def evaluate_holdout(
    params, X_development, y_development, X_holdout, y_holdout
) -> dict[str, float]:
    model = lgb.LGBMRegressor(objective="quantile", alpha=0.5, verbosity=-1, n_jobs=1, **params)
    model.fit(X_development, y_development)
    errors = np.asarray(model.predict(X_holdout)) - np.asarray(y_holdout)
    return {
        "mae": float(np.mean(np.abs(errors))),
        "rmse": float(np.sqrt(np.mean(errors**2))),
        "bias": float(np.mean(errors)),
    }
