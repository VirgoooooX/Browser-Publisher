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
