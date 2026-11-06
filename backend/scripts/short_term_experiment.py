"""Operate the prospective short-term comparison without changing active models.

Training and captures read DATABASE_URL and write only beneath MODEL_DIR/short-term.
Promotion remains the existing worker's retraining job after all gates pass.
Stop the worker before every CLI command; existing worker and job locks enforce exclusivity.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from nordpool_predictor.config import get_settings
from nordpool_predictor.database import dispose_engine
from nordpool_predictor.ml.features import build_feature_matrix
from nordpool_predictor.ml.horizon import DEFAULT_HORIZON_STEPS
from nordpool_predictor.ml.predict import (
    ModelUnavailableError,
    _build_forecast_features,
    select_active_model,
)
from nordpool_predictor.ml.short_term import (
    approve_runtime,
    capture_shadow,
    evaluate_experiment,
    experiment_path,
    reproduce,
    train_experiment,
)
from nordpool_predictor.worker import JOB_PROCESS_LOCK, run_with_worker_lock, worker_lock


async def _run_command(args):
    settings = get_settings()
    if args.area not in settings.area_codes:
        raise ValueError("Area is not configured")
    path = experiment_path(args.area)
    if args.command == "train":
        if path is not None:
            raise ValueError("Pending experiment exists; keep or archive it explicitly first")
        active = await select_active_model(args.area)
        if active is None:
            raise ModelUnavailableError(f"No usable active model for {args.area}")
        active_version = active[0]
        del active
        cutoff = datetime.now(UTC)
        frame = await asyncio.to_thread(
            build_feature_matrix,
            args.area,
            cutoff - timedelta(days=settings.training_window_days),
            cutoff,
        )
        path = await asyncio.to_thread(train_experiment, args.area, active_version, frame)
        print(path)
    elif path is None:
        raise ValueError("No pending experiment for this area")
    elif args.command == "capture":
        issued = datetime.now(UTC)
        features, _ = await asyncio.to_thread(
            _build_forecast_features, args.area, issued, DEFAULT_HORIZON_STEPS
        )
        await capture_shadow(args.area, issued, features)
        print(path / "snapshots")
    elif args.command == "reproduce":
        print(json.dumps(await asyncio.to_thread(reproduce, path), indent=2, allow_nan=False))
    elif args.command == "report":
        print(json.dumps(await evaluate_experiment(path), indent=2, allow_nan=False))
    else:
        print(
            json.dumps(
                await approve_runtime(
                    path,
                    args.memory_budget_mib,
                    args.ingestion_wait_seconds,
                    args.boundary_reviewed,
                    args.training_on_target_reviewed,
                ),
                indent=2,
                allow_nan=False,
            )
        )


async def main(args):
    async def acquire(connection):
        acquired = (
            await connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": JOB_PROCESS_LOCK}
            )
        ).scalar_one()
        if not acquired:
            raise RuntimeError("A worker job is still running; stop it before using this CLI")

    async def release(connection):
        await connection.execute(
            text("SELECT pg_advisory_unlock(:key)"), {"key": JOB_PROCESS_LOCK}
        )

    try:
        async with worker_lock():
            await run_with_worker_lock(acquire)
            try:
                await _run_command(args)
            finally:
                # Native fitting outlives coroutine cancellation; retain both locks until it stops.
                drain = asyncio.create_task(asyncio.get_running_loop().shutdown_default_executor())
                while not drain.done():
                    try:
                        await asyncio.shield(drain)
                    except asyncio.CancelledError:
                        continue
                await drain
                await run_with_worker_lock(release)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["train", "capture", "report", "reproduce", "approve-runtime"]
    )
    parser.add_argument("area")
    parser.add_argument("--memory-budget-mib", type=float)
    parser.add_argument("--ingestion-wait-seconds", type=float)
    parser.add_argument("--boundary-reviewed", action="store_true")
    parser.add_argument("--training-on-target-reviewed", action="store_true")
    args = parser.parse_args()
    if args.command == "approve-runtime" and (
        args.memory_budget_mib is None or args.ingestion_wait_seconds is None
    ):
        parser.error("approve-runtime requires a memory budget and measured ingestion wait")
    asyncio.run(main(args))
