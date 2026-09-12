"""Tests for the official WeChat MP API draft path."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from publisher.config import PublisherSettings
from publisher.models import PublishJob
from publisher.platforms.wechat import WeChatPublisher
from publisher.platforms.wechat_api import WeChatApiClient, WeChatApiError


def make_settings(tmp_path: Path) -> PublisherSettings:
    return PublisherSettings(
        data_dir=tmp_path / "data",
        wechat_mp_app_id="wx-test-app",
        wechat_mp_app_secret=SecretStr("wechat-test-secret"),
        wechat_mp_api_base_url="https://api.weixin.test",
        wechat_mp_author="默认作者",
    )


def make_job() -> PublishJob:
    return PublishJob(
        id="job-official-api",
        platform="wechat_mp",
        mode="publish",
        source_url="https://source.example/article/1",
        content={
            "title": "官方 API 草稿",
            "body_text": "第一段\n\n第二段",
            "author": "文章作者",
            "digest": "文章摘要",
        },
        media=[],
    )


def test_wechat_api_base_url_preserves_reverse_proxy_path_prefix(
    tmp_path: Path,
) -> None:
    settings = PublisherSettings(
        data_dir=tmp_path / "data",
        wechat_mp_api_base_url="https://proxy.example/wechat-api/",
    )

    assert settings.wechat_mp_api_base_url == "https://proxy.example/wechat-api/"


@pytest.mark.asyncio
async def test_wechat_api_creates_complete_draft_without_browser_editor(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/cgi-bin/token"):
            assert request.url.params["appid"] == "wx-test-app"
            assert request.url.params["secret"] == "wechat-test-secret"
            return httpx.Response(
                200, json={"access_token": "api-token", "expires_in": 7200}
            )
        if request.url.path.endswith("/cgi-bin/material/add_material"):
            assert request.url.params["access_token"] == "api-token"
            return httpx.Response(200, json={"media_id": "cover-media-id"})
        if request.url.path.endswith("/cgi-bin/draft/add"):
            assert request.url.params["access_token"] == "api-token"
            article = json.loads(request.content)["articles"][0]
            assert article["title"] == "官方 API 草稿"
            assert article["author"] == "文章作者"
            assert article["digest"] == "文章摘要"
            assert "第一段" in article["content"]
            assert "第二段" in article["content"]
            assert "https://source.example/article/1" in article["content"]
            assert article["thumb_media_id"] == "cover-media-id"
            assert article["need_open_comment"] == 1
            assert article["only_fans_can_comment"] == 0
            return httpx.Response(200, json={"media_id": "draft-media-id"})
        raise AssertionError(f"unexpected path {request.url.path}")

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.weixin.test"
    )
    settings = make_settings(tmp_path)
    api = WeChatApiClient(settings, http_client=http)
    publisher = WeChatPublisher(settings, wechat_api_client=api)
    cover_path = settings.media_dir / "cover.png"
    settings.ensure_directories()
    cover_path.write_bytes(b"fake-png")
    publisher.get_page = AsyncMock(
        side_effect=AssertionError("browser editor must not run")
    )  # type: ignore[method-assign]

    try:
        draft_url, draft_id = await publisher.save_draft(make_job(), [cover_path])
    finally:
        await http.aclose()

    assert draft_url is None
    assert draft_id == "draft-media-id"
    assert publisher.draft_creation_phase == "api_creating_draft"
    assert [request.url.path for request in requests] == [
        "/cgi-bin/token",
        "/cgi-bin/material/add_material",
        "/cgi-bin/draft/add",
    ]


@pytest.mark.asyncio
async def test_wechat_api_uploads_inline_images_before_creating_html_draft(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/cgi-bin/token"):
            return httpx.Response(
                200, json={"access_token": "api-token", "expires_in": 7200}
            )
        if request.url.path.endswith("/cgi-bin/media/uploadimg"):
            assert request.url.params["access_token"] == "api-token"
            assert b"inline-media-id.jpg" in request.content
            assert b"inline-image" in request.content
            return httpx.Response(200, json={"url": "https://mmbiz.qpic.cn/inline/1"})
        if request.url.path.endswith("/cgi-bin/material/add_material"):
            return httpx.Response(200, json={"media_id": "cover-media-id"})
        if request.url.path.endswith("/cgi-bin/draft/add"):
            article = json.loads(request.content)["articles"][0]
            assert "publisher-media://" not in article["content"]
            assert "https://mmbiz.qpic.cn/inline/1" in article["content"]
            assert '<img src="https://mmbiz.qpic.cn/inline/1"' in article["content"]
            return httpx.Response(200, json={"media_id": "draft-media-id"})
        raise AssertionError(f"unexpected path {request.url.path}")

    settings = make_settings(tmp_path)
    settings.ensure_directories()
    cover_path = settings.media_dir / "cover.png"
    inline_path = settings.media_dir / "inline-media-id.jpg"
    cover_path.write_bytes(b"fake-png")
    inline_path.write_bytes(b"inline-image")

    job = make_job()
    job.content = {
        **job.content,
        "body_html": '<p>正文</p><img src="publisher-media://inline-media-id" />',
    }

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.weixin.test"
    )
    api = WeChatApiClient(settings, http_client=http)
    publisher = WeChatPublisher(settings, wechat_api_client=api)
    try:
        draft_url, draft_id = await publisher.save_draft(job, [cover_path, inline_path])
    finally:
        await http.aclose()

    assert draft_url is None
    assert draft_id == "draft-media-id"
    assert [request.url.path for request in requests] == [
        "/cgi-bin/token",
        "/cgi-bin/media/uploadimg",
        "/cgi-bin/material/add_material",
        "/cgi-bin/draft/add",
    ]


@pytest.mark.asyncio
async def test_wechat_api_refreshes_invalid_token_once(tmp_path: Path) -> None:
    token_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if request.url.path.endswith("/cgi-bin/token"):
            token_calls += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"token-{token_calls}",
                    "expires_in": 7200,
                },
            )
        if request.url.path.endswith("/cgi-bin/draft/add"):
            if request.url.params["access_token"] == "token-1":
                return httpx.Response(
                    200,
                    json={"errcode": 40001, "errmsg": "invalid credential"},
                )
            return httpx.Response(200, json={"media_id": "draft-2"})
        raise AssertionError(f"unexpected path {request.url.path}")

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.weixin.test"
    )
    client = WeChatApiClient(make_settings(tmp_path), http_client=http)
    try:
        assert await client.add_draft([{"title": "test"}]) == "draft-2"
    finally:
        await http.aclose()
    assert token_calls == 2


@pytest.mark.asyncio
async def test_wechat_api_draft_timeout_is_marked_result_unknown(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/cgi-bin/token"):
            return httpx.Response(
                200, json={"access_token": "api-token", "expires_in": 7200}
            )
        if request.url.path.endswith("/cgi-bin/draft/add"):
            raise httpx.ReadTimeout("response lost")
        raise AssertionError(f"unexpected path {request.url.path}")

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.weixin.test"
    )
    client = WeChatApiClient(make_settings(tmp_path), http_client=http)
    try:
        with pytest.raises(WeChatApiError) as error:
            await client.add_draft([{"title": "test"}])
    finally:
        await http.aclose()

    assert error.value.result_unknown is True
    assert error.value.retryable is False
