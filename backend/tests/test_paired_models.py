from __future__ import annotations

import json
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
import time_machine
from sqlalchemy import text

from nordpool_predictor.ml import artifacts, features, predict, short_term
from nordpool_predictor.ml.horizon import SHORT_HORIZON_STEPS
from nordpool_predictor.scheduler import bootstrap


class ConstantModel:
    def __init__(self, value, columns):
        self.value = value
        self.feature_name_ = columns
        self.feature_importances_ = np.ones(len(columns))

    def predict(self, frame):
        assert list(frame.columns) == self.feature_name_
        return np.full(len(frame), self.value)


def component(value, columns):
    return {q: ConstantModel(value, columns) for q in artifacts.QUANTILES}


@pytest.fixture
def model_root(tmp_path, test_settings):
    test_settings.model_dir = str(tmp_path)
    artifacts._load_quantile.cache_clear()
    with (
        patch.object(artifacts, "get_settings", return_value=test_settings),
        patch.object(short_term, "get_settings", return_value=test_settings),
    ):
        yield tmp_path
    artifacts._load_quantile.cache_clear()


def test_bundle_owns_all_six_files_and_keeps_long_diagnostics(model_root):
    long, short = component(1, ["old"]), component(2, ["new"])
    path = artifacts.write_bundle(model_root, "paired", "DK1", long, short, {"source": "frozen"})
    long["p50"].value = 99
    loaded = artifacts.load_models("paired", path, "DK1")
    assert loaded["p50"].value == 1
    assert loaded["short"]["p50"].value == 2
    assert len(list(model_root.glob("*.joblib"))) == 6
    assert artifacts.load_feature_importance("paired", path, "DK1") == [
        {"feature": "old", "importance": 1.0}
    ]
    with pytest.raises(ValueError, match="identity"):
        artifacts.load_models("paired", path, "DK2")


@pytest.mark.parametrize("damage", ["missing", "features", "escape", "split"])
def test_entire_bundle_is_unusable_when_a_short_component_is_invalid(model_root, damage):
    path = artifacts.write_bundle(
        model_root, "paired", "DK1", component(1, ["a"]), component(2, ["b"]), {}
    )
    file = model_root / path
    manifest = json.loads(file.read_text())
    if damage == "missing":
        (model_root / manifest["short_quantiles"]["p50"]).unlink()
    elif damage == "features":
        manifest["features"]["short"] = ["wrong"]
    elif damage == "escape":
        manifest["short_quantiles"]["p10"] = "../escape.joblib"
    else:
        manifest["short_horizon_steps"] = 193
    file.write_text(json.dumps(manifest))
    with pytest.raises((ValueError, FileNotFoundError)):
        artifacts.load_models("paired", path, "DK1")


def test_routing_aligns_each_component_without_a_boundary_gap():
    frame = pd.DataFrame({"horizon_step": np.arange(1, 769), "old": 5, "new": 7})
    models = {**component(10, ["old"]), "short": component(20, ["new"])}
    for values in predict._predict_quantiles(models, frame):
        assert values[190:193].tolist() == [20, 20, 10]
        assert len(values) == 768
        assert values[-1] == 10
    for values in predict._predict_quantiles(component(10, ["old"]), frame):
        assert values.tolist() == [10] * 768


def test_short_training_resamples_every_target_instead_of_filtering_long_rows():
    index = pd.date_range("2026-01-01", periods=3000, freq="15min", tz=UTC)
    history = pd.DataFrame({features.TARGET: np.arange(len(index), dtype=float)}, index=index)
    with ExitStack() as stack:
        stack.enter_context(patch.object(features, "load_prices", return_value=history))
        for name in (
            "add_issuance_price_features",
            "add_weather_features",
            "add_production_features",
            "add_crossborder_features",
            "add_cross_features",
            "add_residual_load_features",
        ):
            stack.enter_context(
                patch.object(features, name, side_effect=lambda frame, *a, **k: frame)
            )
        long = features.build_feature_matrix("DK1", index[0], index[-1])
        short = features.build_feature_matrix(
            "DK1", index[0], index[-1], horizon_steps=SHORT_HORIZON_STEPS
        )
    pd.testing.assert_index_equal(long.index, short.index)
    assert short.horizon_step.between(1, 192).all()
    assert (long.horizon_step > 192).any()
    assert not short.loc[short.horizon_step <= 96, "price_lag_24h"].dropna().empty


def test_tuning_folds_share_the_stricter_issuance_purge():
    index = pd.date_range("2026-01-01", periods=5000, freq="15min", tz=UTC)
    long = pd.DataFrame({"horizon_step": 768}, index=index)
    short = pd.DataFrame({"horizon_step": 192}, index=index)
    for train, validation in short_term._shared_folds(long, short, 3):
        earliest = index[validation[0]] - timedelta(days=8)
        assert index[train].max() < earliest
        assert index[validation].min() > index[train].max()


def snapshots(days=28, availability="captured"):
    result = []
    for day in range(days):
        for hour in (0, 6, 12, 18):
            issued = (
                datetime(2026, 1, 1, hour, 5, tzinfo=ZoneInfo("Europe/Copenhagen"))
                + timedelta(days=day)
            ).astimezone(UTC)
            anchor = issued.replace(minute=0)
            rows = [
                {
                    "ts": (anchor + timedelta(minutes=15 * step)).isoformat(),
                    "horizon_step": step,
                    "actual": 1.0,
                    "baseline": [0.7, 1.5, 1.8],
                    "candidate": [0.8, 1.1, 1.2],
                }
                for step in range(1, 193)
            ]
            result.append(
                {
                    "issued_at": issued.isoformat(),
                    "availability": availability,
                    "scheduled_origin": issued.isoformat(),
                    "published_prices": {rows[0]["ts"]: 1},
                    "rows": rows,
                }
            )
    return result


