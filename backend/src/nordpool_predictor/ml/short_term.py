"""A frozen, prospective comparison; no production activation without evidence."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import logging
import os
import platform
import resource
import socket
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import joblib
import lightgbm as lgb
import numpy as np
import optuna
import pandas as pd
from sklearn.model_selection import TimeSeriesSplit
from sqlalchemy import text

from nordpool_predictor.config import get_settings
from nordpool_predictor.database import get_session
from nordpool_predictor.ml.artifacts import (
    clear_model_cache,
    load_models,
    write_bundle,
    write_manifest,
)
from nordpool_predictor.ml.features import HORIZON_STEP_FEATURE, TARGET, build_feature_matrix
from nordpool_predictor.ml.horizon import DEFAULT_HORIZON_STEPS, SHORT_HORIZON_STEPS

logger = logging.getLogger(__name__)
QUANTILES = {"p10": 0.1, "p50": 0.5, "p90": 0.9}
POLICY = {
    "min_dates": 28,
    "min_bucket_samples": 1000,
    "mae_ratio": 0.95,
    "bucket_ratio": 1.05,
    "bias_increase": 0.02,
    "pinball_ratio": 1.05,
    "coverage_decrease": 0.05,
}
CAPTURE_WINDOW = timedelta(seconds=60)


def _prospective(issued: datetime, started: datetime, finished: datetime) -> bool:
    return issued <= started <= finished <= issued + CAPTURE_WINDOW


def _scheduled_origin(value, issued: datetime) -> datetime | None:
    if value is None:
        return None
    origin = pd.Timestamp(value).to_pydatetime()
    if origin.tzinfo is None:
        return None
    local = origin.astimezone(ZoneInfo("Europe/Copenhagen"))
    if (
        local.hour not in (0, 6, 12, 18)
        or local.minute != 5
        or local.second
        or local.microsecond
        or not origin <= issued < local + timedelta(hours=6)
    ):
        return None
    return origin.astimezone(UTC)


def _slot_file(origin: datetime) -> str:
    return f"{origin.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"


def _expected_origins(metadata: dict) -> set[datetime]:
    start, end = (
        pd.Timestamp(metadata[key]).to_pydatetime() for key in ("test_start", "test_end")
    )
    local = start.astimezone(ZoneInfo("Europe/Copenhagen"))
    result = set()
    while local < end:
        for hour in (0, 6, 12, 18):
            origin = datetime.combine(
                local.date(), dtime(hour, 5), tzinfo=ZoneInfo("Europe/Copenhagen")
            )
            if start <= origin < end:
                result.add(origin.astimezone(UTC))
        local += timedelta(days=1)
    return result


def _write_once(path: Path, value: dict) -> None:
    """Publish a whole immutable JSON file, including after process cancellation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x") as handle:
            json.dump(value, handle, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def experiment_path(area: str) -> Path | None:
    root = Path(get_settings().model_dir).resolve()
    if Path(area).name != area:
        raise ValueError("Invalid area")
    pointer = root / "short-term" / area / "pending.json"
    if not pointer.exists():
        return None
    path = (pointer.parent / json.loads(pointer.read_text())["experiment"]).resolve()
    if not path.is_relative_to(pointer.parent.resolve()) or path == pointer.parent.resolve():
        raise ValueError("Experiment path escapes its area directory")
    return path


def _input_coverage(frame: pd.DataFrame) -> dict:
    return {column: float(frame[column].notna().mean()) for column in frame.columns}


def _coverage_by_horizon(frame: pd.DataFrame) -> dict:
    return {
        name: _input_coverage(frame.loc[mask])
        for name, mask in (
            ("0_24", frame[HORIZON_STEP_FEATURE] <= 96),
            ("24_48", frame[HORIZON_STEP_FEATURE].between(97, 192)),
            ("48_192", frame[HORIZON_STEP_FEATURE] > 192),
        )
        if mask.any()
    }


def _archive_features(frame: pd.DataFrame) -> dict:
    return {
        "index": [ts.isoformat() for ts in frame.index],
        "columns": list(frame.columns),
        "data": [
            [
                None
                if pd.isna(value)
                else value.item()
                if isinstance(value, np.generic)
                else value
                for value in row
            ]
            for row in frame.itertuples(index=False, name=None)
        ],
    }


def _peak_mib() -> float:
    divisor = 1024 * 1024 if platform.system() == "Darwin" else 1024
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / divisor


def _shared_folds(long: pd.DataFrame, short: pd.DataFrame, splits: int) -> list:
    folds = []
    for train, validation in TimeSeriesSplit(n_splits=splits).split(long):
        earliest = min(
            (
                frame.index[validation]
                - pd.to_timedelta(
                    frame.iloc[validation][HORIZON_STEP_FEATURE].to_numpy() * 15, unit="min"
                )
            ).min()
            for frame in (long, short)
        )
        train = train[long.index[train] < earliest]
        if len(train) < 100:
            raise ValueError("Insufficient shared training history after issuance purge")
        folds.append((train, validation))
    return folds


def train_experiment(area: str, active_version: str, long_frame: pd.DataFrame) -> Path:
    """Freeze both models before prospective test begins; never touch registrations."""
    from nordpool_predictor.ml.train import N_CV_SPLITS, N_OPTUNA_TRIALS, _objective

    started = time.monotonic()
    settings = get_settings()
    cutoff = datetime.now(UTC)
    short_frame = build_feature_matrix(
        area,
        cutoff - timedelta(days=settings.training_window_days),
        cutoff,
        horizon_steps=SHORT_HORIZON_STEPS,
    ).dropna(subset=[TARGET])
    common = long_frame.index.intersection(short_frame.index).sort_values()
    common = common[common < cutoff]
    long_frame, short_frame = long_frame.loc[common], short_frame.loc[common]
    if len(common) < 100:
        raise ValueError("Insufficient paired training data")
    folds = _shared_folds(long_frame, short_frame, N_CV_SPLITS)
    identifier = f"experiment-{area}-{uuid.uuid4().hex}"
    path = Path(settings.model_dir).resolve() / "short-term" / area / identifier
    path.mkdir(parents=True)
    components = {}
    for role, frame in (("long", long_frame), ("short", short_frame)):
        X, y = frame.drop(columns=[TARGET]), frame[TARGET]
        study = optuna.create_study(
            direction="minimize", sampler=optuna.samplers.TPESampler(seed=42)
        )
        study.optimize(lambda trial: _objective(trial, X, y, folds), n_trials=N_OPTUNA_TRIALS)
        models, files = {}, {}
        version = f"{identifier}-{role}"
        for name, alpha in QUANTILES.items():
            model = lgb.LGBMRegressor(
                objective="quantile",
                alpha=alpha,
                verbosity=-1,
                n_jobs=1,
                random_state=42,
                **study.best_params,
            )
            model.fit(X, y)
            filename = f"{version}-{name}.joblib"
            joblib.dump(model, path / filename)
            models[name], files[name] = model, filename
        manifest = write_manifest(path, version, area, files, models["p50"])
        training_file = path / f"training-{role}.joblib"
        joblib.dump(frame, training_file)
        components[role] = {
            "features": list(X.columns),
            "training_frame": training_file.name,
            "training_sha256": hashlib.sha256(training_file.read_bytes()).hexdigest(),
            "manifest_sha256": hashlib.sha256((path / manifest).read_bytes()).hexdigest(),
            "sha256": {
                name: hashlib.sha256((path / filename).read_bytes()).hexdigest()
                for name, filename in files.items()
            },
            "version": version,
            "manifest": manifest,
            "params": study.best_params,
            "coverage": _input_coverage(X),
            "coverage_by_horizon": _coverage_by_horizon(X),
            "cv_mae": study.best_value,
            "horizon_steps": DEFAULT_HORIZON_STEPS if role == "long" else SHORT_HORIZON_STEPS,
        }
    source = Path(__file__).parent
    digest = hashlib.sha256()
    for file in sorted(source.glob("*.py")):
        digest.update(file.name.encode())
        digest.update(file.read_bytes())
    test_start = (
        pd.Timestamp(datetime.now(UTC)).tz_convert("Europe/Copenhagen").normalize()
        + pd.DateOffset(days=1)
    ).tz_convert("UTC")
    test_end = (test_start.tz_convert("Europe/Copenhagen") + pd.DateOffset(days=28)).tz_convert(
        "UTC"
    )
    try:
        commit = (
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                cwd=Path(__file__).parent,
                check=False,
            ).stdout.strip()
            or None
        )
    except OSError:
        commit = None
    metadata = {
        "format_version": 1,
        "experiment": identifier,
        "area": area,
        "base_model_version": active_version,
        "policy": POLICY,
        "test_start": test_start.isoformat(),
        "test_end": test_end.isoformat(),
        "seed": 42,
        "dependencies": {
            package: importlib.metadata.version(package)
            for package in ("lightgbm", "numpy", "pandas", "scikit-learn", "optuna", "sqlalchemy")
        },
        "training_start": common.min().isoformat(),
        "training_end": common.max().isoformat(),
        "training_samples": len(common),
        "source_sha256": digest.hexdigest(),
        "git_commit": commit,
        "components": components,
        "training_runtime": {
            "host": socket.gethostname(),
            "platform": platform.platform(),
            "seconds": time.monotonic() - started,
            "peak_mib": _peak_mib(),
        },
        "availability": "prospective captured inputs only; historic replay is exploratory",
    }
    _write_once(path / "experiment.json", metadata)
    _write_once(path.parent / "pending.json", {"experiment": identifier})
    return path


