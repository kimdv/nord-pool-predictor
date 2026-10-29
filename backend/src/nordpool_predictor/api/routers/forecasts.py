from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import AwareDatetime
from sqlalchemy import text

from nordpool_predictor.api.pagination import decode_cursor, encode_cursor
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
    response: Response,
    limit: int = Query(14, ge=1, le=100),
    cursor: str | None = Query(None, max_length=512),
    start: AwareDatetime | None = Query(None, description="Start timestamp (ISO 8601)"),
    end: AwareDatetime | None = Query(None, description="End timestamp (ISO 8601)"),
) -> list[ForecastRunResponse]:
    _validate_area(area)

    clauses = ["fr.area = :area"]
    params: dict[str, object] = {"area": area}

    if start is not None:
        clauses.append("fr.issued_at >= :start")
        params["start"] = start
    if end is not None:
        clauses.append("fr.issued_at <= :end")
        params["end"] = end

    if start is not None and end is not None and start > end:
        raise HTTPException(status_code=422, detail="start must not be after end")
    if cursor is not None:
        timestamp, identifier = decode_cursor(cursor)
        try:
            identifier = UUID(identifier)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid forecast cursor") from exc
        clauses.append("(fr.issued_at, fr.run_id) < (:cursor_time, :cursor_id)")
        params.update(cursor_time=timestamp, cursor_id=identifier)
    params["limit"] = limit + 1
    where = " AND ".join(clauses)
    runs_query = text(
        f"SELECT run_id, area, model_version, issued_at, status, notes "  # noqa: S608
        f"FROM forecast_runs fr WHERE {where} "
        f"ORDER BY fr.issued_at DESC, fr.run_id DESC LIMIT :limit"
    )

    async with get_session() as session:
        runs_result = await session.execute(runs_query, params)
        runs = runs_result.mappings().all()

        if len(runs) > limit:
            runs = runs[:limit]
            response.headers["X-Next-Cursor"] = encode_cursor(
                runs[-1]["issued_at"], runs[-1]["run_id"]
            )
        values_by_run: dict = {run["run_id"]: [] for run in runs}
        if runs:
            values_result = await session.execute(
                text(
                    "SELECT run_id, ts, predicted_price_dkk_kwh, lower_dkk_kwh, upper_dkk_kwh "
                    "FROM forecast_values WHERE run_id = ANY(CAST(:ids AS uuid[])) "
                    "ORDER BY run_id, ts"
                ),
                {"ids": list(values_by_run)},
            )
            for value in values_result.mappings().all():
                values_by_run[value["run_id"]].append(ForecastPoint.model_validate(value))
        responses = [
            ForecastRunResponse(
                run_id=str(run["run_id"]),
                area=run["area"],
                model_version=run["model_version"],
                issued_at=run["issued_at"],
                status=run["status"],
                notes=run["notes"],
                values=values_by_run[run["run_id"]],
            )
            for run in runs
        ]

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
            text(
                """
                SELECT MAX(max_ts) AS max_ts
                FROM (
                    SELECT MAX(ts) AS max_ts
                    FROM production_observations
                    WHERE area = :area
                    UNION ALL
                    SELECT MAX(ts) AS max_ts
                    FROM production_forecasts
                    WHERE area = :area
                      AND issued_at = (
                          SELECT MAX(issued_at)
                          FROM production_forecasts
                          WHERE area = :area
                      )
                ) horizons
                """
            ),
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
                "frm.mae_24h, frm.rmse_24h, frm.bias_24h, frm.quality_label, "
                "frm.coverage_24h, frm.coverage_72h, frm.coverage_168h "
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
            coverage_24h=row["coverage_24h"],
            coverage_72h=row["coverage_72h"],
            coverage_168h=row["coverage_168h"],
        )
        for row in rows
    ]


@router.get("/{area}/benchmarks", response_model=BenchmarkResponse)
async def get_benchmarks(area: str) -> BenchmarkResponse:
    _validate_area(area)

    async with get_session() as session:
        run_result = await session.execute(
            text(
                "SELECT fr.run_id, fr.area FROM forecast_runs fr "
                "JOIN forecast_run_metrics fm ON fm.run_id = fr.run_id "
                "WHERE fr.area = :area AND fm.mae_24h IS NOT NULL "
                "ORDER BY fr.issued_at DESC LIMIT 1"
            ),
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