def test_paired_gates_exclude_known_prices_and_keep_daily_evidence():
    data = snapshots()
    for snapshot in data:
        snapshot["rows"][0]["candidate"] = [-999, 999, 1000]
    report = short_term.evaluate_rows(data)
    assert report["status"] == "accepted"
    assert report["dates"] == 28
    assert report["metrics"]["0_24"]["baseline"]["samples"] == 112 * 95
    assert report["metrics"]["0_48"]["candidate"]["mae"] == pytest.approx(0.1)
    assert len(report["daily"]) == 28


@pytest.mark.parametrize(
    "case", ["dates", "samples", "reconstructed", "duplicate", "nan", "crossing", "worse"]
)
def test_bad_or_insufficient_evidence_cannot_promote(case):
    data = snapshots()
    if case == "dates":
        del data[-4:]
    elif case == "samples":
        for snapshot in data:
            snapshot["rows"] = snapshot["rows"][90:100]
    elif case == "reconstructed":
        for snapshot in data:
            snapshot["availability"] = "reconstructed"
    elif case == "duplicate":
        data.append(data[0])
    elif case == "nan":
        data[0]["rows"][10]["actual"] = float("nan")
    elif case == "crossing":
        data[0]["rows"][10]["candidate"] = [2, 1, 3]
    else:
        for snapshot in data:
            for row in snapshot["rows"]:
                row["candidate"] = [0, 2, 3]
    if case == "duplicate":
        assert short_term.evaluate_rows(data) == short_term.evaluate_rows(data[:-1])
    else:
        assert short_term.evaluate_rows(data)["status"] != "accepted"


def test_zero_error_baseline_never_allows_regression():
    data = snapshots()
    for snapshot in data:
        for row in snapshot["rows"]:
            row["baseline"] = [1, 1, 1]
    assert short_term.evaluate_rows(data)["status"] == "rejected"
    for snapshot in data:
        for row in snapshot["rows"]:
            row["candidate"] = [1, 1, 1]
    assert short_term.evaluate_rows(data)["status"] == "accepted"


def test_snapshot_writes_are_immutable_and_experiment_paths_cannot_escape(model_root):
    file = model_root / "immutable.json"
    short_term._write_once(file, {"first": True})
    with pytest.raises(FileExistsError):
        short_term._write_once(file, {"first": False})
    assert json.loads(file.read_text()) == {"first": True}
    pointer = model_root / "short-term/DK1/pending.json"
    short_term._write_once(pointer, {"experiment": "../../../../escape"})
    with pytest.raises(ValueError, match="escapes"):
        short_term.experiment_path("DK1")