def reproduce(path: Path) -> dict:
    """Refit exact frozen inputs/parameters, without replacing any experiment artifacts."""
    metadata = json.loads((path / "experiment.json").read_text())
    components = _components(path, metadata)
    result = {}
    for role, reference in zip(("long", "short"), components, strict=True):
        component = metadata["components"][role]
        frame = joblib.load(path / component["training_frame"])
        features, target = frame.drop(columns=[TARGET]), frame[TARGET]
        differences = {}
        for name, alpha in QUANTILES.items():
            model = lgb.LGBMRegressor(
                objective="quantile",
                alpha=alpha,
                verbosity=-1,
                n_jobs=1,
                random_state=metadata["seed"],
                **component["params"],
            )
            model.fit(features, target)
            differences[name] = float(
                np.max(np.abs(model.predict(features) - reference[name].predict(features)))
            )
        result[role] = differences
    return {"experiment": metadata["experiment"], "max_prediction_difference": result}


def _components(path: Path, metadata: dict) -> tuple[dict, dict]:
    result = []
    for role in ("long", "short"):
        component = metadata["components"][role]
        training = (path / component["training_frame"]).resolve()
        if not training.is_relative_to(path.resolve()) or (
            hashlib.sha256(training.read_bytes()).hexdigest() != component["training_sha256"]
        ):
            raise ValueError("Frozen training input changed")
        if (
            hashlib.sha256((path / component["manifest"]).read_bytes()).hexdigest()
            != (component["manifest_sha256"])
        ):
            raise ValueError("Frozen experiment manifest changed")
        manifest = json.loads((path / component["manifest"]).read_text())
        for quantile in QUANTILES:
            artifact = (path / manifest["quantiles"][quantile]).resolve()
            if not artifact.is_relative_to(path.resolve()):
                raise ValueError("Experiment artifact escapes its directory")
            if hashlib.sha256(artifact.read_bytes()).hexdigest() != component["sha256"][quantile]:
                raise ValueError("Frozen experiment artifact changed")
        result.append(
            load_models(component["version"], str(path / component["manifest"]), metadata["area"])
        )
        if any(list(result[-1][q].feature_name_) != component["features"] for q in QUANTILES):
            raise ValueError("Frozen experiment feature contract changed")
    return tuple(result)


