from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

import holidays
import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from nordpool_predictor.config import get_settings
from nordpool_predictor.ml.asof import issuance_times, load_asof_covariates
from nordpool_predictor.ml.horizon import DEFAULT_HORIZON_STEPS

logger = logging.getLogger(__name__)

TARGET = "price_dkk_kwh"
HORIZON_STEP_FEATURE = "horizon_step"

# How far ahead (in 15-min steps) each lag feature is still observable from
# issuance time.  A 15-min step = 1; an hour = 4; a day = 96; a week = 672.
# Example: ``price_lag_24h`` at target ``t`` references price at ``t - 24h``.
# At issuance ``t - h`` we only know prices up to ``t - h``, so this lag is
# observable iff ``h ≤ 24h`` (≤ 96 steps).  Beyond that the lag would require
# a *predicted* price we don't have, so it's masked to NaN.
LAG_HORIZON_LIMITS: dict[str, int] = {
    "price_lag_24h": 96,
    "price_lag_48h": 192,
    "price_lag_168h": 672,
}

_sync_engine: Engine | None = None


def _get_sync_engine() -> Engine:
    """Lazy-initialised sync SQLAlchemy engine (cached module-level)."""
    global _sync_engine
    if _sync_engine is None:
        settings = get_settings()
        _sync_engine = create_engine(
            settings.sync_database_url,
            pool_size=3,
            max_overflow=5,
            pool_pre_ping=True,
        )
    return _sync_engine


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_prices(area: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Load price observations for *area* in ``[start, end)``.

    Returns a DataFrame indexed by ``ts`` with column ``price_dkk_kwh``.
    """
    engine = _get_sync_engine()
    query = text(
        "SELECT ts, price_dkk_kwh "
        "FROM price_observations "
        "WHERE area = :area AND ts >= :start AND ts < :end "
        "ORDER BY ts"
    )
    with engine.connect() as conn:
        df = pd.read_sql(
            query,
            conn,
            params={"area": area, "start": start, "end": end},
            parse_dates={"ts": {"utc": True}},
        )
    if df.empty:
        return pd.DataFrame(columns=[TARGET])
    df = df.set_index("ts").sort_index()
    df = df.rename(columns={"price_dkk_kwh": TARGET})
    df = df[~df.index.duplicated(keep="last")]
    return df


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def build_feature_matrix(
    area: str,
    start: datetime,
    end: datetime,
    *,
    horizon_seed: int = 42,
    horizon_steps: int = DEFAULT_HORIZON_STEPS,
) -> pd.DataFrame:
    """Build the full feature matrix for *area* over ``[start, end)``.

    Extends the data window backward to support lag / rolling features,
    then returns the complete DataFrame (including the extended rows whose
    lags may be partially ``NaN``).
    """
    if not 1 <= horizon_steps <= DEFAULT_HORIZON_STEPS:
        raise ValueError("Training horizon must be between 1 and 768 quarter hours")
    extended_start = start - timedelta(hours=360)

    df = load_prices(area, extended_start, end)
    if df.empty:
        logger.warning("No price data for %s [%s, %s]", area, start, end)
        return df

    history = df.copy()
    df = add_calendar_features(df)
    df = add_price_lag_features(df)
    df = df.loc[df.index >= start].copy()
    if df.empty:
        return df
    rng = np.random.default_rng(horizon_seed)
    horizons = rng.integers(1, horizon_steps + 1, size=len(df), dtype=np.int32)
    cutoffs = pd.Series(df.index - pd.to_timedelta(horizons * 15, unit="min"), index=df.index)
    df = add_issuance_price_features(df, history, cutoffs)
    df = add_weather_features(df, area, issued_at=cutoffs)
    df = add_production_features(df, area, issued_at=cutoffs)
    production_provenance = df.attrs.get("production_provenance", {})
    df = add_crossborder_features(df, area, issued_at=cutoffs)
    df = add_demand_features(df, area, issued_at=cutoffs)
    df = add_cross_features(df)
    df = add_residual_load_features(df)
    df = apply_horizon_features(df, horizons)

    for col in df.columns:
        if df[col].dtype == object:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df.attrs["production_provenance"] = production_provenance
    return df


# ---------------------------------------------------------------------------
# Calendar features
# ---------------------------------------------------------------------------


def add_calendar_features(df: pd.DataFrame) -> pd.DataFrame:
    """Danish local calendar features without changing the timestamp index."""
    idx = df.index.tz_convert("Europe/Copenhagen")
    hour = idx.hour
    dow = idx.dayofweek

    df["hour_sin"] = np.sin(2 * math.pi * hour / 24)
    df["hour_cos"] = np.cos(2 * math.pi * hour / 24)
    df["dow_sin"] = np.sin(2 * math.pi * dow / 7)
    df["dow_cos"] = np.cos(2 * math.pi * dow / 7)

    quarter_of_day = hour * 4 + idx.minute // 15
    df["quarter_sin"] = np.sin(2 * math.pi * quarter_of_day / 96)
    df["quarter_cos"] = np.cos(2 * math.pi * quarter_of_day / 96)

    df["month"] = idx.month
    df["is_weekend"] = (dow >= 5).astype(int)

    year_min, year_max = int(idx.year.min()), int(idx.year.max())
    dk_hols = holidays.Denmark(years=range(year_min, year_max + 1))
    df["is_holiday"] = np.array([int(d in dk_hols) for d in idx.date])
    return df


# ---------------------------------------------------------------------------
# Price lag / rolling features
# ---------------------------------------------------------------------------


def add_price_lag_features(df: pd.DataFrame) -> pd.DataFrame:
    """Lag and rolling-window statistics on the target price (15-min steps)."""
    if TARGET not in df.columns:
        return df

    price = df[TARGET]

    full_idx = pd.date_range(df.index.min(), df.index.max(), freq="15min")
    aligned = price.reindex(full_idx)

    df["price_lag_24h"] = aligned.shift(96).reindex(df.index).values
    df["price_lag_48h"] = aligned.shift(192).reindex(df.index).values
    df["price_lag_168h"] = aligned.shift(672).reindex(df.index).values

    shifted = aligned.shift(1)
    df["price_rolling_mean_24h"] = (
        shifted.rolling(96, min_periods=1).mean().reindex(df.index).values
    )
    df["price_rolling_std_24h"] = shifted.rolling(96, min_periods=1).std().reindex(df.index).values
    df["price_rolling_mean_168h"] = (
        shifted.rolling(672, min_periods=1).mean().reindex(df.index).values
    )
    df["price_rolling_std_168h"] = (
        shifted.rolling(672, min_periods=1).std().reindex(df.index).values
    )
    return df


# ---------------------------------------------------------------------------
# Weather features
# ---------------------------------------------------------------------------


def add_weather_features(
    df: pd.DataFrame, area: str, *, issued_at: datetime | pd.Series
) -> pd.DataFrame:
    """Weather forecast snapshots available at issuance."""
    return load_asof_covariates(_get_sync_engine(), df, area, issued_at, "weather")


# ---------------------------------------------------------------------------
# Production features
# ---------------------------------------------------------------------------


def add_production_features(
    df: pd.DataFrame, area: str, *, issued_at: datetime | pd.Series
) -> pd.DataFrame:
    """Production forecast snapshots available at issuance."""
    return load_asof_covariates(_get_sync_engine(), df, area, issued_at, "production")


# ---------------------------------------------------------------------------
# Cross-border features
# ---------------------------------------------------------------------------


def add_crossborder_features(
    df: pd.DataFrame, area: str, *, issued_at: datetime | pd.Series
) -> pd.DataFrame:
    """Latest complete observed hourly net import known at each issuance."""
    return load_asof_covariates(_get_sync_engine(), df, area, issued_at, "crossborder")


def add_demand_features(
    df: pd.DataFrame, area: str, *, issued_at: datetime | pd.Series
) -> pd.DataFrame:
    """Day-ahead demand forecasts first received no later than issuance."""
    return load_asof_covariates(_get_sync_engine(), df, area, issued_at, "demand")


# ---------------------------------------------------------------------------
# Cross (interaction) features
# ---------------------------------------------------------------------------


def add_cross_features(df: pd.DataFrame) -> pd.DataFrame:
    """Interaction terms between existing feature columns."""
    if "wind_speed_mean" in df.columns and "hour_sin" in df.columns:
        df["wind_x_hour_sin"] = df["wind_speed_mean"] * df["hour_sin"]
    if "temp_mean" in df.columns and "is_weekend" in df.columns:
        df["temp_x_is_weekend"] = df["temp_mean"] * df["is_weekend"]
    return df


# ---------------------------------------------------------------------------
# Renewable supply
# ---------------------------------------------------------------------------


def add_residual_load_features(df: pd.DataFrame) -> pd.DataFrame:
    """Renewable supply and forecast demand left after wind and solar."""
    wind = df.get("wind_mw")
    solar = df.get("solar_mw")

    if wind is not None or solar is not None:
        w = wind.fillna(0) if wind is not None else pd.Series(0, index=df.index)
        s = solar.fillna(0) if solar is not None else pd.Series(0, index=df.index)
        df["total_renewable_mw"] = w + s

    if {"load_mw", "wind_mw", "solar_mw"}.issubset(df.columns):
        df["residual_load_mw"] = df["load_mw"] - df["wind_mw"] - df["solar_mw"]

    return df


# ---------------------------------------------------------------------------
# Horizon-aware features
# ---------------------------------------------------------------------------


def apply_horizon_features(
    df: pd.DataFrame,
    horizon_steps: np.ndarray | pd.Series | list[int],
) -> pd.DataFrame:
    """Attach a ``horizon_step`` feature and mask lags that wouldn't be observable.

    ``horizon_steps`` is the number of 15-min steps between the forecast
    *issuance* time and the *target* timestamp for each row (1-based: step 1
    means 15 minutes ahead).

    The same trained model is used for every horizon, including the refresh buffer.
    Without a horizon-step signal it can't distinguish "predict 15 minutes
    ahead" from "predict 7 days ahead", and it can't learn that long-horizon
    lags are unreliable.  This function fixes both:

    * Adds ``horizon_step`` as an explicit feature so the model can condition
      on it.
    * Sets each lag column to ``NaN`` for rows whose horizon exceeds the
      lag's availability (see :data:`LAG_HORIZON_LIMITS`).  At training time
      this teaches the model what features it will (or won't) have access to
      at prediction time.
    """
    horizon_array = np.asarray(horizon_steps, dtype=np.int32)
    if len(horizon_array) != len(df):
        raise ValueError(
            f"horizon_steps length {len(horizon_array)} does not match DataFrame length {len(df)}"
        )

    df = df.copy()
    df[HORIZON_STEP_FEATURE] = horizon_array

    for col, max_step in LAG_HORIZON_LIMITS.items():
        if col in df.columns:
            mask = horizon_array > max_step
            if mask.any():
                df.loc[mask, col] = np.nan

    return df


def add_issuance_price_features(
    df: pd.DataFrame,
    history: pd.DataFrame,
    issued_at: datetime | pd.Series,
) -> pd.DataFrame:
    """Compute rolling statistics from prices strictly before issuance."""
    if history.empty:
        for hours in (24, 168):
            for label in ("mean", "std"):
                df[f"price_rolling_{label}_{hours}h"] = np.nan
        return df
    cutoffs = issuance_times(df.index, issued_at)
    # Values are shifted one quarter: ceiling includes the current quarter for
    # off-grid issuance while still excluding prices at or after issuance.
    sample_at = pd.DatetimeIndex(cutoffs).ceil("15min")
    index = pd.date_range(
        history.index.min(), max(history.index.max(), sample_at.max()), freq="15min"
    )
    prices = history[TARGET].reindex(index).shift(1)
    for hours, steps in [(24, 96), (168, 672)]:
        for label, values in [
            ("mean", prices.rolling(steps, min_periods=1).mean()),
            ("std", prices.rolling(steps, min_periods=1).std()),
        ]:
            df[f"price_rolling_{label}_{hours}h"] = values.reindex(sample_at).to_numpy()
    return df
