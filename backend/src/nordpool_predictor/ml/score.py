from __future__ import annotations

import logging
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import text

from nordpool_predictor.database import get_session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def score_forecasts(area: str | None = None) -> None:
    """Score all *completed* (but not yet scored) forecast runs.

    If *area* is given, only runs for that area are scored.
    """
    if area:
        query = text(
            "SELECT run_id, area, issued_at "
            "FROM forecast_runs "
            "WHERE status = 'completed' AND area = :area "
            "ORDER BY issued_at"
        )
        params: dict = {"area": area}
    else:
        query = text(
            "SELECT run_id, area, issued_at "
            "FROM forecast_runs "
            "WHERE status = 'completed' "
            "ORDER BY issued_at"
        )
        params = {}

    async with get_session() as session:
        result = await session.execute(query, params)
        runs = result.fetchall()

    if not runs:
        logger.info("No forecast runs to score%s", f" for {area}" if area else "")
        return

    logger.info("Scoring %d forecast run(s)", len(runs))
    for row in runs:
        run_id = str(row[0])
        run_area = str(row[1])
        issued_at = row[2]
        try:
            await _score_single_run(run_id, run_area, issued_at)
        except Exception:
            logger.exception("Failed to score run %s", run_id)


# ---------------------------------------------------------------------------
# Single-run scoring
# ---------------------------------------------------------------------------


async def _score_single_run(
    run_id: str,
    area: str,
    issued_at: datetime,
) -> None:
    # -- Forecast values -----------------------------------------------------
    async with get_session() as session:
        result = await session.execute(
            text(
                "SELECT ts, horizon_step, predicted_price_dkk_kwh, "
                "       lower_dkk_kwh, upper_dkk_kwh "
                "FROM forecast_values "
                "WHERE run_id = CAST(:run_id AS uuid) ORDER BY ts"
            ),
            {"run_id": run_id},
        )
        fc_rows = result.fetchall()

    if not fc_rows:
        logger.warning("No forecast values for run %s", run_id)
        return

    fc = pd.DataFrame(
        fc_rows,
        columns=["ts", "horizon_step", "predicted", "lower", "upper"],
    )

    # -- Actual prices -------------------------------------------------------
    ts_min, ts_max = fc["ts"].min(), fc["ts"].max()

    async with get_session() as session:
        result = await session.execute(
            text(
                "SELECT ts, price_dkk_kwh "
                "FROM price_observations "
                "WHERE area = :area AND ts >= :ts_min AND ts <= :ts_max"
            ),
            {"area": area, "ts_min": ts_min, "ts_max": ts_max},
        )
        actual_rows = result.fetchall()

    if not actual_rows:
        logger.info("No actuals available yet for run %s — skipping", run_id)
        return

    actuals = pd.DataFrame(actual_rows, columns=["ts", "actual"])
    merged = fc.merge(actuals, on="ts", how="inner")

    if merged.empty:
        logger.info("No matching actuals for run %s", run_id)
        return

    coverage = len(merged) / len(fc)
    if coverage < 0.5:
        logger.info(
            "Insufficient coverage for run %s (%.0f%%) — skipping",
            run_id,
            coverage * 100,
        )
        return

    # -- Per-hour errors -----------------------------------------------------
    merged["abs_error"] = (merged["predicted"] - merged["actual"]).abs()
    merged["signed_error"] = merged["predicted"] - merged["actual"]
    merged["sq_error"] = merged["signed_error"] ** 2

    error_rows = [
        {
            "run_id": run_id,
            "ts": r.ts,
            "actual_price_dkk_kwh": float(r.actual),
            "predicted_price_dkk_kwh": float(r.predicted),
            "abs_error": float(r.abs_error),
            "signed_error": float(r.signed_error),
            "sq_error": float(r.sq_error),
        }
        for r in merged.itertuples()
    ]

    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO forecast_errors "
                "  (run_id, ts, actual_price_dkk_kwh, predicted_price_dkk_kwh, "
                "   abs_error, signed_error, sq_error) "
                "VALUES "
                "  (CAST(:run_id AS uuid), :ts, :actual_price_dkk_kwh, "
                "   :predicted_price_dkk_kwh, :abs_error, :signed_error, :sq_error) "
                "ON CONFLICT (run_id, ts) DO NOTHING"
            ),
            error_rows,
        )
        await session.commit()

    # -- Aggregate metrics by horizon (15-min steps) -------------------------
    metrics: dict[str, float | None] = {}
    for label, max_step in [("24h", 96), ("72h", 288), ("168h", 672)]:
        subset = merged[merged["horizon_step"] <= max_step]
        if subset.empty:
            metrics[f"mae_{label}"] = None
            metrics[f"rmse_{label}"] = None
            metrics[f"bias_{label}"] = None
            continue
        metrics[f"mae_{label}"] = float(subset["abs_error"].mean())
        metrics[f"rmse_{label}"] = float(np.sqrt(subset["sq_error"].mean()))
        metrics[f"bias_{label}"] = float(subset["signed_error"].mean())

    # -- Hit-rates -----------------------------------------------------------
    n = len(merged)
    metrics["hitrate_0_05"] = float((merged["abs_error"] <= 0.05).sum() / n)
    metrics["hitrate_0_10"] = float((merged["abs_error"] <= 0.10).sum() / n)
    metrics["hitrate_0_20"] = float((merged["abs_error"] <= 0.20).sum() / n)

    # -- Quality label -------------------------------------------------------
    overall_mae = float(merged["abs_error"].mean())
    if overall_mae < 0.05:
        quality = "excellent"
    elif overall_mae < 0.10:
        quality = "good"
    elif overall_mae < 0.15:
        quality = "acceptable"
    else:
        quality = "poor"

    # -- Worst hour ----------------------------------------------------------
    worst_idx = int(merged["abs_error"].idxmax())
    worst_ts = merged.loc[worst_idx, "ts"]
    worst_error = float(merged.loc[worst_idx, "abs_error"])

    # -- Write forecast_run_metrics ------------------------------------------
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO forecast_run_metrics "
                "  (run_id, mae_24h, mae_72h, mae_168h, "
                "   rmse_24h, rmse_72h, rmse_168h, "
                "   bias_24h, bias_72h, bias_168h, "
                "   hitrate_0_05, hitrate_0_10, hitrate_0_20, "
                "   quality_label, worst_hour_ts, worst_hour_abs_error) "
                "VALUES "
                "  (CAST(:run_id AS uuid), :mae_24h, :mae_72h, :mae_168h, "
                "   :rmse_24h, :rmse_72h, :rmse_168h, "
                "   :bias_24h, :bias_72h, :bias_168h, "
                "   :hitrate_0_05, :hitrate_0_10, :hitrate_0_20, "
                "   :quality_label, :worst_hour_ts, :worst_hour_abs_error) "
                "ON CONFLICT (run_id) DO NOTHING"
            ),
            {
                "run_id": run_id,
                **metrics,
                "quality_label": quality,
                "worst_hour_ts": worst_ts,
                "worst_hour_abs_error": worst_error,
            },
        )
        await session.commit()

    # -- Baselines -----------------------------------------------------------
    await score_baselines(run_id, area, fc, actuals)

    # -- Mark run as scored --------------------------------------------------
    async with get_session() as session:
        await session.execute(
            text(
                "UPDATE forecast_runs "
                "SET status = 'scored', updated_at = NOW() "
                "WHERE run_id = CAST(:run_id AS uuid)"
            ),
            {"run_id": run_id},
        )
        await session.commit()

    logger.info(
        "Scored run %s: quality=%s MAE_24h=%.4f",
        run_id,
        quality,
        metrics.get("mae_24h") or 0.0,
    )