async def capture_shadow(
    area: str,
    issued_at: datetime,
    features: pd.DataFrame,
    production_model_version: str | None = None,
    scheduled_origin: datetime | None = None,
) -> None:
    """Archive this issuance's inputs and pair, outside all production-run tables."""
    from nordpool_predictor.ml.predict import _predict_quantiles

    started_at = datetime.now(UTC)
    path = experiment_path(area)
    if path is None:
        return
    metadata = json.loads((path / "experiment.json").read_text())
    if not (
        datetime.fromisoformat(metadata["test_start"])
        <= issued_at
        < datetime.fromisoformat(metadata["test_end"])
    ):
        return
    origin = _scheduled_origin(scheduled_origin, issued_at)
    exploratory = (
        path / "exploratory" / f"{issued_at.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex}.json"
    )
    target = path / "snapshots" / _slot_file(origin) if origin is not None else exploratory
    if target.exists():
        return
    long, short = await asyncio.to_thread(_components, path, metadata)
    baseline = np.sort(
        np.stack(await asyncio.to_thread(_predict_quantiles, long, features), axis=1), axis=1
    )
    candidate = np.sort(
        np.stack(
            await asyncio.to_thread(_predict_quantiles, {**long, "short": short}, features), axis=1
        ),
        axis=1,
    )
    async with get_session() as session:
        result = await session.execute(
            text("""SELECT ts, price_dkk_kwh FROM price_observations
                    WHERE area = :area AND ts > :issued AND ts <= :end
                      AND created_at <= :issued ORDER BY ts"""),
            {"area": area, "issued": issued_at, "end": features.index.max().to_pydatetime()},
        )
        published = {
            row["ts"].isoformat(): row["price_dkk_kwh"] for row in result.mappings().all()
        }
    finished_at = datetime.now(UTC)
    near_live = _prospective(issued_at, started_at, finished_at)
    if not near_live:
        target = exploratory
    _write_once(
        target,
        {
            "experiment": metadata["experiment"],
            "area": area,
            "issued_at": issued_at.isoformat(),
            "capture_started_at": started_at.isoformat(),
            "captured_at": finished_at.isoformat(),
            "availability": "captured"
            if near_live and origin is not None
            else "exploratory"
            if near_live
            else "reconstructed",
            "scheduled_origin": origin.isoformat() if origin is not None else None,
            "issuance_delay_seconds": (issued_at - origin).total_seconds()
            if origin is not None
            else None,
            "host": socket.gethostname(),
            "production_model_version": production_model_version,
            "peak_mib": _peak_mib(),
            "components_sha256": {role: c["sha256"] for role, c in metadata["components"].items()},
            "metadata_sha256": hashlib.sha256((path / "experiment.json").read_bytes()).hexdigest(),
            "coverage_by_horizon": _coverage_by_horizon(features),
            "features": _archive_features(features),
            "published_prices": published,
            "rows": [
                {
                    "ts": ts.isoformat(),
                    "horizon_step": i + 1,
                    "baseline": baseline[i].tolist(),
                    "candidate": candidate[i].tolist(),
                }
                for i, ts in enumerate(features.index)
            ],
            "boundary": {
                "baseline_jump": float(baseline[192, 1] - baseline[191, 1]),
                "candidate_jump": float(candidate[192, 1] - candidate[191, 1]),
            }
            if len(features) > SHORT_HORIZON_STEPS
            else None,
        },
    )


