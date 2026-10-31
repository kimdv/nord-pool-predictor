"""Covariates selected from snapshots available at each forecast issuance."""

from __future__ import annotations

import json
from datetime import datetime

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from nordpool_predictor.config import get_settings


def issuance_times(index: pd.DatetimeIndex, value: datetime | pd.Series) -> pd.Series:
    if isinstance(value, pd.Series):
        return value.reindex(index)
    return pd.Series(value, index=index)


def load_asof_covariates(
    engine: Engine,
    df: pd.DataFrame,
    area: str,
    issued_at: datetime | pd.Series,
    kind: str,
) -> pd.DataFrame:
    cutoffs = issuance_times(df.index, issued_at)
    targets = json.dumps(
        [
            {"ts": ts.isoformat(), "issued_at": cutoff.isoformat()}
            for ts, cutoff in zip(df.index, cutoffs, strict=True)
        ]
    )
    if kind == "weather":
        points = [wp.id for wp in get_settings().areas[area].weather_points]
        owners = json.dumps(points)
        table, owner = "weather_forecasts", "area"
        fields = (
            "temperature_c, wind_speed_ms, cloud_cover_pct, precipitation_mm, solar_irradiance_wm2"
        )
        aggregates = """
            AVG(temperature_c) AS temp_mean, MIN(temperature_c) AS temp_min,
            MAX(temperature_c) AS temp_max, AVG(wind_speed_ms) AS wind_speed_mean,
            MAX(wind_speed_ms) AS wind_speed_max, AVG(precipitation_mm) AS precip_mean,
            SUM(precipitation_mm) AS precip_sum, AVG(cloud_cover_pct) AS cloud_cover_mean,
            CASE WHEN COUNT(cloud_cover_pct) = 0 THEN NULL
                 ELSE COALESCE(STDDEV(cloud_cover_pct), 0)
            END AS cloud_cover_std,
            AVG(solar_irradiance_wm2) AS solar_irradiance_mean
        """
    elif kind == "production":
        owners = json.dumps([area])
        table, owner = "production_forecasts", "area"
        fields = "wind_mw, solar_mw"
        aggregates = "AVG(wind_mw) AS wind_mw, AVG(solar_mw) AS solar_mw"
    elif kind == "crossborder":
        with engine.connect() as conn:
            owners = json.dumps(
                list(
                    conn.execute(
                        text(
                            "SELECT DISTINCT connection FROM crossborder_forecasts "
                            "WHERE connection LIKE :area"
                        ),
                        {"area": f"%{area}%"},
                    ).scalars()
                )
            )
        table, owner = "crossborder_forecasts", "connection"
        fields = "flow_mw"
        aggregates = "SUM(flow_mw) AS net_import_mw"
    else:
        raise ValueError(f"Unknown covariate kind: {kind}")

    # All identifiers above are internal constants. Issuance and target times are bound data.
    query = text(f"""
        WITH targets AS (
            SELECT * FROM jsonb_to_recordset(CAST(:targets AS jsonb))
                AS t(ts timestamptz, issued_at timestamptz)
        ), owners AS (SELECT jsonb_array_elements_text(CAST(:owners AS jsonb)) AS id)
        SELECT t.ts, {aggregates}
        FROM targets t CROSS JOIN owners o
        LEFT JOIN LATERAL (
            SELECT {fields} FROM {table} f
            WHERE f.{owner} = o.id
              AND f.ts = date_trunc('hour', t.ts AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
              AND f.issued_at <= t.issued_at
            ORDER BY f.issued_at DESC LIMIT 1
        ) snapshot ON TRUE
        GROUP BY t.ts ORDER BY t.ts
    """)
    with engine.connect() as conn:
        values = pd.read_sql(
            query, conn, params={"targets": targets, "owners": owners}, parse_dates=["ts"]
        )
    if values.empty:
        columns = {
            "weather": [
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
            ],
            "production": ["wind_mw", "solar_mw"],
            "crossborder": ["net_import_mw"],
        }[kind]
        for column in columns:
            df[column] = np.nan
        return df
    return df.join(values.set_index("ts"), how="left")
