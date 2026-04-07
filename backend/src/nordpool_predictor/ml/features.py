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

logger = logging.getLogger(__name__)

TARGET = "price_dkk_kwh"

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
# Helpers
# ---------------------------------------------------------------------------


def _weather_point_ids(area: str) -> list[str]:
    """Return weather-point IDs that belong to *area* (from config)."""
    settings = get_settings()
    area_cfg = settings.areas.get(area)
    if area_cfg is None:
        return []
    return [wp.id for wp in area_cfg.weather_points]


def _in_params(prefix: str, values: list[str]) -> tuple[str, dict[str, str]]:
    """Build a parameterised ``IN (…)`` fragment.

    Returns the placeholder SQL string and the matching params dict.
    """
    if not values:
        return "NULL", {}
    placeholders = ", ".join(f":{prefix}_{i}" for i in range(len(values)))
    params = {f"{prefix}_{i}": v for i, v in enumerate(values)}
    return placeholders, params


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
            parse_dates=["ts"],
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
) -> pd.DataFrame:
    """Build the full feature matrix for *area* over ``[start, end)``.

    Extends the data window backward to support lag / rolling features,
    then returns the complete DataFrame (including the extended rows whose
    lags may be partially ``NaN``).
    """
    extended_start = start - timedelta(hours=192)

    df = load_prices(area, extended_start, end)
    if df.empty:
        logger.warning("No price data for %s [%s, %s]", area, start, end)
        return df

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

    return df


# ---------------------------------------------------------------------------
# Calendar features
# ---------------------------------------------------------------------------


def add_calendar_features(df: pd.DataFrame) -> pd.DataFrame:
    """Sin/cos cyclical encoding, month, weekend and Danish-holiday flags."""
    idx = df.index
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

_WEATHER_AGG_SQL = """\
    AVG(temperature_c)                       AS temp_mean,
    MIN(temperature_c)                       AS temp_min,
    MAX(temperature_c)                       AS temp_max,
    AVG(wind_speed_ms)                       AS wind_speed_mean,
    MAX(wind_speed_ms)                       AS wind_speed_max,
    AVG(precipitation_mm)                    AS precip_mean,
    SUM(precipitation_mm)                    AS precip_sum,
    AVG(cloud_cover_pct)                     AS cloud_cover_mean,
    COALESCE(STDDEV(cloud_cover_pct), 0)     AS cloud_cover_std,
    AVG(solar_irradiance_wm2)                AS solar_irradiance_mean"""

_WEATHER_FEATURE_COLS = [
    "temp_mean",
    "temp_min",
    "temp_max",
    "wind_speed_mean",
    "wind_speed_max",
    "precip_mean",
    "precip_sum",
    "cloud_cover_mean",
    "cloud_cover_std",
    "solar_irradiance_mean",
]


def _query_weather_obs(
    engine: Engine,
    point_ids: list[str],
    ts_min: datetime,
    ts_max: datetime,
) -> pd.DataFrame:
    placeholders, params = _in_params("wp", point_ids)
    params.update(ts_min=ts_min, ts_max=ts_max)
    query = text(
        f"SELECT ts, {_WEATHER_AGG_SQL} "  # noqa: S608
        f"FROM weather_observations "
        f"WHERE area IN ({placeholders}) AND ts >= :ts_min AND ts <= :ts_max "
        f"GROUP BY ts ORDER BY ts"
    )
    with engine.connect() as conn:
        df = pd.read_sql(query, conn, params=params, parse_dates=["ts"])
    if df.empty:
        return pd.DataFrame(columns=_WEATHER_FEATURE_COLS)
    return df.set_index("ts")


