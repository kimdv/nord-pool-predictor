from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import text

from nordpool_predictor.database import get_session
from nordpool_predictor.ml.artifacts import load_models
from nordpool_predictor.ml.features import (
    TARGET,
    add_calendar_features,
    add_cross_features,
    add_crossborder_features,
    add_issuance_price_features,
    add_price_lag_features,
    add_production_features,
    add_residual_load_features,
    add_weather_features,
    apply_horizon_features,
    load_prices,
)
from nordpool_predictor.ml.horizon import DEFAULT_HORIZON_STEPS, SHORT_HORIZON_STEPS

logger = logging.getLogger(__name__)
PRICE_HISTORY_DAYS = 8


class ModelUnavailableError(ValueError):
    """No candidate has usable model artifacts."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_models(
    model_version: str,
    artifact_path: str,
    area: str,
) -> dict[str, object]:
    return load_models(model_version, artifact_path, area)


async def select_usable_model(
    area: str, candidates: Sequence[Mapping[str, object]]
) -> tuple[str, dict[str, object]]:
    """Load the first usable candidate, in the caller's preference order."""
    last_error = None
    for candidate in candidates:
        version = str(candidate["model_version"])
        try:
            models = await asyncio.to_thread(
                _load_models, version, str(candidate["artifact_path"]), area
            )
        except Exception as exc:
            logger.exception("Skipping unusable model artifacts for %s (%s)", area, version)
            last_error = exc
        else:
            return version, models
    raise ModelUnavailableError(
        f"No usable active compatible model for area {area}"
    ) from last_error


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
    grid_anchor = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    target_ts = pd.date_range(
        grid_anchor + timedelta(minutes=15),
        periods=horizon_steps,
        freq="15min",
    )

    history_start = now - timedelta(days=PRICE_HISTORY_DAYS)
    hist = load_prices(area, history_start, now)

    future = pd.DataFrame({TARGET: np.nan}, index=target_ts)
    future.index.name = "ts"

    if not hist.empty:
        df = pd.concat([hist, future])
        df = df[~df.index.duplicated(keep="first")].sort_index()
    else:
        df = future.copy()

    df = add_calendar_features(df)
    df = add_price_lag_features(df)
    df = df.reindex(target_ts)
    df = add_issuance_price_features(df, hist, now)
    df = add_weather_features(df, area, issued_at=now)
    df = add_production_features(df, area, issued_at=now)
    df = add_crossborder_features(df, area, issued_at=now)
    df = add_cross_features(df)
    df = add_residual_load_features(df)

    for col in df.columns:
        if df[col].dtype == object:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    forecast_df = df.reindex(target_ts).drop(columns=[TARGET], errors="ignore")

    horizon_steps_per_row = np.arange(1, len(forecast_df) + 1, dtype=np.int32)
    forecast_df = apply_horizon_features(forecast_df, horizon_steps_per_row)

    return forecast_df, target_ts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_forecast(
    area: str,
    horizon_steps: int = DEFAULT_HORIZON_STEPS,
    *,
    issued_at: datetime | None = None,
    model_version: str | None = None,
) -> str:
    """Produce a price forecast for *area* and return the ``run_id``."""
    logger.info("Starting forecast for %s (horizon_steps=%d)", area, horizon_steps)
    now = issued_at if issued_at is not None else datetime.now(UTC)

    # 1. Resolve the requested model without falling back to another version.
    query = (
        "SELECT model_version, artifact_path FROM model_versions "
        "WHERE area = :area AND is_active = TRUE "
        "AND feature_set_version = 'v4-crossborder-history' "
    )
    params = {"area": area}
    if model_version is not None:
        query += "AND model_version = :model_version "
        params["model_version"] = model_version
    query += "ORDER BY trained_at DESC, model_version DESC"
    async with get_session() as session:
        result = await session.execute(text(query), params)
        candidates = result.mappings().all()

    if not candidates:
        if model_version is not None:
            raise ValueError(
                f"Requested model {model_version} is not active or compatible for area {area}"
            )
        raise ValueError(f"No active compatible model for area {area}")

    # Pinned queries contain only the requested version. Unpinned refreshes
    # share bootstrap's artifact-aware fallback across all compatible actives.
    model_version, models = await select_usable_model(area, candidates)
    logger.info("Usable active model for %s: %s", area, model_version)

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
        if "short" in models:
            expected_features = list(
                dict.fromkeys([*expected_features, *models["short"]["p50"].feature_name_])
            )
        missing = sorted(set(expected_features) - set(X.columns))
        notes: str | None = None
        if missing:
            notes = f"Degraded: missing {len(missing)} features: {missing[:10]}"
            logger.warning("Degraded forecast for %s: %s", area, notes)

        # 5. Predict ---------------------------------------------------------
        pred_p10, pred_p50, pred_p90 = await asyncio.to_thread(_predict_quantiles, models, X)

        # Independent quantile models can produce crossings (e.g. p10 > p50).
        # Sort per row so the band is always coherent.
        stacked = np.stack([pred_p10, pred_p50, pred_p90], axis=1)
        stacked.sort(axis=1)
        pred_p10, pred_p50, pred_p90 = stacked[:, 0], stacked[:, 1], stacked[:, 2]

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

        # An incomplete experiment must never make a completed production run fail.
        try:
            from nordpool_predictor.ml.short_term import capture_shadow
            from nordpool_predictor.worker import forecast_origin

            await capture_shadow(
                area,
                now,
                X,
                production_model_version=model_version,
                scheduled_origin=forecast_origin.get(),
            )
        except Exception:
            logger.exception(
                "Short-term shadow capture failed for %s; production run retained", area
            )

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


def _predict_quantiles(models: dict, features: pd.DataFrame) -> tuple:
    result = []
    for quantile in ("p10", "p50", "p90"):
        estimator = models[quantile]
        values = np.asarray(
            estimator.predict(
                features.reindex(columns=estimator.feature_name_, fill_value=np.nan)
            ),
            dtype=float,
        )
        if "short" in models:
            mask = features["horizon_step"].to_numpy() <= SHORT_HORIZON_STEPS
            estimator = models["short"][quantile]
            values[mask] = estimator.predict(
                features.loc[mask].reindex(columns=estimator.feature_name_, fill_value=np.nan)
            )
        result.append(values)
    return tuple(result)
