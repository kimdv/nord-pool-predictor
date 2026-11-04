"""Covariates selected from snapshots available at each forecast issuance."""

from __future__ import annotations

import json
from datetime import datetime

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from nordpool_predictor.config import get_settings
from nordpool_predictor.ingestion.crossborder import EXCHANGE_COLUMNS, SOURCE


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
        table, owner = "production_feature_snapshots", "area"
        fields = "wind_mw, solar_mw, availability_basis"
        aggregates = (
            "AVG(wind_mw) AS wind_mw, AVG(solar_mw) AS solar_mw, "
            "MAX(availability_basis) AS _production_basis"
        )
    elif kind == "crossborder":
        connections = {
            connection
            for mapping in EXCHANGE_COLUMNS.values()
            for connection in mapping.values()
            if area in connection.split("-")
        }
        owners = json.dumps(
            [
                {"id": connection, "direction": -1 if connection.endswith(f"-{area}") else 1}
                for connection in sorted(connections)
            ]
        )
        query = text("""
            WITH targets AS (
                SELECT * FROM jsonb_to_recordset(CAST(:targets AS jsonb))
                    AS t(ts timestamptz, issued_at timestamptz)
            ), connections AS (
                SELECT * FROM jsonb_to_recordset(CAST(:owners AS jsonb))
                    AS c(id text, direction integer)
            )
            SELECT t.ts, snapshot.observed_net_import_mw
            FROM targets t
            LEFT JOIN LATERAL (
                SELECT SUM(o.flow_mw * c.direction) AS observed_net_import_mw
                FROM connections c JOIN crossborder_observations o ON o.connection = c.id
                WHERE o.source = :source AND o.created_at <= t.issued_at
                  AND o.ts <= t.issued_at - INTERVAL '1 hour'
                  AND o.ts >= t.issued_at - INTERVAL '26 hours'
                GROUP BY o.ts
                HAVING COUNT(o.flow_mw) = (SELECT COUNT(*) FROM connections)
                ORDER BY o.ts DESC LIMIT 1
            ) snapshot ON TRUE
            ORDER BY t.ts
        """)
        with engine.connect() as conn:
            values = pd.read_sql(
                query,
                conn,
                params={"targets": targets, "owners": owners, "source": SOURCE},
                parse_dates=["ts"],
            )
        return df.join(values.set_index("ts"), how="left")
    else:
        raise ValueError(f"Unknown covariate kind: {kind}")

    # All identifiers above are internal constants. Issuance and target times are bound data.
    availability = "available_at" if kind == "production" else "issued_at"
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
              AND f.{availability} <= t.issued_at
            ORDER BY f.{availability} DESC, f.issued_at DESC, f.source LIMIT 1
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
        }[kind]
        for column in columns:
            df[column] = np.nan
        return df
    provenance = None
    if kind == "production":
        provenance = values.pop("_production_basis").fillna("unavailable").value_counts().to_dict()
    result = df.join(values.set_index("ts"), how="left")
    if provenance is not None:
        result.attrs["production_provenance"] = {str(key): int(n) for key, n in provenance.items()}
    return result