async def test_corrupt_only_active_bundle_does_not_select_inactive_predecessor(
    database, model_root
):
    from nordpool_predictor.ml.train import _has_usable_active_model

    path = artifacts.write_bundle(
        model_root, "paired", "DK1", component(1, ["a"]), component(2, ["a"]), {}
    )
    import joblib

    old_models = component(1, ["a"])
    old_paths = {}
    for quantile in artifacts.QUANTILES:
        old_paths[quantile] = f"older-{quantile}.joblib"
        joblib.dump(old_models[quantile], model_root / old_paths[quantile])
    old_manifest = artifacts.write_manifest(
        model_root, "older", "DK1", old_paths, old_models["p50"]
    )
    (model_root / "paired-short-p50.joblib").unlink()
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO model_versions (model_version, model_type, area, horizon_strategy,
                feature_set_version, trained_at, artifact_path, is_active)
            VALUES ('paired', 'test', 'DK1', 'paired_48h', 'v4-crossborder-history',
                    NOW(), :path, TRUE),
                   ('older', 'test', 'DK1', 'direct', 'v4-crossborder-history',
                    NOW(), :old_path, FALSE)
        """),
            {"path": path, "old_path": old_manifest},
        )
    with pytest.raises(predict.ModelUnavailableError):
        await _has_usable_active_model("DK1")
    with pytest.raises(predict.ModelUnavailableError):
        await bootstrap.ready_models()
    async with database.begin() as conn:
        await conn.execute(text("UPDATE model_versions SET is_active = (model_version = 'older')"))
    assert await _has_usable_active_model("DK1")
    assert await bootstrap.ready_models() == {"DK1": "older"}


@pytest.fixture
def pending_experiment(model_root):
    import hashlib
    import socket

    import joblib

    path = model_root / "short-term/DK1/test-pair"
    path.mkdir(parents=True)
    components = {}
    for role, value in (("long", 1.5), ("short", 1.1)):
        version = f"test-{role}"
        models = component(value, ["horizon_step"])
        files = {}
        for quantile in artifacts.QUANTILES:
            files[quantile] = f"{version}-{quantile}.joblib"
            joblib.dump(models[quantile], path / files[quantile])
        manifest = artifacts.write_manifest(path, version, "DK1", files, models["p50"])
        training_file = path / f"training-{role}.joblib"
        joblib.dump(pd.DataFrame({"horizon_step": [1], features.TARGET: [1.0]}), training_file)
        components[role] = {
            "training_frame": training_file.name,
            "training_sha256": hashlib.sha256(training_file.read_bytes()).hexdigest(),
            "version": version,
            "manifest": manifest,
            "features": ["horizon_step"],
            "sha256": {
                q: hashlib.sha256((path / files[q]).read_bytes()).hexdigest()
                for q in artifacts.QUANTILES
            },
            "manifest_sha256": hashlib.sha256((path / manifest).read_bytes()).hexdigest(),
        }
    metadata = {
        "experiment": "test-pair",
        "area": "DK1",
        "base_model_version": "older",
        "policy": short_term.POLICY,
        "test_start": "2025-12-31T23:00:00+00:00",
        "test_end": "2026-01-28T23:00:00+00:00",
        "training_end": "2025-12-31T22:45:00+00:00",
        "components": components,
        "training_runtime": {"host": socket.gethostname(), "seconds": 1, "peak_mib": 1},
    }
    short_term._write_once(path / "experiment.json", metadata)
    short_term._write_once(path.parent / "pending.json", {"experiment": path.name})
    return path, metadata


def test_modified_frozen_estimator_is_rejected_before_loading(pending_experiment):
    path, metadata = pending_experiment
    with (path / "test-short-p50.joblib").open("ab") as handle:
        handle.write(b"changed")
    with pytest.raises(ValueError, match="artifact changed"):
        short_term._components(path, metadata)


async def test_capture_records_receipts_without_production_rows(database, pending_experiment):
    path, _ = pending_experiment
    issued = datetime(2026, 1, 2, 5, 5, tzinfo=UTC)
    targets = pd.date_range("2026-01-02 05:15", periods=768, freq="15min", tz=UTC)
    frame = pd.DataFrame({"horizon_step": np.arange(1, 769)}, index=targets)
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO price_observations (area, ts, price_dkk_kwh, source, created_at)
            VALUES ('DK1', :ts, 0, 'test', :received)
        """),
            [
                {"ts": targets[0].to_pydatetime(), "received": issued - timedelta(seconds=1)},
                {"ts": targets[1].to_pydatetime(), "received": issued + timedelta(seconds=1)},
            ],
        )
    with time_machine.travel(issued, tick=False):
        await short_term.capture_shadow("DK1", issued, frame, scheduled_origin=issued)
    snapshot = json.loads(next((path / "snapshots").glob("*.json")).read_text())
    assert snapshot["published_prices"] == {targets[0].isoformat(): 0}
    assert snapshot["rows"][191]["candidate"] == [1.1, 1.1, 1.1]
    assert snapshot["rows"][192]["candidate"] == [1.5, 1.5, 1.5]
    assert "features" in snapshot and "components_sha256" in snapshot
    async with database.connect() as conn:
        assert (await conn.execute(text("SELECT COUNT(*) FROM forecast_runs"))).scalar_one() == 0


@pytest.fixture
def settled_experiment(pending_experiment):
    import hashlib

    path, metadata = pending_experiment
    for snapshot in snapshots():
        snapshot.update(
            {
                "capture_started_at": snapshot["issued_at"],
                "captured_at": snapshot["issued_at"],
                "experiment": metadata["experiment"],
                "area": "DK1",
                "metadata_sha256": hashlib.sha256(
                    (path / "experiment.json").read_bytes()
                ).hexdigest(),
                "components_sha256": {
                    role: c["sha256"] for role, c in metadata["components"].items()
                },
            }
        )
        actual = {row["ts"]: row.pop("actual") for row in snapshot["rows"]}
        file = (
            path
            / "snapshots"
            / short_term._slot_file(datetime.fromisoformat(snapshot["scheduled_origin"]))
        )
        short_term._write_once(file, snapshot)
        short_term._write_once(
            path / "outcomes" / file.name,
            {"actual": actual, "snapshot_sha256": hashlib.sha256(file.read_bytes()).hexdigest()},
        )
    return path, metadata


async def test_evidence_and_runtime_approval_are_required_for_promotion(settled_experiment):
    import hashlib
    import socket

    path, metadata = settled_experiment
    report = await short_term.evaluate_experiment(path)
    assert report["status"] == "awaiting_runtime_approval"
    short_term._write_once(
        path / "runtime-approval.json",
        {
            "approved": True,
            "production_model_version": "older",
            "evidence_sha256": report["evidence_sha256"],
            "training_on_target_reviewed": True,
            "metadata_sha256": hashlib.sha256((path / "experiment.json").read_bytes()).hexdigest(),
            "boundary_reviewed": True,
            "host": socket.gethostname(),
            "child_peak_mib": 75,
            "worker_parent_peak_mib": 25,
            "peak_mib": 100,
            "memory_budget_mib": 1536,
            "ingestion_wait_seconds": 12,
        },
    )
    report = await short_term.evaluate_experiment(path)
    assert report["status"] == "accepted"
    version, manifest, metrics, returned_path = await short_term.start_or_promote(
        "DK1", "older", pd.DataFrame()
    )
    assert returned_path == path
    assert metrics["short_term_evaluation"]["status"] == "accepted"
    assert "short" in artifacts.load_models(version, manifest, "DK1")
    assert artifacts._load_quantile.cache_info().currsize == 6
    # No database registration has been attempted by building a validated package.
    assert short_term.experiment_path("DK1") == path