def _query_weather_fc(
    engine: Engine,
    point_ids: list[str],
    ts_min: datetime,
    ts_max: datetime,
) -> pd.DataFrame:
    placeholders, params = _in_params("wp", point_ids)
    params.update(ts_min=ts_min, ts_max=ts_max)
    query = text(
        f"WITH latest AS ("
        f"  SELECT DISTINCT ON (area, ts) "
        f"    area, ts, temperature_c, wind_speed_ms, cloud_cover_pct, "
        f"    precipitation_mm, solar_irradiance_wm2 "
        f"  FROM weather_forecasts "
        f"  WHERE area IN ({placeholders}) AND ts >= :ts_min AND ts <= :ts_max "
        f"  ORDER BY area, ts, issued_at DESC"
        f") "
        f"SELECT ts, {_WEATHER_AGG_SQL} "
        f"FROM latest GROUP BY ts ORDER BY ts"
    )
    with engine.connect() as conn:
        df = pd.read_sql(query, conn, params=params, parse_dates=["ts"])
    if df.empty:
        return pd.DataFrame(columns=_WEATHER_FEATURE_COLS)
    return df.set_index("ts")


def add_weather_features(df: pd.DataFrame, area: str) -> pd.DataFrame:
    """Zone-level aggregated weather: observations first, forecasts as fallback."""
    point_ids = _weather_point_ids(area)
    if not point_ids:
        logger.warning("No weather points configured for area %s", area)
        for col in _WEATHER_FEATURE_COLS:
            df[col] = np.nan
        return df

    engine = _get_sync_engine()
    ts_min, ts_max = df.index.min(), df.index.max()

    obs = _query_weather_obs(engine, point_ids, ts_min, ts_max)

    missing = df.index.difference(obs.index) if not obs.empty else df.index
    if len(missing) > 0:
        fc = _query_weather_fc(engine, point_ids, missing.min(), missing.max())
        if obs.empty:
            obs = fc
        elif not fc.empty:
            obs = pd.concat([obs, fc])
            obs = obs[~obs.index.duplicated(keep="first")]

    if obs.empty:
        logger.warning("No weather data available for area %s", area)
        for col in _WEATHER_FEATURE_COLS:
            df[col] = np.nan
        return df

    obs = obs.reindex(df.index, method="ffill")
    df = df.join(obs, how="left")
    return df


# ---------------------------------------------------------------------------
# Production features
# ---------------------------------------------------------------------------


def _query_production_obs(
    engine: Engine,
    area: str,
    ts_min: datetime,
    ts_max: datetime,
) -> pd.DataFrame:
    query = text(
        "SELECT ts, AVG(wind_mw) AS wind_mw, AVG(solar_mw) AS solar_mw "
        "FROM production_observations "
        "WHERE area = :area AND ts >= :ts_min AND ts <= :ts_max "
        "GROUP BY ts ORDER BY ts"
    )
    with engine.connect() as conn:
        df = pd.read_sql(
            query,
            conn,
            params={"area": area, "ts_min": ts_min, "ts_max": ts_max},
            parse_dates=["ts"],
        )
    if df.empty:
        return pd.DataFrame(columns=["wind_mw", "solar_mw"])
    return df.set_index("ts")


def _query_production_fc(
    engine: Engine,
    area: str,
    ts_min: datetime,
    ts_max: datetime,
) -> pd.DataFrame:
    query = text(
        "SELECT DISTINCT ON (ts) ts, wind_mw, solar_mw "
        "FROM production_forecasts "
        "WHERE area = :area AND ts >= :ts_min AND ts <= :ts_max "
        "ORDER BY ts, issued_at DESC"
    )
    with engine.connect() as conn:
        df = pd.read_sql(
            query,
            conn,
            params={"area": area, "ts_min": ts_min, "ts_max": ts_max},
            parse_dates=["ts"],
        )
    if df.empty:
        return pd.DataFrame(columns=["wind_mw", "solar_mw"])
    return df.set_index("ts")


