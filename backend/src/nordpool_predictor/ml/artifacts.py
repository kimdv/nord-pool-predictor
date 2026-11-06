"""One artifact contract for training, inference, bootstrap and diagnostics."""

from __future__ import annotations

import gc
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import joblib

from nordpool_predictor.config import get_settings
from nordpool_predictor.ml.horizon import SHORT_HORIZON_STEPS

QUANTILES = ("p10", "p50", "p90")
_model_cache_area: str | None = None


def clear_model_cache() -> None:
    _load_quantile.cache_clear()
    gc.collect()


def _use_area_cache(area: str) -> None:
    global _model_cache_area
    if _model_cache_area != area:
        clear_model_cache()
        _model_cache_area = area


def feature_importance(model: Any) -> list[dict[str, str | float]]:
    if not hasattr(model, "feature_importances_"):
        raise AttributeError("Model does not expose feature importances")
    values = model.feature_importances_
    names = getattr(model, "feature_name_", [f"feature_{i}" for i in range(len(values))])
    return [
        {"feature": name, "importance": float(value)}
        for name, value in sorted(
            zip(names, values, strict=True), key=lambda item: item[1], reverse=True
        )
    ]


def write_manifest(
    model_dir: Path, version: str, area: str, paths: dict[str, str], model: Any
) -> str:
    path = model_dir / f"{version}.json"
    manifest = {
        "format_version": 1,
        "model_version": version,
        "area": area,
        "quantiles": paths,
        "feature_importance": feature_importance(model),
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest))
    temporary.replace(path)
    return path.name


def _resolve(version: str, artifact_path: str, area: str) -> tuple[dict[str, Path], dict | None]:
    root = Path(get_settings().model_dir).resolve()
    registration = Path(artifact_path)
    # Manifests store root-relative filenames; legacy directories are CWD-relative.
    path = (root / registration if registration.suffix == ".json" else registration).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Artifact path is outside MODEL_DIR")
    if path.suffix == ".json":
        manifest = json.loads(path.read_text())
        if (
            manifest.get("format_version") not in (1, 2)
            or manifest.get("model_version") != version
            or manifest.get("area") != area
        ):
            raise ValueError("Artifact manifest identity does not match the registered model")
        paths = {q: (path.parent / manifest["quantiles"][q]).resolve() for q in QUANTILES}
        if manifest["format_version"] == 2:
            if manifest.get("short_horizon_steps") != SHORT_HORIZON_STEPS:
                raise ValueError("Unsupported short forecast horizon")
            paths.update(
                {
                    f"short:{q}": (path.parent / manifest["short_quantiles"][q]).resolve()
                    for q in QUANTILES
                }
            )
    else:
        # Legacy registrations store the directory rather than a manifest.
        suffix = version.removeprefix(f"lgbm-{area}-")
        paths = {q: (path / f"lgbm-{area}-{q}-{suffix}.joblib").resolve() for q in QUANTILES}
        manifest = None
    if any(not item.is_relative_to(root) for item in paths.values()):
        raise ValueError("Quantile path is outside MODEL_DIR")
    return paths, manifest


@lru_cache(maxsize=12)
def _load_quantile(version: str, path: str) -> Any:
    # Versioned files are immutable once registered. The bound keeps old models out of memory.
    return joblib.load(path)


def load_models(version: str, artifact_path: str, area: str) -> dict[str, Any]:
    _use_area_cache(area)
    paths, manifest = _resolve(version, artifact_path, area)
    models = {q: _load_quantile(version, str(paths[q])) for q in QUANTILES}
    if manifest is not None and manifest["format_version"] == 2:
        short = {q: _load_quantile(version, str(paths[f"short:{q}"])) for q in QUANTILES}
        for role, component in (("long", models), ("short", short)):
            expected = manifest["features"][role]
            if not expected or any(
                list(component[q].feature_name_) != expected for q in QUANTILES
            ):
                raise ValueError(f"Invalid {role} component feature contract")
        models["short"] = short
    return models


def write_bundle(
    model_dir: Path, version: str, area: str, long: dict, short: dict, metadata: dict
) -> str:
    """Own every component file; never depend on a predecessor's artifact lifetime."""
    model_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "format_version": 2,
        "model_version": version,
        "area": area,
        "short_horizon_steps": SHORT_HORIZON_STEPS,
        "feature_importance": feature_importance(long["p50"]),
        "short_feature_importance": feature_importance(short["p50"]),
        "features": {},
        "components": metadata,
    }
    for role, models in (("long", long), ("short", short)):
        names = list(models["p50"].feature_name_)
        if not names or any(list(models[q].feature_name_) != names for q in QUANTILES):
            raise ValueError(f"Invalid {role} component feature contract")
        manifest["features"][role] = names
        paths = {}
        for quantile in QUANTILES:
            target = model_dir / f"{version}-{role}-{quantile}.joblib"
            temporary = target.with_suffix(".joblib.tmp")
            joblib.dump(models[quantile], temporary)
            temporary.replace(target)
            paths[quantile] = target.name
        manifest["quantiles" if role == "long" else "short_quantiles"] = paths
    target = model_dir / f"{version}.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, allow_nan=False))
    temporary.replace(target)
    return target.name


def load_feature_importance(version: str, artifact_path: str, area: str) -> list[dict]:
    _use_area_cache(area)
    paths, manifest = _resolve(version, artifact_path, area)
    if manifest is not None:
        return manifest["feature_importance"]
    return feature_importance(_load_quantile(version, str(paths["p50"])))
