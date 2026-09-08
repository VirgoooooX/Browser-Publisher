"""Tests for Media upload, validation, and checksum deduplication."""

from __future__ import annotations

import io
import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_media_upload_and_deduplication(client: AsyncClient) -> None:
    # 1. Upload valid image
    fake_png_data = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4"
    files = {"file": ("test.png", io.BytesIO(fake_png_data), "image/png")}
    resp1 = await client.post("/v1/media", files=files)
    assert resp1.status_code == 201
    data1 = resp1.json()
    assert data1["media_id"].startswith("med_")
    assert data1["content_type"] == "image/png"

    # 2. Upload identical content again -> should deduplicate
    files2 = {"file": ("another_name.png", io.BytesIO(fake_png_data), "image/png")}
    resp2 = await client.post("/v1/media", files=files2)
    assert resp2.status_code == 201
    data2 = resp2.json()
    assert data2["media_id"] == data1["media_id"]


@pytest.mark.asyncio
async def test_media_upload_disallowed_type(client: AsyncClient) -> None:
    files = {"file": ("script.sh", io.BytesIO(b"echo hello"), "text/plain")}
    resp = await client.post("/v1/media", files=files)
    assert resp.status_code == 400
    assert "Unsupported file type" in resp.text
