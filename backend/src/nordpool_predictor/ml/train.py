from __future__ import annotations

import json
import logging
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
from nordpool_predictor.ml.features import TARGET, build_feature_matrix

logger = logging.getLogger(__name__)

N_OPTUNA_TRIALS = 50
N_CV_SPLITS = 5
EARLY_STOPPING_ROUNDS = 50
QUANTILES: dict[str, float] = {"p10": 0.1, "p50": 0.5, "p90": 0.9}


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
    for train_idx, val_idx in tscv.split(X):
        X_tr, X_val = X.iloc[train_idx], X.iloc[val_idx]
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
    area: str, settings_training_window_days: int, model_dir_str: str
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
    df = build_feature_matrix(area, start, now)

    if df.empty:
        raise ValueError(f"No training data available for area {area}")

    df = df.dropna(subset=[TARGET])
    if len(df) < 100:
        raise ValueError(f"Insufficient training samples for {area}: {len(df)}")

    y = df[TARGET]
    X = df.drop(columns=[TARGET])
    logger.info("Training set: %d samples × %d features for %s", len(X), X.shape[1], area)

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
        lambda trial: _objective(trial, X, y, tscv),
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

    time_str = now.strftime("%Y%m%d-%H%M%S")
    model_version = f"lgbm-{area}-{time_str}"
    model_dir = Path(model_dir_str)
    model_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Training final quantile models (p10/p50/p90) for %s", area)
    trained_models: dict[str, lgb.LGBMRegressor] = {}
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
        joblib.dump(model, artifact)
        trained_models[q_name] = model
        logger.info("Saved %s → %s", q_name, artifact)

    folds = list(tscv.split(X))
    _, last_val_idx = folds[-1]
    X_val, y_val = X.iloc[last_val_idx], y.iloc[last_val_idx]
    val_preds = trained_models["p50"].predict(X_val)

    mae = float(np.mean(np.abs(y_val - val_preds)))
    rmse = float(np.sqrt(np.mean((y_val - val_preds) ** 2)))
    bias = float(np.mean(val_preds - y_val))

    metrics = {
        "mae": mae,
        "rmse": rmse,
        "bias": bias,
        "best_optuna_value": float(study.best_value),
        "n_trials": len(study.trials),
        "n_samples": len(X),
        "n_features": X.shape[1],
        "feature_names": list(X.columns),
    }

    return model_version, model_dir_str, metrics, trained_models, now


async def train_models(area: str) -> str:
    """Train p10 / p50 / p90 quantile LightGBM models for *area*.

    Returns the ``model_version`` string (e.g. ``lgbm-DK1-20260406-134500``).
    """
    import asyncio

    settings = get_settings()
    logger.info(
        "Starting training for %s (window=%d days)",
        area,
        settings.training_window_days,
    )

    model_version, artifact_path, metrics, _models, now = await asyncio.to_thread(
        _train_sync,
        area,
        settings.training_window_days,
        settings.model_dir,
    )

    try:
        async with get_session() as session:
            await session.execute(
                text(
                    "UPDATE model_versions SET is_active = FALSE "
                    "WHERE area = :area AND is_active = TRUE"
                ),
                {"area": area},
            )
            await session.execute(
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
                    "feature_set_version": "v1",
                    "trained_at": now,
                    "metrics_json": json.dumps(metrics),
                    "artifact_path": artifact_path,
                },
            )
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