def add_production_features(df: pd.DataFrame, area: str) -> pd.DataFrame:
    """Wind and solar production (MW): observations first, forecasts as fallback."""
    engine = _get_sync_engine()
    ts_min, ts_max = df.index.min(), df.index.max()

    obs = _query_production_obs(engine, area, ts_min, ts_max)

    missing = df.index.difference(obs.index) if not obs.empty else df.index
    if len(missing) > 0:
        fc = _query_production_fc(engine, area, missing.min(), missing.max())
        if obs.empty:
            obs = fc
        elif not fc.empty:
            obs = pd.concat([obs, fc])
            obs = obs[~obs.index.duplicated(keep="first")]

    if obs.empty:
        logger.warning("No production data for area %s", area)
        df["wind_mw"] = np.nan
        df["solar_mw"] = np.nan
        return df

    obs = obs[["wind_mw", "solar_mw"]].reindex(df.index, method="ffill")
    df = df.join(obs, how="left")
    return df


# ---------------------------------------------------------------------------
# Cross-border features
# ---------------------------------------------------------------------------


def _query_crossborder_obs(
    engine: Engine,
    pattern: str,
    ts_min: datetime,
    ts_max: datetime,
) -> pd.DataFrame:
    query = text(
        "SELECT connection, ts, AVG(flow_mw) AS flow_mw "
        "FROM crossborder_observations "
        "WHERE connection LIKE :pattern AND ts >= :ts_min AND ts <= :ts_max "
        "GROUP BY connection, ts"
    )
    with engine.connect() as conn:
        return pd.read_sql(
            query,
            conn,
            params={"pattern": pattern, "ts_min": ts_min, "ts_max": ts_max},
            parse_dates=["ts"],
        )


def _query_crossborder_fc(
    engine: Engine,
    pattern: str,
    ts_min: datetime,
    ts_max: datetime,
) -> pd.DataFrame:
    query = text(
        "WITH latest AS ("
        "  SELECT DISTINCT ON (connection, ts) connection, ts, flow_mw "
        "  FROM crossborder_forecasts "
        "  WHERE connection LIKE :pattern AND ts >= :ts_min AND ts <= :ts_max "
        "  ORDER BY connection, ts, issued_at DESC"
        ") SELECT * FROM latest"
    )
    with engine.connect() as conn:
        return pd.read_sql(
            query,
            conn,
            params={"pattern": pattern, "ts_min": ts_min, "ts_max": ts_max},
            parse_dates=["ts"],
        )


def add_crossborder_features(df: pd.DataFrame, area: str) -> pd.DataFrame:
    """Per-connection flow and aggregate net-import feature."""
    engine = _get_sync_engine()
    ts_min, ts_max = df.index.min(), df.index.max()
    pattern = f"%{area}%"

    obs = _query_crossborder_obs(engine, pattern, ts_min, ts_max)
    fc = _query_crossborder_fc(engine, pattern, ts_min, ts_max)

    combined = pd.concat([obs, fc], ignore_index=True)
    combined = combined.drop_duplicates(subset=["connection", "ts"], keep="first")

    if combined.empty:
        logger.warning("No crossborder data for area %s", area)
        df["net_import_mw"] = np.nan
        return df

    pivot = combined.pivot_table(
        index="ts",
        columns="connection",
        values="flow_mw",
        aggfunc="mean",
    )
    pivot.columns = [f"flow_{c}" for c in pivot.columns]
    pivot["net_import_mw"] = pivot.sum(axis=1)

    pivot = pivot.reindex(df.index, method="ffill")
    df = df.join(pivot, how="left")
    return df


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
# Residual-load proxy
# ---------------------------------------------------------------------------


def add_residual_load_features(df: pd.DataFrame) -> pd.DataFrame:
    """Derived renewable-supply features and residual-load proxy."""
    wind = df.get("wind_mw")
    solar = df.get("solar_mw")

    if wind is not None or solar is not None:
        w = wind.fillna(0) if wind is not None else pd.Series(0, index=df.index)
        s = solar.fillna(0) if solar is not None else pd.Series(0, index=df.index)
        df["total_renewable_mw"] = w + s

    if "total_renewable_mw" in df.columns and "net_import_mw" in df.columns:
        df["residual_load_proxy_mw"] = df["net_import_mw"] - df["total_renewable_mw"]

    return df
