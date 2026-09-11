"""Tests for platform endpoints: session querying, QR retrieval, and resume."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_list_platforms(client: AsyncClient) -> None:
    resp = await client.get("/v1/platforms")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["platforms"]) == 2
    platforms = {p["platform"]: p for p in data["platforms"]}
    assert "wechat_mp" in platforms
    assert "xiaohongshu" in platforms


@pytest.mark.asyncio
async def test_platform_login_and_qr(client: AsyncClient) -> None:
    login_resp = await client.post("/v1/platforms/wechat_mp/login")
    assert login_resp.status_code == 200


@pytest.mark.asyncio
async def test_platform_resume(client: AsyncClient) -> None:
    resume_resp = await client.post("/v1/platforms/xiaohongshu/resume")
    assert resume_resp.status_code == 200
    assert resume_resp.json()["status"] == "resumed"


@pytest.mark.asyncio
async def test_platform_reauth_starts_a_new_alert_incident(
    client: AsyncClient,
    test_db: tuple[object, object],
) -> None:
    _engine, session_factory = test_db
    from publisher.models import PlatformState

    async with session_factory() as session:
        state = await session.get(PlatformState, "wechat_mp")
        assert state is not None
        state.alert_incident_id = "old-incident"
        await session.commit()

    resp = await client.post("/v1/platforms/wechat_mp/reauth")
    assert resp.status_code == 200

    async with session_factory() as session:
        state = await session.get(PlatformState, "wechat_mp")
        assert state is not None
        assert state.alert_incident_id is None
