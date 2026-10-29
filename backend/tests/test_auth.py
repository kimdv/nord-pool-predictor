from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from nordpool_predictor.api.routers.jobs import router


@pytest.mark.parametrize(
    ("configured", "provided", "status"),
    [
        (None, None, 503),
        ("", "secret", 503),
        ("secret", None, 401),
        ("secret", "wrong", 401),
        ("secret", "secret", 202),
    ],
)
async def test_job_authentication(configured, provided, status, test_settings):
    test_settings.admin_api_token = None if configured is None else SecretStr(configured)
    app = FastAPI()
    app.include_router(router)
    headers = {} if provided is None else {"Authorization": f"Bearer {provided}"}
    operation = AsyncMock(return_value="00000000-0000-0000-0000-000000000001")
    with (
        patch("nordpool_predictor.api.auth.get_settings", return_value=test_settings),
        patch("nordpool_predictor.api.routers.jobs.enqueue_job", operation),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/jobs/trigger/ingest_prices", headers=headers)
        assert response.status_code == status
        if status != 202:
            operation.assert_not_called()
        if status == 401:
            assert response.headers["www-authenticate"] == "Bearer"
