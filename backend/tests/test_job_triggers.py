from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from nordpool_predictor.api.routers.jobs import router
from nordpool_predictor.scheduler.queue import JOB_NAMES


@pytest.mark.parametrize("job_name", sorted(JOB_NAMES | {"bootstrap"}))
async def test_manual_jobs_can_be_queued_without_credentials(job_name):
    app = FastAPI()
    app.include_router(router)
    job_id = "00000000-0000-0000-0000-000000000001"
    operation = AsyncMock(return_value=job_id)
    with patch("nordpool_predictor.api.routers.jobs.enqueue_job", operation):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/api/jobs/trigger/{job_name}")

    assert response.status_code == 202
    assert response.json() == {"status": "pending", "job": job_name, "job_id": job_id}
    operation.assert_awaited_once_with(job_name)


@pytest.mark.parametrize(("job_name", "status"), [("unknown_job", 404), ("ingest_weather", 409)])
async def test_manual_job_errors_are_preserved(job_name, status):
    app = FastAPI()
    app.include_router(router)
    operation = AsyncMock(return_value=None)
    with patch("nordpool_predictor.api.routers.jobs.enqueue_job", operation):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/api/jobs/trigger/{job_name}")

    assert response.status_code == status
    if status == 404:
        operation.assert_not_awaited()
    else:
        operation.assert_awaited_once_with(job_name)


@pytest.mark.parametrize(
    ("job_name", "result", "status"),
    [
        ("retrain_model", "cancelling", 202),
        ("ingest_prices", "cancelled", 202),
        ("bootstrap", "cancelling", 202),
        ("ingest_weather", None, 409),
        ("unknown_job", None, 404),
    ],
)
async def test_cancel_job(job_name, result, status):
    app = FastAPI()
    app.include_router(router)
    operation = AsyncMock(return_value=result)
    with patch("nordpool_predictor.api.routers.jobs.cancel_job", operation):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/api/jobs/cancel/{job_name}")
    assert response.status_code == status
    if status == 404:
        operation.assert_not_awaited()
    else:
        operation.assert_awaited_once_with(job_name)
    if status == 202:
        assert response.json() == {"status": result, "job": job_name}


@pytest.mark.parametrize("action", ["trigger", "cancel"])
@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Origin": "https://attacker.example"}, 403),
        ({"Origin": "null"}, 403),
        ({"Sec-Fetch-Site": "cross-site"}, 403),
        ({"Origin": "http://test"}, 202),
        ({"Origin": "http://localhost:3000", "Sec-Fetch-Site": "cross-site"}, 202),
        ({}, 202),
    ],
)
async def test_job_changes_reject_cross_origin_forms(action, headers, expected, test_settings):
    app = FastAPI()
    app.include_router(router)
    operation = AsyncMock(return_value="00000000-0000-0000-0000-000000000001")
    name = "enqueue_job" if action == "trigger" else "cancel_job"
    with (
        patch(f"nordpool_predictor.api.routers.jobs.{name}", operation),
        patch("nordpool_predictor.api.routers.jobs.get_settings", return_value=test_settings),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                f"/api/jobs/{action}/retrain_model", headers=headers, data={"job": "retrain_model"}
            )

    assert response.status_code == expected
    if expected == 403:
        operation.assert_not_awaited()
    else:
        operation.assert_awaited_once_with("retrain_model")