def _metrics(rows: list[dict], model: str) -> dict:
    observed = np.asarray([row["actual"] for row in rows])
    predictions = np.asarray([row[model] for row in rows])
    errors = predictions[:, 1] - observed
    return {
        "samples": len(rows),
        "mae": float(np.mean(abs(errors))),
        "rmse": float(np.sqrt(np.mean(errors**2))),
        "bias": float(np.mean(errors)),
        "coverage": float(
            np.mean((observed >= predictions[:, 0]) & (observed <= predictions[:, 2]))
        ),
        "pinball": {
            name: float(
                np.mean(
                    np.maximum(
                        alpha * (observed - predictions[:, i]),
                        (alpha - 1) * (observed - predictions[:, i]),
                    )
                )
            )
            for i, (name, alpha) in enumerate(QUANTILES.items())
        },
    }


def evaluate_rows(snapshots: list[dict], policy: dict = POLICY) -> dict:
    """Score identical unknown targets, with hard minimum coverage and quality gates."""
    paired, dates, daily, seen, origins = [], set(), {}, set(), set()
    for snapshot in sorted(snapshots, key=lambda snapshot: snapshot["issued_at"]):
        issued = pd.Timestamp(snapshot["issued_at"])
        origin = _scheduled_origin(snapshot.get("scheduled_origin"), issued.to_pydatetime())
        if snapshot.get("availability") != "captured" or origin is None or origin in origins:
            continue
        origins.add(origin)
        date = issued.tz_convert("Europe/Copenhagen").date().isoformat()
        known = {pd.Timestamp(ts) for ts in snapshot["published_prices"]}
        rows = [
            row
            for row in snapshot["rows"]
            if 1 <= row["horizon_step"] <= SHORT_HORIZON_STEPS
            and pd.Timestamp(row["ts"]) not in known
            and "actual" in row
        ]
        if not rows:
            continue
        anchor = issued.floor("15min")
        for row in rows:
            ts = pd.Timestamp(row["ts"])
            key = (issued, ts)
            if key in seen or ts != anchor + pd.Timedelta(minutes=row["horizon_step"] * 15):
                return {"status": "invalid_data", "reasons": ["Duplicate or off-grid target"]}
            seen.add(key)
        numbers = np.asarray(
            [[row["actual"], *row["baseline"], *row["candidate"]] for row in rows]
        )
        if numbers.shape[1] != 7 or not np.isfinite(numbers).all():
            return {"status": "invalid_data", "reasons": ["Non-finite or malformed prices"]}
        if any(
            row[model] != sorted(row[model]) for row in rows for model in ("baseline", "candidate")
        ):
            return {"status": "invalid_data", "reasons": ["Crossing quantiles"]}
        paired.extend(rows)
        dates.add(date)
        daily.setdefault(date, []).extend(rows)
    groups = {
        "0_24": [r for r in paired if r["horizon_step"] <= 96],
        "24_48": [r for r in paired if r["horizon_step"] > 96],
        "0_48": paired,
    }
    if len(dates) < policy["min_dates"] or any(
        len(groups[bucket]) < policy["min_bucket_samples"] for bucket in ("0_24", "24_48")
    ):
        return {
            "status": "insufficient_data",
            "dates": len(dates),
            "samples": {name: len(rows) for name, rows in groups.items()},
        }
    metrics = {
        name: {model: _metrics(rows, model) for model in ("baseline", "candidate")}
        for name, rows in groups.items()
    }
    if any(
        not np.isfinite(
            [
                values[model]["mae"],
                values[model]["rmse"],
                values[model]["bias"],
                values[model]["coverage"],
                *values[model]["pinball"].values(),
            ]
        ).all()
        for values in metrics.values()
        for model in ("baseline", "candidate")
    ):
        return {"status": "invalid_data", "reasons": ["Non-finite evaluation metrics"]}
    reasons = []
    for name, values in metrics.items():
        baseline, candidate = values["baseline"], values["candidate"]
        ratio = policy["mae_ratio"] if name == "0_48" else policy["bucket_ratio"]
        if candidate["mae"] > baseline["mae"] * ratio:
            reasons.append(f"{name}: MAE")
        if abs(candidate["bias"]) > abs(baseline["bias"]) + policy["bias_increase"]:
            reasons.append(f"{name}: bias")
        if candidate["coverage"] < baseline["coverage"] - policy["coverage_decrease"]:
            reasons.append(f"{name}: interval coverage")
        for quantile in QUANTILES:
            if candidate["pinball"][quantile] > (
                baseline["pinball"][quantile] * policy["pinball_ratio"]
            ):
                reasons.append(f"{name}: {quantile} pinball")
    return {
        "status": "rejected" if reasons else "accepted",
        "reasons": reasons,
        "dates": len(dates),
        "metrics": metrics,
        "daily": {
            date: {model: _metrics(rows, model) for model in ("baseline", "candidate")}
            for date, rows in daily.items()
        },
    }


