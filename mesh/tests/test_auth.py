"""Tests for runtime.auth — Bearer token authentication middleware."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from runtime.auth import AuthMiddleware


def _make_app(auth_token: str | None = None) -> FastAPI:
    """Create a minimal FastAPI app with auth middleware."""
    app = FastAPI()
    app.add_middleware(AuthMiddleware, auth_token=auth_token)

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "healthy"})

    @app.post("/action")
    async def action() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/skills")
    async def skills() -> JSONResponse:
        return JSONResponse({"skills": []})

    return app


@pytest.mark.asyncio
async def test_no_token_configured_allows_all() -> None:
    """When no auth_token is set, all requests pass through."""
    app = _make_app(auth_token=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/action")
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_exempt_paths_skip_auth() -> None:
    """Health and skills endpoints are exempt from auth."""
    app = _make_app(auth_token="secret-123")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200

        resp = await client.get("/skills")
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_missing_token_returns_401() -> None:
    """Requests without Authorization header return 401."""
    app = _make_app(auth_token="secret-123")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/action")
        assert resp.status_code == 401
        assert "UNAUTHORIZED" in resp.json()["error"]


@pytest.mark.asyncio
async def test_wrong_token_returns_403() -> None:
    """Requests with wrong Bearer token return 403."""
    app = _make_app(auth_token="secret-123")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/action",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 403
        assert "FORBIDDEN" in resp.json()["error"]


@pytest.mark.asyncio
async def test_valid_token_allows_request() -> None:
    """Requests with correct Bearer token pass through."""
    app = _make_app(auth_token="secret-123")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/action",
            headers={"Authorization": "Bearer secret-123"},
        )
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_malformed_auth_header_returns_401() -> None:
    """Non-Bearer auth scheme returns 401."""
    app = _make_app(auth_token="secret-123")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/action",
            headers={"Authorization": "Basic dXNlcjpwYXNz"},
        )
        assert resp.status_code == 401
