"""Tests for Console and API authentication."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from publisher.config import PublisherSettings
from publisher.main import create_app


@pytest.mark.asyncio
async def test_api_unauthorized_without_token(test_settings: PublisherSettings, test_db: object) -> None:
    engine, session_factory = test_db
    app = create_app(test_settings)
    app.state.engine = engine
    app.state.session_factory = session_factory

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/v1/jobs")
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_console_login_flow(test_settings: PublisherSettings, test_db: object) -> None:
    engine, session_factory = test_db
    app = create_app(test_settings)
    app.state.engine = engine
    app.state.session_factory = session_factory

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. Accessing root without cookie redirects to /login
        resp1 = await ac.get("/", follow_redirects=False)
        assert resp1.status_code == 303
        assert resp1.headers["location"] == "/login"

        # 2. Login with incorrect token fails
        resp2 = await ac.post("/console/login", data={"token": "wrong_token"})
        assert resp2.status_code == 401
        assert "无效的 Access Token" in resp2.text

        # 3. Login with correct token sets cookie and redirects
        resp3 = await ac.post(
            "/console/login",
            data={"token": test_settings.access_token.get_secret_value()},
            follow_redirects=False,
        )
        assert resp3.status_code == 303
        assert resp3.headers["location"] == "/"
        assert "publisher_session" in resp3.cookies

        # 4. Accessing root with cookie succeeds
        resp4 = await ac.get("/", cookies=resp3.cookies)
        assert resp4.status_code == 200
        assert "Browser Publisher" in resp4.text