async def evaluate_experiment(path: Path) -> dict:
    """Freeze settled outcomes separately; never rewrite issuance snapshots."""
    metadata = json.loads((path / "experiment.json").read_text())
    snapshots = []
    evidence = hashlib.sha256()
    expected_origins = _expected_origins(metadata)
    captured_origins, settled_origins = set(), set()
    await asyncio.to_thread(_components, path, metadata)
    for file in sorted((path / "snapshots").glob("*.json")):
        snapshot = json.loads(file.read_text())
        issued = datetime.fromisoformat(snapshot["issued_at"])
        origin = _scheduled_origin(snapshot.get("scheduled_origin"), issued)
        if (
            snapshot.get("availability") != "captured"
            or origin not in expected_origins
            or file.name != _slot_file(origin)
        ):
            continue
        captured_origins.add(origin)
        identities = {
            role: component["sha256"] for role, component in metadata["components"].items()
        }
        if (
            snapshot["experiment"] != metadata["experiment"]
            or snapshot["area"] != metadata["area"]
            or snapshot.get("components_sha256") != identities
            or issued < datetime.fromisoformat(metadata["test_start"])
            or issued >= datetime.fromisoformat(metadata["test_end"])
            or snapshot.get("metadata_sha256")
            != hashlib.sha256((path / "experiment.json").read_bytes()).hexdigest()
        ):
            raise ValueError("Snapshot does not belong to the frozen experiment")
        if snapshot.get("availability") == "captured" and not _prospective(
            issued,
            datetime.fromisoformat(snapshot["capture_started_at"]),
            datetime.fromisoformat(snapshot["captured_at"]),
        ):
            raise ValueError("Snapshot is not a verified near-live capture")
        if datetime.now(UTC) < issued + timedelta(hours=48):
            continue
        outcome = path / "outcomes" / file.name
        if not outcome.exists():
            async with get_session() as session:
                result = await session.execute(
                    text("""SELECT ts, price_dkk_kwh FROM price_observations
                            WHERE area = :area AND ts > :issued AND ts <= :end"""),
                    {
                        "area": metadata["area"],
                        "issued": issued,
                        "end": issued + timedelta(hours=48),
                    },
                )
                actual = {
                    row["ts"].isoformat(): row["price_dkk_kwh"] for row in result.mappings().all()
                }
            expected = [row for row in snapshot["rows"] if row["horizon_step"] <= 192]
            if any(row["ts"] not in actual for row in expected):
                continue
            _write_once(
                outcome,
                {
                    "actual": actual,
                    "scored_at": datetime.now(UTC).isoformat(),
                    "snapshot_sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
                },
            )
        observed = json.loads(outcome.read_text())
        if observed["snapshot_sha256"] != hashlib.sha256(file.read_bytes()).hexdigest():
            raise ValueError("Issuance snapshot was modified after scoring")
        expected = [row for row in snapshot["rows"] if row["horizon_step"] <= SHORT_HORIZON_STEPS]
        if any(row["ts"] not in observed["actual"] for row in expected):
            continue
        evidence.update(file.name.encode())
        evidence.update(hashlib.sha256(file.read_bytes()).digest())
        evidence.update(hashlib.sha256(outcome.read_bytes()).digest())
        for row in snapshot["rows"]:
            if row["ts"] in observed["actual"]:
                row["actual"] = observed["actual"][row["ts"]]
        snapshots.append(snapshot)
        settled_origins.add(origin)
    if metadata["policy"] != POLICY:
        raise ValueError("Experiment acceptance policy changed")
    result = evaluate_rows(snapshots, metadata["policy"])
    if result["status"] == "accepted" and datetime.now(UTC) < datetime.fromisoformat(
        metadata["test_end"]
    ) + timedelta(hours=48):
        result["status"] = "insufficient_data"
    result["missing_origins"] = sorted(
        origin.isoformat() for origin in expected_origins - captured_origins
    )
    result["unsettled_origins"] = sorted(
        origin.isoformat() for origin in captured_origins - settled_origins
    )
    if result["missing_origins"] or result["unsettled_origins"]:
        result["status"] = "insufficient_data"
    result["evidence_sha256"] = evidence.hexdigest()
    result["experiment"] = metadata["experiment"]
    result["components"] = metadata["components"]
    result["training_end"] = metadata["training_end"]
    result["metadata_sha256"] = hashlib.sha256((path / "experiment.json").read_bytes()).hexdigest()
    result["input_coverage"] = [
        {"issued_at": s["issued_at"], "coverage_by_horizon": s.get("coverage_by_horizon", {})}
        for s in snapshots
    ]
    result["boundary_diagnostics"] = [
        {"issued_at": s["issued_at"], "boundary": s.get("boundary")} for s in snapshots
    ]
    runtime = path / "runtime-approval.json"
    approval = json.loads(runtime.read_text()) if runtime.exists() else {}
    runtime_numbers = [
        approval.get(key, float("nan"))
        for key in (
            "peak_mib",
            "child_peak_mib",
            "worker_parent_peak_mib",
            "memory_budget_mib",
            "ingestion_wait_seconds",
        )
    ]
    result["runtime_approved"] = (
        all(type(value) in (int, float) and np.isfinite(value) for value in runtime_numbers)
        and approval.get("child_peak_mib", -1) >= 0
        and approval.get("worker_parent_peak_mib", 0) > 0
        and approval.get("peak_mib", -1)
        >= (
            approval.get("child_peak_mib", float("inf"))
            + approval.get("worker_parent_peak_mib", float("inf"))
        )
        and approval.get("memory_budget_mib", 0) > 0
        and approval.get("approved") is True
        and approval.get("boundary_reviewed") is True
        and approval.get("training_on_target_reviewed") is True
        and approval.get("metadata_sha256") == result["metadata_sha256"]
        and approval.get("evidence_sha256") == result["evidence_sha256"]
        and approval.get("production_model_version") == metadata["base_model_version"]
        and approval.get("peak_mib", float("inf")) <= approval.get("memory_budget_mib", 0)
        and approval.get("ingestion_wait_seconds", -1) >= 0
    )
    if result["status"] == "accepted" and not result["runtime_approved"]:
        result["status"] = "awaiting_runtime_approval"
    return result