async def test_retraining_keeps_the_active_version_when_evidence_is_insufficient(
    database,
    pending_experiment,
):
    from nordpool_predictor.ml import train

    path, _ = pending_experiment
    # The existing long-only version is a real, loadable artifact.
    metadata = json.loads((path / "experiment.json").read_text())
    active = metadata["components"]["long"]
    active["version"] = "test-long"
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO model_versions (model_version, model_type, area, horizon_strategy,
                feature_set_version, trained_at, artifact_path, is_active)
            VALUES ('older', 'test', 'DK1', 'direct', 'v4-crossborder-history', NOW(), :path, TRUE)
        """),
            {"path": str(path / active["manifest"])},
        )
    # Candidate identity is checked before production writes; use a compatible old manifest.
    manifest_file = path / "older.json"
    original = json.loads((path / active["manifest"]).read_text())
    original["model_version"] = "older"
    manifest_file.write_text(json.dumps(original))
    async with database.begin() as conn:
        await conn.execute(
            text("UPDATE model_versions SET artifact_path = :path"), {"path": str(manifest_file)}
        )
    with patch.object(train, "prepare_training_data", AsyncMock(return_value=pd.DataFrame())):
        assert await train.train_models("DK1") == "older"
    async with database.connect() as conn:
        assert (
            await conn.execute(
                text("SELECT model_version FROM model_versions WHERE is_active = TRUE")
            )
        ).scalar_one() == "older"
        assert (await conn.execute(text("SELECT COUNT(*) FROM model_versions"))).scalar_one() == 1


async def test_backdated_capture_is_archived_as_exploratory(
    pending_experiment,
    mock_db_session,
):
    path, _ = pending_experiment
    factory, _ = mock_db_session
    issued = datetime(2026, 1, 2, 0, 5, tzinfo=UTC)
    targets = pd.date_range("2026-01-02 00:15", periods=768, freq="15min", tz=UTC)
    frame = pd.DataFrame({"horizon_step": np.arange(1, 769)}, index=targets)
    with (
        time_machine.travel(issued + timedelta(days=3), tick=False),
        patch.object(short_term, "get_session", factory),
    ):
        await short_term.capture_shadow("DK1", issued, frame)
    snapshot = json.loads(next((path / "exploratory").glob("*.json")).read_text())
    assert snapshot["availability"] == "reconstructed"
    assert short_term.evaluate_rows([snapshot])["status"] == "insufficient_data"


@pytest.mark.parametrize("git_available", [True, False])
def test_experiment_freezes_both_resampled_training_sets_before_testing(model_root, git_available):
    from nordpool_predictor.ml import train

    index = pd.date_range("2026-01-01", periods=5000, freq="15min", tz=UTC)
    long = pd.DataFrame(
        {
            "horizon_step": np.arange(5000) % 768 + 1,
            "price_rolling_mean_24h": 1.0,
            features.TARGET: np.sin(np.arange(5000) / 96),
        },
        index=index,
    )
    short = long.copy()
    short["horizon_step"] = np.arange(5000) % 192 + 1
    with (
        time_machine.travel(datetime(2026, 3, 28, 18, 0, tzinfo=UTC), tick=False),
        patch.object(short_term, "build_feature_matrix", return_value=short) as build,
        patch.object(train, "N_OPTUNA_TRIALS", 1),
        patch.object(train, "N_CV_SPLITS", 3),
        patch.object(train, "_objective", return_value=0.1),
        patch.object(short_term.platform, "platform", return_value="test-platform"),
        patch.object(
            short_term.subprocess,
            "run",
            return_value=SimpleNamespace(stdout="commit\n"),
            side_effect=None if git_available else FileNotFoundError("git"),
        ),
    ):
        path = short_term.train_experiment("DK1", "older", long)
    assert build.call_args.kwargs["horizon_steps"] == 192
    metadata = json.loads((path / "experiment.json").read_text())
    assert metadata["training_samples"] == 5000
    assert metadata["git_commit"] == ("commit" if git_available else None)
    assert len(metadata["source_sha256"]) == 64
    local_start = pd.Timestamp(metadata["test_start"]).tz_convert("Europe/Copenhagen")
    local_end = pd.Timestamp(metadata["test_end"]).tz_convert("Europe/Copenhagen")
    assert local_start.isoformat() == "2026-03-29T00:00:00+01:00"
    assert local_end.isoformat() == "2026-04-26T00:00:00+02:00"
    assert (local_end.date() - local_start.date()).days == 28
    assert pd.Timestamp(metadata["training_end"]) < pd.Timestamp(metadata["test_start"])
    assert (path / "training-long.joblib").is_file() and (path / "training-short.joblib").is_file()
    assert metadata["components"]["long"]["horizon_steps"] == 768
    assert metadata["components"]["short"]["horizon_steps"] == 192
    replay = short_term.reproduce(path)
    assert (
        max(v for values in replay["max_prediction_difference"].values() for v in values.values())
        == 0
    )
    long_models, short_models = short_term._components(path, metadata)
    assert long_models["p50"].feature_name_ == short_models["p50"].feature_name_


async def _register_predecessor(database, path, *, broken_newest=False):
    original = json.loads((path / "test-long.json").read_text())
    original["model_version"] = "older"
    registration = path / "older.json"
    registration.write_text(json.dumps(original))
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO model_versions (model_version, model_type, area, horizon_strategy,
                feature_set_version, trained_at, artifact_path, is_active)
            VALUES ('older', 'test', 'DK1', 'direct', 'v4-crossborder-history', NOW(), :path, TRUE)
        """),
            {"path": str(registration)},
        )
        if broken_newest:
            await conn.execute(
                text("""
                INSERT INTO model_versions (model_version, model_type, area, horizon_strategy,
                    feature_set_version, trained_at, artifact_path, is_active)
                VALUES ('newer-broken', 'test', 'DK1', 'direct', 'v4-crossborder-history',
                        NOW() + INTERVAL '1 minute', 'missing.json', TRUE)
            """)
            )


