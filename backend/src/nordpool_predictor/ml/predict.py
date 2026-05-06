from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sqlalchemy import text

from nordpool_predictor.database import get_session
from nordpool_predictor.ml.features import (
    TARGET,
    add_calendar_features,
    add_cross_features,
    add_crossborder_features,
    add_price_lag_features,
    add_production_features,
    add_residual_load_features,
    add_weather_features,
    load_prices,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_models(
    model_version: str,
    artifact_path: str,
    area: str,
) -> dict[str, object]:
    """Load the three quantile model artifacts from disk."""
    # model_version is "lgbm-{area}-{time_str}" where time_str may be
    # "20260406" (old) or "20260406-134500" (new).  Extract everything
    # after the area prefix.
    prefix = f"lgbm-{area}-"
    time_str = (
        model_version[len(prefix) :]
        if model_version.startswith(prefix)
        else model_version.rsplit("-", 1)[-1]
    )
    models: dict[str, object] = {}
    for q in ("p10", "p50", "p90"):
        path = Path(artifact_path) / f"lgbm-{area}-{q}-{time_str}.joblib"
        if not path.exists():
            raise FileNotFoundError(f"Model artifact not found: {path}")
        models[q] = joblib.load(path)
        logger.info("Loaded %s model from %s", q, path)
    return models


def _build_forecast_features(
    area: str,
    now: datetime,
    horizon_steps: int,
) -> tuple[pd.DataFrame, pd.DatetimeIndex]:
    """Assemble the feature matrix for the forecast horizon.

    Historical prices are prepended so that lag / rolling features can be
    computed for the first forecast steps.  Weather and production features
    transparently fall back to forecasts for future timestamps.
    """
    target_ts = pd.date_range(
        now + timedelta(minutes=15),
        periods=horizon_steps,
        freq="15min",
    )

    history_start = now - timedelta(hours=192)
    hist = load_prices(area, history_start, now + timedelta(minutes=15))

    future = pd.DataFrame({TARGET: np.nan}, index=target_ts)
    future.index.name = "ts"

    if not hist.empty:
        df = pd.concat([hist, future])
        df = df[~df.index.duplicated(keep="first")].sort_index()
    else:
        df = future.copy()

    df = add_calendar_features(df)
    df = add_price_lag_features(df)
    df = add_weather_features(df, area)
    df = add_production_features(df, area)
    df = add_crossborder_features(df, area)
    df = add_cross_features(df)
    df = add_residual_load_features(df)

    for col in df.columns:
        if df[col].dtype == object:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    forecast_df = df.reindex(target_ts).drop(columns=[TARGET], errors="ignore")
    return forecast_df, target_ts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_forecast(area: str, horizon_steps: int = 672) -> str:
    """Produce a price forecast for *area* and return the ``run_id``."""
    logger.info("Starting forecast for %s (horizon_steps=%d)", area, horizon_steps)
    now = datetime.now(UTC)
    now = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)

    # 1. Resolve the active model -------------------------------------------
    async with get_session() as session:
        result = await session.execute(
            text(
                "SELECT model_version, artifact_path "
                "FROM model_versions "
                "WHERE area = :area AND is_active = TRUE "
                "LIMIT 1"
            ),
            {"area": area},
        )
        row = result.fetchone()

    if row is None:
        raise ValueError(f"No active model for area {area}")

    model_version: str = row[0]
    artifact_path: str = row[1]
    logger.info("Active model for %s: %s", area, model_version)

    # 2. Load quantile models ------------------------------------------------
    models = _load_models(model_version, artifact_path, area)

    # 3. Create a forecast_run record ----------------------------------------
    run_id = str(uuid.uuid4())

    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO forecast_runs "
                "  (run_id, area, model_version, issued_at,"
                "   horizon_steps, status) "
                "VALUES "
                "  (CAST(:run_id AS uuid), :area,"
                "   :model_version, :issued_at,"
                "   :horizon_steps, 'running')"
            ),
            {
                "run_id": run_id,
                "area": area,
                "model_version": model_version,
                "issued_at": now,
                "horizon_steps": horizon_steps,
            },
        )
        await session.commit()

    try:
        # 4. Build features (sync DB work -- run in thread) ------------------
        import asyncio

        X, target_ts = await asyncio.to_thread(
            _build_forecast_features,
            area,
            now,
            horizon_steps,
        )
        logger.info(
            "Built feature matrix for %s: %d rows × %d cols",
            area,
            len(X),
            X.shape[1],
        )

        expected_features: list[str] = models["p50"].feature_name_  # type: ignore[union-attr]
        missing = sorted(set(expected_features) - set(X.columns))
        notes: str | None = None
        if missing:
            notes = f"Degraded: missing {len(missing)} features: {missing[:10]}"
            logger.warning("Degraded forecast for %s: %s", area, notes)

        X_aligned = X.reindex(columns=expected_features, fill_value=np.nan)

        # 5. Predict ---------------------------------------------------------
        pred_p10 = models["p10"].predict(X_aligned)  # type: ignore[union-attr]
        pred_p50 = models["p50"].predict(X_aligned)  # type: ignore[union-attr]
        pred_p90 = models["p90"].predict(X_aligned)  # type: ignore[union-attr]

        # 6. Persist forecast values -----------------------------------------
        forecast_rows = [
            {
                "run_id": run_id,
                "ts": ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
                "horizon_step": i + 1,
                "predicted_price_dkk_kwh": float(pred_p50[i]),
                "lower_dkk_kwh": float(pred_p10[i]),
                "upper_dkk_kwh": float(pred_p90[i]),
            }
            for i, ts in enumerate(target_ts)
        ]

        async with get_session() as session:
            await session.execute(
                text(
                    "INSERT INTO forecast_values "
                    "  (run_id, ts, horizon_step, "
                    "   predicted_price_dkk_kwh, lower_dkk_kwh, upper_dkk_kwh) "
                    "VALUES "
                    "  (CAST(:run_id AS uuid), :ts, :horizon_step, "
                    "   :predicted_price_dkk_kwh, :lower_dkk_kwh, :upper_dkk_kwh)"
                ),
                forecast_rows,
            )
            await session.execute(
                text(
                    "UPDATE forecast_runs "
                    "SET status = 'completed', notes = :notes, updated_at = NOW() "
                    "WHERE run_id = CAST(:run_id AS uuid)"
                ),
                {"run_id": run_id, "notes": notes},
            )
            await session.commit()

        logger.info("Persisted %d forecast rows for run %s", len(forecast_rows), run_id)

        logger.info(
            "Forecast %s complete: %d steps for %s (model=%s%s)",
            run_id,
            horizon_steps,
            area,
            model_version,
            ", DEGRADED" if notes else "",
        )

    except Exception:
        logger.exception("Forecast failed for %s (run_id=%s)", area, run_id)
        async with get_session() as session:
            await session.execute(
                text(
                    "UPDATE forecast_runs "
                    "SET status = 'failed', updated_at = NOW() "
                    "WHERE run_id = CAST(:run_id AS uuid)"
                ),
                {"run_id": run_id},
            )
            await session.commit()
        raise

    return run_id
