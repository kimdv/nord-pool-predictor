"""One artifact contract for training, inference, bootstrap and diagnostics."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import joblib

from nordpool_predictor.config import get_settings

QUANTILES = ("p10", "p50", "p90")


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
    path = (root / artifact_path).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Artifact path is outside MODEL_DIR")
    if path.suffix == ".json":
        manifest = json.loads(path.read_text())
        if (
            manifest.get("format_version") != 1
            or manifest.get("model_version") != version
            or manifest.get("area") != area
        ):
            raise ValueError("Artifact manifest identity does not match the registered model")
        paths = {q: (path.parent / manifest["quantiles"][q]).resolve() for q in QUANTILES}
    else:
        # Legacy registrations store the directory rather than a manifest.
        suffix = version.removeprefix(f"lgbm-{area}-")
        paths = {q: path / f"lgbm-{area}-{q}-{suffix}.joblib" for q in QUANTILES}
        manifest = None
    if any(not item.is_relative_to(root) for item in paths.values()):
        raise ValueError("Quantile path is outside MODEL_DIR")
    return paths, manifest


@lru_cache(maxsize=12)
def _load_quantile(version: str, path: str) -> Any:
    # Versioned files are immutable once registered. The bound keeps old models out of memory.
    return joblib.load(path)


def load_models(version: str, artifact_path: str, area: str) -> dict[str, Any]:
    paths, _ = _resolve(version, artifact_path, area)
    return {q: _load_quantile(version, str(path)) for q, path in paths.items()}


def load_feature_importance(version: str, artifact_path: str, area: str) -> list[dict]:
    paths, manifest = _resolve(version, artifact_path, area)
    if manifest is not None:
        return manifest["feature_importance"]
    return feature_importance(_load_quantile(version, str(paths["p50"])))