async def approve_runtime(
    path: Path,
    memory_budget_mib: float,
    ingestion_wait_seconds: float,
    boundary_reviewed: bool,
    training_on_target_reviewed: bool,
    worker_parent_peak_mib: float | None = None,
) -> dict:
    """Run on the target NAS after checking queue delay and boundary diagnostics."""
    from nordpool_predictor.ml.predict import (
        _build_forecast_features,
        _predict_quantiles,
        select_active_model,
    )

    if (
        not np.isfinite([memory_budget_mib, ingestion_wait_seconds]).all()
        or memory_budget_mib <= 0
        or ingestion_wait_seconds < 0
        or not boundary_reviewed
        or not training_on_target_reviewed
    ):
        raise ValueError(
            "Positive memory budget, measured queue delay and boundary review required"
        )
    if (
        worker_parent_peak_mib is None
        or not np.isfinite(worker_parent_peak_mib)
        or worker_parent_peak_mib <= 0
    ):
        raise ValueError("Measured worker-parent peak RSS in MiB is required")
    metadata = json.loads((path / "experiment.json").read_text())
    active = await select_active_model(metadata["area"])
    if active is None or active[0] != metadata["base_model_version"]:
        raise ValueError("Runtime measurement requires the experiment's unchanged active version")
    active_version, production = active
    report = await evaluate_experiment(path)
    if report["status"] not in {"accepted", "awaiting_runtime_approval"}:
        raise ValueError("Complete the fixed prospective holdout before runtime/boundary approval")
    started = time.monotonic()
    long, short = await asyncio.to_thread(_components, path, metadata)
    features, _ = await asyncio.to_thread(
        _build_forecast_features, metadata["area"], datetime.now(UTC), DEFAULT_HORIZON_STEPS
    )
    await asyncio.to_thread(_predict_quantiles, production, features)
    await asyncio.to_thread(_predict_quantiles, long, features)
    await asyncio.to_thread(_predict_quantiles, {**long, "short": short}, features)
    child_peak_mib = max(_peak_mib(), metadata["training_runtime"]["peak_mib"])
    approval = {
        "host": socket.gethostname(),
        "training_host": metadata["training_runtime"]["host"],
        "production_model_version": active_version,
        "training_on_target_reviewed": True,
        "metadata_sha256": hashlib.sha256((path / "experiment.json").read_bytes()).hexdigest(),
        "evidence_sha256": report["evidence_sha256"],
        "memory_budget_mib": memory_budget_mib,
        "child_peak_mib": child_peak_mib,
        "worker_parent_peak_mib": worker_parent_peak_mib,
        "peak_mib": child_peak_mib + worker_parent_peak_mib,
        "training_seconds": metadata["training_runtime"]["seconds"],
        "inference_seconds": time.monotonic() - started,
        "ingestion_wait_seconds": ingestion_wait_seconds,
        "boundary_reviewed": True,
        "approved": True,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    if approval["peak_mib"] > memory_budget_mib:
        raise ValueError("Measured peak memory exceeds target NAS budget")
    _write_once(path / "runtime-approval.json", approval)
    return approval


async def start_or_promote(
    area: str, active_version: str, frame: pd.DataFrame | None
) -> tuple | None:
    """Called by existing retraining job; returning None preserves its active version."""
    import asyncio

    path = experiment_path(area)
    if path is None:
        if frame is None:
            raise ValueError("Training data is required to start a new experiment")
        await asyncio.to_thread(train_experiment, area, active_version, frame)
        logger.info(
            "Started prospective short-term experiment for %s; active model preserved", area
        )
        return None
    del frame
    metadata = json.loads((path / "experiment.json").read_text())
    if metadata["base_model_version"] != active_version:
        raise ValueError(
            "Active version changed; archive pending experiment before starting another"
        )
    report = await evaluate_experiment(path)
    _write_once(path / "reports" / f"{uuid.uuid4().hex}.json", report)
    if report["status"] != "accepted":
        logger.info(
            "Short-term experiment for %s: %s; active model preserved", area, report["status"]
        )
        return None
    long, short = await asyncio.to_thread(_components, path, metadata)
    version = f"lgbm-{area}-paired-{uuid.uuid4().hex}"
    artifact = await asyncio.to_thread(
        write_bundle,
        Path(get_settings().model_dir),
        version,
        area,
        long,
        short,
        {"experiment": metadata, "evaluation": report},
    )
    # Loading the finished package validates all six files before the DB transaction.
    del long, short
    await asyncio.to_thread(clear_model_cache)
    await asyncio.to_thread(load_models, version, artifact, area)
    return (
        version,
        artifact,
        {"short_term_evaluation": report, "components": metadata["components"]},
        path,
    )