# ---------------------------------------------------------------------------
# Baseline scoring
# ---------------------------------------------------------------------------


async def score_baselines(
    run_id: str,
    area: str,
    forecast_df: pd.DataFrame,
    actuals_df: pd.DataFrame,
) -> None:
    """Evaluate naive baselines against the same actuals.

    Baselines:
    * ``yesterday_same_slot`` – actual price 24 h ago
    * ``last_week_same_slot`` – actual price 168 h ago
    * ``quarter_of_week_average`` – rolling 4-week mean for the same quarter-of-week
    """
    forecast_ts = forecast_df["ts"]
    ts_min = forecast_ts.min()
    ts_max = forecast_ts.max()

    history_start = ts_min - timedelta(weeks=4)

    async with get_session() as session:
        result = await session.execute(
            text(
                "SELECT ts, price_dkk_kwh "
                "FROM price_observations "
                "WHERE area = :area AND ts >= :start AND ts <= :end "
                "ORDER BY ts"
            ),
            {"area": area, "start": history_start, "end": ts_max},
        )
        rows = result.fetchall()

    if not rows:
        logger.warning("No price history for baselines (run %s)", run_id)
        return

    prices = pd.Series(
        {r[0]: float(r[1]) for r in rows},
        dtype=float,
    )
    actual_map: dict = dict(zip(actuals_df["ts"], actuals_df["actual"]))

    baselines: dict[str, dict] = {
        "yesterday_same_slot": {},
        "last_week_same_slot": {},
        "quarter_of_week_average": {},
    }

    for ts in forecast_ts:
        lag_24 = ts - timedelta(hours=24)
        if lag_24 in prices.index:
            baselines["yesterday_same_slot"][ts] = float(prices[lag_24])

        lag_168 = ts - timedelta(hours=168)
        if lag_168 in prices.index:
            baselines["last_week_same_slot"][ts] = float(prices[lag_168])

        candidates = [ts - timedelta(weeks=w) for w in range(1, 5)]
        vals = [float(prices[c]) for c in candidates if c in prices.index]
        if vals:
            baselines["quarter_of_week_average"][ts] = float(np.mean(vals))

    insert_rows: list[dict] = []
    for name, preds in baselines.items():
        errors: list[float] = []
        for ts, pred in preds.items():
            if ts in actual_map:
                errors.append(pred - float(actual_map[ts]))
        if not errors:
            continue
        err = np.asarray(errors, dtype=float)
        insert_rows.append(
            {
                "run_id": run_id,
                "baseline_name": name,
                "mae": float(np.mean(np.abs(err))),
                "rmse": float(np.sqrt(np.mean(err**2))),
                "bias": float(np.mean(err)),
            }
        )

    if not insert_rows:
        return

    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO baseline_metrics "
                "  (run_id, baseline_name, mae, rmse, bias) "
                "VALUES "
                "  (CAST(:run_id AS uuid), :baseline_name, :mae, :rmse, :bias) "
                "ON CONFLICT (run_id, baseline_name) DO NOTHING"
            ),
            insert_rows,
        )
        await session.commit()

    logger.info("Scored %d baseline(s) for run %s", len(insert_rows), run_id)