@pytest.mark.parametrize("broken_newest", [False, True])
@pytest.mark.parametrize("fail_registration", [False, True])
async def test_promotion_transaction_and_pinned_forecast_preserve_one_version(
    database,
    pending_experiment,
    model_root,
    fail_registration,
    broken_newest,
):
    from sqlalchemy.exc import IntegrityError

    from nordpool_predictor.ml import train
    from nordpool_predictor.worker import worker_lock

    path, _ = pending_experiment
    await _register_predecessor(database, path, broken_newest=broken_newest)
    version = "older" if fail_registration else "paired"
    artifact = artifacts.write_bundle(
        model_root,
        version,
        "DK1",
        component(1, ["horizon_step"]),
        component(2, ["horizon_step"]),
        {},
    )
    promoted = (version, artifact, {"short_term_evaluation": {"status": "accepted"}}, path)
    with (
        patch.object(train, "prepare_training_data", AsyncMock(return_value=pd.DataFrame())),
        patch.object(short_term, "start_or_promote", AsyncMock(return_value=promoted)),
    ):
        async with worker_lock():
            if fail_registration:
                with pytest.raises(IntegrityError):
                    await train.train_models("DK1")
            else:
                assert await train.train_models("DK1") == version
    async with database.connect() as conn:
        active = (
            (
                await conn.execute(
                    text("SELECT model_version FROM model_versions WHERE is_active = TRUE")
                )
            )
            .scalars()
            .all()
        )
        expected = (
            {"older", "newer-broken"}
            if fail_registration and broken_newest
            else {"older" if fail_registration else "paired"}
        )
        assert set(active) == expected
    if fail_registration:
        assert (path.parent / "pending.json").exists()
        return
    issued = datetime(2026, 11, 6, 12, 8, tzinfo=UTC)
    targets = pd.date_range("2026-11-06 12:15", periods=768, freq="15min", tz=UTC)
    frame = pd.DataFrame({"horizon_step": np.arange(1, 769)}, index=targets)
    with patch.object(predict, "_build_forecast_features", return_value=(frame, targets)):
        run_id = await predict.run_forecast("DK1", issued_at=issued, model_version=version)
    async with database.connect() as conn:
        run = (
            await conn.execute(
                text("SELECT model_version, status FROM forecast_runs WHERE run_id = :id"),
                {"id": run_id},
            )
        ).one()
        assert run.model_version == version and run.status == "completed"
        values = (
            (
                await conn.execute(
                    text(
                        "SELECT predicted_price_dkk_kwh FROM forecast_values "
                        "WHERE run_id = :id ORDER BY ts"
                    ),
                    {"id": run_id},
                )
            )
            .scalars()
            .all()
        )
        assert values[:192] == [2] * 192 and values[192:] == [1] * 576


def test_archived_features_preserve_real_lightgbm_threshold_decisions():
    import lightgbm as lgb

    values = np.repeat([0.12345678901, 0.12345678905], 20)
    training = pd.DataFrame({"signal": values})
    model = lgb.LGBMRegressor(
        n_estimators=1,
        min_child_samples=1,
        num_leaves=2,
        min_data_in_bin=1,
        n_jobs=1,
        verbosity=-1,
    ).fit(training, np.repeat([0.0, 1.0], 20))
    target = pd.DataFrame(
        {"signal": [values[-1]]}, index=pd.date_range("2026-01-01", periods=1, tz=UTC)
    )
    archived = json.loads(json.dumps(short_term._archive_features(target), allow_nan=False))
    replay = pd.DataFrame(archived["data"], columns=archived["columns"])
    assert replay.signal.iloc[0] == values[-1]
    assert model.predict(replay).tolist() == model.predict(target).tolist()
    rounded = json.loads(target.to_json(orient="split"))
    assert model.predict(pd.DataFrame(rounded["data"], columns=rounded["columns"])).tolist() != (
        model.predict(target).tolist()
    )
    target.iloc[0, 0] = np.nan
    assert short_term._archive_features(target)["data"] == [[None]]


def test_duplicate_good_origins_cannot_overweight_the_fixed_holdout():
    import copy

    data = snapshots()
    for snapshot in data:
        good_date = (
            pd.Timestamp(snapshot["scheduled_origin"])
            .tz_convert("Europe/Copenhagen")
            .date()
            .isoformat()
            == "2026-01-01"
        )
        for row in snapshot["rows"]:
            row["candidate"] = [0.7, 1.0 if good_date else 1.52, 1.8]
    before = short_term.evaluate_rows(data)
    assert before["status"] == "rejected"
    duplicates = []
    for second in range(1, 11):
        duplicate = copy.deepcopy(data[0])
        duplicate["issued_at"] = (
            pd.Timestamp(duplicate["issued_at"]) + timedelta(seconds=second)
        ).isoformat()
        duplicates.append(duplicate)
    assert short_term.evaluate_rows(data + duplicates) == before


