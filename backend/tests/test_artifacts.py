from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import joblib
import numpy as np
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from nordpool_predictor.api.routers.models import router
from nordpool_predictor.ml import artifacts

VERSION = "lgbm-DK1-20260101-000000"


@pytest.fixture
def artifact_bundle(tmp_path, test_settings):
    test_settings.model_dir = str(tmp_path)
    model = SimpleNamespace(feature_name_=["a", "b"], feature_importances_=np.array([2, 5]))
    paths = {}
    for quantile in artifacts.QUANTILES:
        name = f"lgbm-DK1-{quantile}-20260101-000000.joblib"
        joblib.dump(model, tmp_path / name)
        paths[quantile] = name
    manifest = artifacts.write_manifest(tmp_path, VERSION, "DK1", paths, model)
    with patch.object(artifacts, "get_settings", return_value=test_settings):
        yield tmp_path, manifest
    artifacts._load_quantile.cache_clear()


@pytest.mark.parametrize("legacy", [False, True])
def test_manifest_and_legacy_directory_load_the_same_quantiles(artifact_bundle, legacy):
    root, manifest = artifact_bundle
    path = str(root) if legacy else manifest
    with patch.object(artifacts.joblib, "load", wraps=joblib.load) as load:
        first = artifacts.load_models(VERSION, path, "DK1")
        second = artifacts.load_models(VERSION, path, "DK1")
    assert set(first) == {"p10", "p50", "p90"}
    assert first["p50"] is second["p50"]
    assert load.call_count == 3
    assert artifacts.load_feature_importance(VERSION, path, "DK1")[0] == {
        "feature": "b",
        "importance": 5.0,
    }


async def test_feature_importance_reads_manifest_without_unpickling(database, artifact_bundle):
    _, manifest = artifact_bundle
    async with database.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO model_versions (model_version, model_type, area, horizon_strategy,
                feature_set_version, trained_at, artifact_path)
            VALUES (:version, 'test', 'DK1', 'direct', 'v2-asof', NOW(), :artifact)
        """),
            {"version": VERSION, "artifact": manifest},
        )
    app = FastAPI()
    app.include_router(router)
    with patch.object(artifacts.joblib, "load", side_effect=AssertionError("Unexpected unpickle")):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/api/models/{VERSION}/feature-importance")
    assert response.status_code == 200
    assert response.json()[0] == {"feature": "b", "importance": 5.0}


def test_manifest_identity_and_path_escape_are_rejected(artifact_bundle):
    _, manifest = artifact_bundle
    with pytest.raises(ValueError, match="identity"):
        artifacts.load_models("different-version", manifest, "DK1")
    with pytest.raises(ValueError, match="outside"):
        artifacts.load_models(VERSION, "../outside.json", "DK1")
