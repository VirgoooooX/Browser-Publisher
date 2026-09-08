"""Tests for Job API endpoints, validation rules, idempotency, and cancellation."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_create_job_idempotency(client: AsyncClient) -> None:
    payload = {
        "client_request_id": "test-req-001",
        "platform": "wechat_mp",
        "mode": "publish",
        "content": {
            "title": "测试文章标题",
            "body_text": "文章正文内容",
            "author": "Tester",
        },
        "media": [{"kind": "url", "url": "https://example.com/cover.jpg"}],
    }

    # 1. First submission
    resp1 = await client.post("/v1/jobs", json=payload)
    assert resp1.status_code == 202
    data1 = resp1.json()
    assert data1["id"].startswith("job_")
    assert data1["status"] == "queued"
    assert data1["effective_mode"] == "publish"

    # 2. Resubmission with identical client_request_id
    resp2 = await client.post("/v1/jobs", json=payload)
    assert resp2.status_code == 202
    data2 = resp2.json()
    assert data2["id"] == data1["id"]
    assert data2["status"] == data1["status"]


@pytest.mark.asyncio
async def test_wechat_mode_default_resolution(client: AsyncClient) -> None:
    payload = {
        "client_request_id": "test-req-mode-wechat",
        "platform": "wechat_mp",
        "mode": None,  # should default to settings.wechat_mp_default_mode ("publish")
        "content": {
            "title": "微信模式解析测试",
            "body_text": "正文内容",
        },
        "media": [{"kind": "url", "url": "https://example.com/pic.jpg"}],
    }
    resp = await client.post("/v1/jobs", json=payload)
    assert resp.status_code == 202
    assert resp.json()["effective_mode"] == "publish"


@pytest.mark.asyncio
async def test_xhs_mode_default_resolution(client: AsyncClient) -> None:
    payload = {
        "client_request_id": "test-req-mode-xhs",
        "platform": "xiaohongshu",
        "mode": None,  # should default to settings.xhs_default_mode ("draft")
        "content": {
            "title": "小红书标题",  # <= 20 chars
            "body_text": "笔记正文内容",
        },
        "media": [{"kind": "url", "url": "https://example.com/pic1.jpg"}],
    }
    resp = await client.post("/v1/jobs", json=payload)
    assert resp.status_code == 202
    assert resp.json()["effective_mode"] == "draft"


@pytest.mark.asyncio
async def test_xhs_validation_rules(client: AsyncClient) -> None:
    # 1. Title > 20 chars rejected
    long_title_payload = {
        "client_request_id": "test-xhs-long-title",
        "platform": "xiaohongshu",
        "content": {
            "title": "这是一个超过二十个字的小红书笔记标题测试样例不应被接受",
            "body_text": "正文",
        },
        "media": [{"kind": "url", "url": "https://example.com/1.jpg"}],
    }
    resp = await client.post("/v1/jobs", json=long_title_payload)
    assert resp.status_code == 422
    assert "cannot exceed 20 characters" in resp.text

    # 2. Missing images (0 images) rejected
    zero_media_payload = {
        "client_request_id": "test-xhs-no-media",
        "platform": "xiaohongshu",
        "content": {"title": "合规标题", "body_text": "正文"},
        "media": [],
    }
    resp2 = await client.post("/v1/jobs", json=zero_media_payload)
    assert resp2.status_code == 422
    assert "requires between 1 and 18 media images" in resp2.text

    # 3. Media > 18 images rejected
    too_many_media = [{"kind": "url", "url": f"https://example.com/{i}.jpg"} for i in range(19)]
    resp3 = await client.post(
        "/v1/jobs",
        json={
            "client_request_id": "test-xhs-too-many-media",
            "platform": "xiaohongshu",
            "content": {"title": "合规标题", "body_text": "正文"},
            "media": too_many_media,
        },
    )
    assert resp3.status_code == 422
    assert "requires between 1 and 18 media images" in resp3.text


@pytest.mark.asyncio
async def test_wechat_validation_rules(client: AsyncClient) -> None:
    # Title > 64 chars rejected
    resp = await client.post(
        "/v1/jobs",
        json={
            "client_request_id": "test-mp-long-title",
            "platform": "wechat_mp",
            "content": {
                "title": "A" * 65,
                "body_text": "正文",
            },
            "media": [{"kind": "url", "url": "https://example.com/cover.jpg"}],
        },
    )
    assert resp.status_code == 422
    assert "cannot exceed 64 characters" in resp.text


@pytest.mark.asyncio
async def test_job_query_and_cancel(client: AsyncClient) -> None:
    payload = {
        "client_request_id": "test-job-cancel",
        "platform": "wechat_mp",
        "content": {"title": "待取消任务", "body_text": "内容"},
        "media": [{"kind": "url", "url": "https://example.com/pic.jpg"}],
    }
    create_resp = await client.post("/v1/jobs", json=payload)
    job_id = create_resp.json()["id"]

    # Query details
    get_resp = await client.get(f"/v1/jobs/{job_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == "queued"

    # Cancel job
    cancel_resp = await client.post(f"/v1/jobs/{job_id}/cancel")
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["status"] == "cancelled"

    # Cannot cancel again
    cancel_again = await client.post(f"/v1/jobs/{job_id}/cancel")
    assert cancel_again.status_code == 400