async def test_all_eligible_outcomes_must_settle_before_immutable_runtime_approval(
    database,
    settled_experiment,
    mock_db_session,
):
    import hashlib
    import socket

    path, _ = settled_experiment
    await _register_predecessor(database, path)
    snapshot = sorted((path / "snapshots").glob("*.json"))[0]
    outcome = path / "outcomes" / snapshot.name
    saved = outcome.read_bytes()
    outcome.unlink()
    factory, _ = mock_db_session
    with patch.object(short_term, "get_session", factory):
        report = await short_term.evaluate_experiment(path)
        assert report["status"] == "insufficient_data"
        assert not report["missing_origins"] and len(report["unsettled_origins"]) == 1
        assert report["dates"] == 28  # 111 mature origins already meet the numeric minimums.
        with pytest.raises(ValueError, match="Complete the fixed prospective holdout"):
            await short_term.approve_runtime(path, 1536, 1, True, True, worker_parent_peak_mib=25)
    assert not (path / "runtime-approval.json").exists()
    outcome.write_bytes(saved)
    report = await short_term.evaluate_experiment(path)
    assert report["status"] == "awaiting_runtime_approval"
    short_term._write_once(
        path / "runtime-approval.json",
        {
            "approved": True,
            "boundary_reviewed": True,
            "training_on_target_reviewed": True,
            "production_model_version": "older",
            "host": socket.gethostname(),
            "metadata_sha256": hashlib.sha256((path / "experiment.json").read_bytes()).hexdigest(),
            "evidence_sha256": report["evidence_sha256"],
            "child_peak_mib": 75,
            "worker_parent_peak_mib": 25,
            "peak_mib": 100,
            "memory_budget_mib": 1536,
            "ingestion_wait_seconds": 1,
        },
    )
    approved = await short_term.evaluate_experiment(path)
    assert approved["status"] == "accepted"
    # An extra debug capture is neither a new eligible origin nor part of the evidence hash.
    debug = json.loads(snapshot.read_text())
    debug["issued_at"] = (pd.Timestamp(debug["issued_at"]) + timedelta(seconds=5)).isoformat()
    short_term._write_once(path / "snapshots" / "debug-copy.json", debug)
    assert (await short_term.evaluate_experiment(path))["evidence_sha256"] == approved[
        "evidence_sha256"
    ]
    assert (await short_term.evaluate_experiment(path))["status"] == "accepted"


def test_full_local_holdout_has_112_fixed_slots_even_across_dst():
    metadata = {"test_start": "2026-03-28T23:00:00+00:00", "test_end": "2026-04-25T22:00:00+00:00"}
    origins = short_term._expected_origins(metadata)
    assert len(origins) == 112
    assert (
        len({origin.astimezone(ZoneInfo("Europe/Copenhagen")).date() for origin in origins}) == 28
    )
    assert {origin.astimezone(ZoneInfo("Europe/Copenhagen")).hour for origin in origins} == {
        0,
        6,
        12,
        18,
    }


async def test_loaded_partial_outcome_keeps_the_entire_origin_unsettled(
    database, settled_experiment
):
    path, _ = settled_experiment
    await _register_predecessor(database, path)
    outcome = sorted((path / "outcomes").glob("*.json"))[0]
    observed = json.loads(outcome.read_text())
    missing = list(observed["actual"])[10]
    del observed["actual"][missing]
    outcome.write_text(json.dumps(observed))
    partial = outcome.read_bytes()
    report = await short_term.evaluate_experiment(path)
    assert report["status"] == "insufficient_data"
    assert report["dates"] == 28
    assert len(report["unsettled_origins"]) == 1 and not report["missing_origins"]
    assert report["metrics"]["0_48"]["baseline"]["samples"] == 111 * 191
    with pytest.raises(ValueError, match="Complete the fixed prospective holdout"):
        await short_term.approve_runtime(path, 1536, 1, True, True, worker_parent_peak_mib=25)
    assert not (path / "runtime-approval.json").exists()
    assert outcome.read_bytes() == partial


async def test_trial_training_carries_the_actual_artifact_fallback_version(
    database,
    pending_experiment,
):
    from nordpool_predictor.ml import train

    path, _ = pending_experiment
    await _register_predecessor(database, path, broken_newest=True)
    with (
        patch.object(train, "prepare_training_data", AsyncMock(return_value=pd.DataFrame())),
        patch.object(short_term, "start_or_promote", AsyncMock(return_value=None)) as trial,
    ):
        assert await train.train_models("DK1") == "older"
    assert trial.await_args.args[1] == "older"


async def test_cli_trial_training_uses_the_actual_artifact_fallback(
    database,
    pending_experiment,
):
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path

    path, _ = pending_experiment
    await _register_predecessor(database, path, broken_newest=True)
    spec = spec_from_file_location(
        "trial_cli", Path(__file__).parents[1] / "scripts/short_term_experiment.py"
    )
    cli = module_from_spec(spec)
    spec.loader.exec_module(cli)
    with (
        patch.object(cli, "experiment_path", return_value=None),
        patch.object(cli, "build_feature_matrix", return_value=pd.DataFrame()),
        patch.object(cli, "train_experiment", return_value=path) as train,
    ):
        await cli._run_command(SimpleNamespace(command="train", area="DK1"))
    assert train.call_args.args[1] == "older"


