from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from nordpool_predictor.api.schemas.forecasts import (
    AccuracyMetrics,
    BenchmarkEntry,
    BenchmarkResponse,
    ForecastPoint,
    ForecastRunResponse,
    SnapshotSummary,
)
from nordpool_predictor.config import get_settings
from nordpool_predictor.database import get_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/forecasts", tags=["forecasts"])


def _validate_area(area: str) -> None:
    if area not in get_settings().area_codes:
        raise HTTPException(status_code=404, detail=f"Unknown area: {area}")


@router.get("/{area}", response_model=list[ForecastRunResponse])
async def get_forecasts(
    area: str,
    start: datetime | None = Query(None, description="Start timestamp (ISO 8601)"),
    end: datetime | None = Query(None, description="End timestamp (ISO 8601)"),
) -> list[ForecastRunResponse]:
    _validate_area(area)

    clauses = ["fr.area = :area"]
    params: dict[str, str | datetime] = {"area": area}

    if start is not None:
        clauses.append("fr.issued_at >= :start")
        params["start"] = start
    if end is not None:
        clauses.append("fr.issued_at <= :end")
        params["end"] = end

    where = " AND ".join(clauses)
    runs_query = text(
        f"SELECT run_id, area, model_version, issued_at, status, notes "  # noqa: S608
        f"FROM forecast_runs fr WHERE {where} "
        f"ORDER BY fr.issued_at DESC"
    )

    async with get_session() as session:
        runs_result = await session.execute(runs_query, params)
        runs = runs_result.mappings().all()

        responses: list[ForecastRunResponse] = []
        for run in runs:
            vals_result = await session.execute(
                text(
                    "SELECT ts, predicted_price_dkk_kwh, lower_dkk_kwh, upper_dkk_kwh "
                    "FROM forecast_values WHERE run_id = :run_id ORDER BY ts"
                ),
                {"run_id": run["run_id"]},
            )
            values = [ForecastPoint.model_validate(v) for v in vals_result.mappings().all()]
            responses.append(
                ForecastRunResponse(
                    run_id=str(run["run_id"]),
                    area=run["area"],
                    model_version=run["model_version"],
                    issued_at=run["issued_at"],
                    status=run["status"],
                    notes=run["notes"],
                    values=values,
                )
            )

    return responses


@router.get("/{area}/latest", response_model=ForecastRunResponse)
async def get_latest_forecast(area: str) -> ForecastRunResponse:
    _validate_area(area)

    async with get_session() as session:
        run_result = await session.execute(
            text(
                "SELECT run_id, area, model_version, issued_at, status "
                "FROM latest_forecast_runs WHERE area = :area"
            ),
            {"area": area},
        )
        run = run_result.mappings().first()
        if run is None:
            raise HTTPException(status_code=404, detail=f"No forecast found for area {area}")

        vals_result = await session.execute(
            text(
                "SELECT ts, predicted_price_dkk_kwh, lower_dkk_kwh, upper_dkk_kwh "
                "FROM forecast_values WHERE run_id = :run_id ORDER BY ts"
            ),
            {"run_id": run["run_id"]},
        )
        values = [ForecastPoint.model_validate(v) for v in vals_result.mappings().all()]

        prod_horizon_result = await session.execute(
            text("SELECT MAX(ts) AS max_ts FROM production_forecasts WHERE area = :area"),
            {"area": area},
        )
        prod_row = prod_horizon_result.mappings().first()
        prod_horizon = prod_row["max_ts"] if prod_row else None
        logger.info("Production forecast horizon for %s: %s", area, prod_horizon)

    return ForecastRunResponse(
        run_id=str(run["run_id"]),
        area=run["area"],
        model_version=run["model_version"],
        issued_at=run["issued_at"],
        status=run["status"],
        notes=None,
        values=values,
        production_forecast_horizon=prod_horizon,
    )


@router.get("/{area}/accuracy", response_model=list[AccuracyMetrics])
async def get_accuracy(
    area: str,
    limit: int = Query(10, ge=1, le=100),
) -> list[AccuracyMetrics]:
    _validate_area(area)

    async with get_session() as session:
        result = await session.execute(
            text(
                "SELECT frm.run_id, fr.issued_at, "
                "frm.mae_24h, frm.rmse_24h, frm.bias_24h, frm.quality_label "
                "FROM forecast_run_metrics frm "
                "JOIN forecast_runs fr ON fr.run_id = frm.run_id "
                "WHERE fr.area = :area "
                "ORDER BY fr.issued_at DESC LIMIT :limit"
            ),
            {"area": area, "limit": limit},
        )
        rows = result.mappings().all()

    return [
        AccuracyMetrics(
            run_id=str(row["run_id"]),
            issued_at=row["issued_at"],
            mae_24h=row["mae_24h"],
            rmse_24h=row["rmse_24h"],
            bias_24h=row["bias_24h"],
            quality_label=row["quality_label"],
        )
        for row in rows
    ]


@router.get("/{area}/benchmarks", response_model=BenchmarkResponse)
async def get_benchmarks(area: str) -> BenchmarkResponse:
    _validate_area(area)

    async with get_session() as session:
        run_result = await session.execute(
            text("SELECT run_id, area FROM latest_forecast_runs WHERE area = :area"),
            {"area": area},
        )
        run = run_result.mappings().first()
        if run is None:
            raise HTTPException(status_code=404, detail=f"No forecast found for area {area}")

        run_id = run["run_id"]

        ml_result = await session.execute(
            text("SELECT mae_24h, rmse_24h FROM forecast_run_metrics WHERE run_id = :run_id"),
            {"run_id": run_id},
        )
        ml_row = ml_result.mappings().first()

        baselines_result = await session.execute(
            text(
                "SELECT baseline_name, mae, rmse, bias "
                "FROM baseline_metrics WHERE run_id = :run_id "
                "ORDER BY baseline_name"
            ),
            {"run_id": run_id},
        )
        baselines = [BenchmarkEntry.model_validate(b) for b in baselines_result.mappings().all()]

    return BenchmarkResponse(
        run_id=str(run_id),
        area=area,
        ml_mae=ml_row["mae_24h"] if ml_row else None,
        ml_rmse=ml_row["rmse_24h"] if ml_row else None,
        baselines=baselines,
    )


@router.get("/{area}/snapshots", response_model=list[SnapshotSummary])
async def get_snapshots(
    area: str,
    limit: int = Query(14, ge=1, le=100),
) -> list[SnapshotSummary]:
    _validate_area(area)

    async with get_session() as session:
        result = await session.execute(
            text(
                "SELECT fr.run_id, fr.issued_at, fr.horizon_steps AS steps "
                "FROM forecast_runs fr "
                "WHERE fr.area = :area AND fr.status IN ('completed', 'scored') "
                "ORDER BY fr.issued_at DESC LIMIT :limit"
            ),
            {"area": area, "limit": limit},
        )
        rows = result.mappings().all()

    return [
        SnapshotSummary(run_id=str(row["run_id"]), issued_at=row["issued_at"], steps=row["steps"])
        for row in rows
    ]
