from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def _patched_settings(test_settings):
    """Patch get_settings in every module that imports it."""
    with (
        patch("nordpool_predictor.config.get_settings", return_value=test_settings),
        patch("nordpool_predictor.api.app.get_settings", return_value=test_settings),
        patch("nordpool_predictor.api.routers.areas.get_settings", return_value=test_settings),
        patch("nordpool_predictor.api.routers.prices.get_settings", return_value=test_settings),
    ):
        yield


@pytest.fixture
def _patched_lifespan():
    """Replace the application lifespan with a no-op."""

    @asynccontextmanager
    async def _noop(_app):
        yield

    with patch("nordpool_predictor.api.app.lifespan", _noop):
        yield


@pytest.fixture
async def client(_patched_settings, _patched_lifespan):
    from nordpool_predictor.api.app import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_session_ctx(session: AsyncMock):
    """Wrap *session* in an async-context-manager matching ``get_session``."""

    @asynccontextmanager
    async def _ctx():
        yield session

    return _ctx


def _empty_query_session() -> AsyncMock:
    """Session whose execute() returns empty mappings."""
    session = AsyncMock()
    result = MagicMock()
    result.mappings.return_value.first.return_value = None
    result.mappings.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=result)
    return session


# ---------------------------------------------------------------------------
# GET /api/health
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    async def test_returns_200_with_structure(self, client):
        session = AsyncMock()
        mock_row = {"last_updated": None, "last_forecast_at": None}
        result = MagicMock()
        result.mappings.return_value.first.return_value = mock_row
        session.execute = AsyncMock(return_value=result)

        with patch(
            "nordpool_predictor.api.routers.health.get_session",
            _mock_session_ctx(session),
        ):
            resp = await client.get("/api/health")

        assert resp.status_code == 200
        body = resp.json()
        assert "status" in body
        assert "sources" in body
        assert isinstance(body["sources"], list)
        assert "degraded" in body

    async def test_error_status_on_db_failure(self, client):
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=RuntimeError("DB down"))

        with patch(
            "nordpool_predictor.api.routers.health.get_session",
            _mock_session_ctx(session),
        ):
            resp = await client.get("/api/health")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "error"
        assert body["degraded"] is True


# ---------------------------------------------------------------------------
# GET /api/areas
# ---------------------------------------------------------------------------


class TestAreasEndpoint:
    async def test_returns_configured_areas(self, client):
        resp = await client.get("/api/areas")

        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        codes = {a["code"] for a in body}
        assert codes == {"DK1", "DK2"}

    async def test_area_structure(self, client):
        resp = await client.get("/api/areas")
        body = resp.json()

        for area in body:
            assert "code" in area
            assert "label" in area
            assert "weather_points" in area
            assert isinstance(area["weather_points"], list)
            assert len(area["weather_points"]) >= 1


# ---------------------------------------------------------------------------
# GET /api/prices/{area}
# ---------------------------------------------------------------------------


class TestPricesEndpoint:
    async def test_invalid_area_returns_404(self, client):
        resp = await client.get("/api/prices/INVALID")

        assert resp.status_code == 404
        assert "INVALID" in resp.json()["detail"]

    async def test_valid_area_empty_result(self, client):
        session = _empty_query_session()

        with patch(
            "nordpool_predictor.api.routers.prices.get_session",
            _mock_session_ctx(session),
        ):
            resp = await client.get("/api/prices/DK1")

        assert resp.status_code == 200
        body = resp.json()
        assert body["area"] == "DK1"
        assert body["prices"] == []