async def test_runtime_approval_measures_the_same_artifact_fallback_version(
    database,
    settled_experiment,
):
    path, _ = settled_experiment
    await _register_predecessor(database, path, broken_newest=True)
    targets = pd.date_range("2026-01-01", periods=768, freq="15min", tz=UTC)
    frame = pd.DataFrame({"horizon_step": np.arange(1, 769)}, index=targets)
    evaluate = short_term.evaluate_experiment

    async def verify_active_is_resident_during_evaluation(path):
        assert artifacts._load_quantile.cache_info().currsize == 3
        return await evaluate(path)

    with (
        patch.object(
            short_term,
            "evaluate_experiment",
            side_effect=verify_active_is_resident_during_evaluation,
        ),
        patch.object(predict, "_build_forecast_features", return_value=(frame, targets)),
        patch.object(short_term, "_peak_mib", return_value=100),
    ):
        approved = await short_term.approve_runtime(
            path, 1536, 1, True, True, worker_parent_peak_mib=25
        )
    assert approved["production_model_version"] == "older"
    assert approved["child_peak_mib"] == 100
    assert approved["worker_parent_peak_mib"] == 25
    assert approved["peak_mib"] == 125
    assert artifacts._load_quantile.cache_info().currsize == 9
    assert (await evaluate(path))["status"] == "accepted"


def test_changing_area_releases_cached_estimators_before_loading_the_next(model_root):
    import weakref

    import joblib

    first = artifacts.write_bundle(
        model_root, "one", "DK1", component(1, ["a"]), component(2, ["a"]), {}
    )
    second = artifacts.write_bundle(
        model_root, "two", "DK2", component(1, ["a"]), component(2, ["a"]), {}
    )
    models = artifacts.load_models("one", first, "DK1")
    references = [weakref.ref(models[q]) for q in artifacts.QUANTILES]
    references.extend(weakref.ref(models["short"][q]) for q in artifacts.QUANTILES)
    del models
    assert all(reference() is not None for reference in references)
    load = joblib.load

    def verify_released(path):
        assert all(reference() is None for reference in references)
        return load(path)

    with patch.object(artifacts.joblib, "load", side_effect=verify_released):
        artifacts.load_models("two", second, "DK2")
    assert artifacts._load_quantile.cache_info().currsize == 6


async def test_bootstrap_releases_each_areas_temporary_models(database, model_root):
    import weakref

    import joblib

    for area in ("DK1", "DK2"):
        manifest = artifacts.write_bundle(
            model_root, area, area, component(1, ["a"]), component(2, ["a"]), {}
        )
        async with database.begin() as conn:
            await conn.execute(
                text("""
                INSERT INTO model_versions (model_version, model_type, area, horizon_strategy,
                    feature_set_version, trained_at, artifact_path, is_active)
                VALUES (:area, 'test', :area, 'paired_48h', 'v4-crossborder-history',
                        NOW(), :path, TRUE)
            """),
                {"area": area, "path": manifest},
            )
    load = joblib.load
    area_seen, references = None, []

    def track_area(path):
        nonlocal area_seen, references
        area = path.split("/")[-1].split("-")[0]
        if area_seen is not None and area != area_seen:
            assert all(reference() is None for reference in references)
            references = []
        model = load(path)
        references.append(weakref.ref(model))
        area_seen = area
        return model

    with patch.object(artifacts.joblib, "load", side_effect=track_area):
        assert await bootstrap.ready_models() == {"DK1": "DK1", "DK2": "DK2"}
    assert artifacts._load_quantile.cache_info().currsize == 6


@pytest.mark.parametrize("parent_peak", [None, 0, -1, float("nan"), float("inf")])
async def test_runtime_approval_requires_measured_worker_parent_proof(
    settled_experiment, parent_peak
):
    path, _ = settled_experiment
    with pytest.raises(ValueError, match="Measured worker-parent peak RSS"):
        await short_term.approve_runtime(
            path, 1536, 1, True, True, worker_parent_peak_mib=parent_peak
        )
    assert not (path / "runtime-approval.json").exists()


async def test_container_budget_includes_resident_worker_parent(database, settled_experiment):
    path, _ = settled_experiment
    await _register_predecessor(database, path)
    targets = pd.date_range("2026-01-01", periods=768, freq="15min", tz=UTC)
    frame = pd.DataFrame({"horizon_step": np.arange(1, 769)}, index=targets)
    with (
        patch.object(predict, "_build_forecast_features", return_value=(frame, targets)),
        patch.object(short_term, "_peak_mib", return_value=100),
    ):
        # Child alone fits150 MiB, but the genuine parent reserve makes160 MiB.
        with pytest.raises(ValueError, match="exceeds target NAS budget"):
            await short_term.approve_runtime(path, 150, 1, True, True, worker_parent_peak_mib=60)
        assert not (path / "runtime-approval.json").exists()
        approved = await short_term.approve_runtime(
            path, 160, 1, True, True, worker_parent_peak_mib=60
        )
    assert approved["child_peak_mib"] == 100 and approved["worker_parent_peak_mib"] == 60
    assert approved["peak_mib"] == 160
    assert (await short_term.evaluate_experiment(path))["status"] == "accepted"
    # Child-only legacy records and totals smaller than their components are not proof.
    approval_file = path / "runtime-approval.json"
    for altered in (
        {
            key: value
            for key, value in approved.items()
            if key not in {"child_peak_mib", "worker_parent_peak_mib"}
        },
        {**approved, "peak_mib": 100},
        {**approved, "memory_budget_mib": 150},
        {**approved, "worker_parent_peak_mib": None},
    ):
        approval_file.write_text(json.dumps(altered))
        assert (await short_term.evaluate_experiment(path))[
            "status"
        ] == "awaiting_runtime_approval"


async def test_pending_trial_skips_fresh_training_history(database, pending_experiment):
    from nordpool_predictor.ml import train

    path, _ = pending_experiment
    await _register_predecessor(database, path)
    with patch.object(
        train,
        "prepare_training_data",
        AsyncMock(side_effect=RuntimeError("fresh training history is unavailable")),
    ) as prepare:
        assert await train.train_models("DK1") == "older"
    prepare.assert_not_awaited()


def _damage_short_grid(snapshot, case, actual):
    import copy

    if case == "missing":
        snapshot["rows"].pop(10)
    elif case == "duplicate_known":
        snapshot["rows"][10] = copy.deepcopy(snapshot["rows"][11])
        snapshot["published_prices"][snapshot["rows"][10]["ts"]] = 1
    else:
        row = snapshot["rows"][0 if case == "misaligned_known" else 10]
        row["ts"] = (pd.Timestamp(row["ts"]) + timedelta(minutes=1)).isoformat()
        actual[row["ts"]] = 1
        if case == "misaligned_known":
            snapshot["published_prices"][row["ts"]] = 1


@pytest.mark.parametrize(
    "case", ["missing", "duplicate_known", "misaligned_known", "misaligned_unknown"]
)
def test_pure_metrics_reject_malformed_grid_before_known_price_masking(case):
    data = snapshots()
    for snapshot in data:
        _damage_short_grid(snapshot, case, {})
    assert len(data) == 112
    assert all(len(snapshot["rows"]) >= 191 for snapshot in data)
    assert short_term.evaluate_rows(data)["status"] == "invalid_data"


def test_short_grid_accepts_normal_full_horizon_snapshot():
    snapshot = snapshots()[0]
    issued = pd.Timestamp(snapshot["issued_at"]).floor("15min")
    snapshot["rows"].extend(
        {
            "horizon_step": step,
            "ts": (issued + timedelta(minutes=15 * step)).isoformat(),
        }
        for step in range(193, 769)
    )
    assert len(snapshot["rows"]) == 768
    assert short_term._short_rows(snapshot) == snapshot["rows"][:192]


@pytest.mark.parametrize(
    "case", ["missing", "duplicate_known", "misaligned_known", "misaligned_unknown"]
)
async def test_all_112_origins_with_partial_or_invalid_grids_cannot_promote(
    database,
    settled_experiment,
    case,
):
    import hashlib

    path, _ = settled_experiment
    await _register_predecessor(database, path)
    files = sorted((path / "snapshots").glob("*.json"))
    assert len(files) == 112
    for file in files:
        snapshot = json.loads(file.read_text())
        outcome_file = path / "outcomes" / file.name
        outcome = json.loads(outcome_file.read_text())
        _damage_short_grid(snapshot, case, outcome["actual"])
        file.write_text(json.dumps(snapshot))
        # Simulate an originally archived malformed capture, not a later hash mismatch.
        outcome["snapshot_sha256"] = hashlib.sha256(file.read_bytes()).hexdigest()
        outcome_file.write_text(json.dumps(outcome))
    report = await short_term.evaluate_experiment(path)
    assert report["status"] == "invalid_data"
    assert len(report["unsettled_origins"]) == 112 and not report["missing_origins"]
    assert len(report["invalid_snapshot_origins"]) == 112
    with pytest.raises(ValueError, match="Complete the fixed prospective holdout"):
        await short_term.approve_runtime(path, 1536, 1, True, True, worker_parent_peak_mib=25)
    assert not (path / "runtime-approval.json").exists()


async def test_pending_fully_evidenced_promotion_needs_no_fresh_training_history(
    database,
    settled_experiment,
):
    import hashlib

    from nordpool_predictor.ml import train
    from nordpool_predictor.worker import worker_lock

    path, _ = settled_experiment
    await _register_predecessor(database, path)
    report = await short_term.evaluate_experiment(path)
    assert report["status"] == "awaiting_runtime_approval"
    short_term._write_once(
        path / "runtime-approval.json",
        {
            "approved": True,
            "boundary_reviewed": True,
            "training_on_target_reviewed": True,
            "production_model_version": "older",
            "metadata_sha256": hashlib.sha256((path / "experiment.json").read_bytes()).hexdigest(),
            "evidence_sha256": report["evidence_sha256"],
            "child_peak_mib": 75,
            "worker_parent_peak_mib": 25,
            "peak_mib": 100,
            "memory_budget_mib": 1536,
            "ingestion_wait_seconds": 1,
        },
    )
    with patch.object(
        train,
        "prepare_training_data",
        AsyncMock(side_effect=RuntimeError("fresh training history is unavailable")),
    ) as prepare:
        async with worker_lock():
            version = await train.train_models("DK1")
    prepare.assert_not_awaited()
    assert version.startswith("lgbm-DK1-paired-")
    async with database.connect() as connection:
        active = (
            (
                await connection.execute(
                    text("SELECT model_version FROM model_versions WHERE is_active = TRUE")
                )
            )
            .scalars()
            .all()
        )
    assert active == [version]


@pytest.mark.parametrize("has_active_model", [False, True])
async def test_first_install_and_new_trials_still_require_training_preparation(
    database,
    pending_experiment,
    has_active_model,
):
    from nordpool_predictor.ml import train

    path, _ = pending_experiment
    if has_active_model:
        await _register_predecessor(database, path)
        (path.parent / "pending.json").unlink()
    with (
        patch.object(
            train,
            "prepare_training_data",
            AsyncMock(side_effect=RuntimeError("fresh training history is unavailable")),
        ) as prepare,
        patch.object(train, "_train_sync") as fit,
        patch.object(short_term, "train_experiment") as trial,
    ):
        with pytest.raises(RuntimeError, match="fresh training history is unavailable"):
            await train.train_models("DK1")
    prepare.assert_awaited_once()
    fit.assert_not_called()
    trial.assert_not_called()
