import json
from unittest.mock import patch

import httpx
import pytest
from pydantic import SecretStr

from publisher.config import PublisherSettings
from publisher.notify_alert import emit_notify_hub_alert


@pytest.mark.parametrize("event,category", [
    ("publisher.session.recovered", "login"),
    ("publisher.job.failed", "publish-error"),
    ("publisher.wechat.manual_confirm", "publish-confirm"),
])
async def test_category_cover_and_short_copy(event, category):
    events = []

    def handler(request):
        if request.url.path == "/api/v1/media":
            assert (category + ".png").encode() in request.content
            return httpx.Response(201, json={"id": "media_cover"})
        events.append(json.loads(request.content))
        return httpx.Response(202)

    settings = PublisherSettings(notify_event_url="https://notify.test/api/v1/events",
        notify_api_key=SecretStr("test-key"), notify_recipient_ids=["person_test"])
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with patch("publisher.notify_alert.httpx.AsyncClient", return_value=client):
        assert await emit_notify_hub_alert(settings, event_type=event, event_key="stable-key",
            title="old", content="Call log: internal details that must not be shown",
            subject="文章标题", payload={"platform": "wechat_mp"})
    message = events[0]
    assert message["message_type"] == "article"
    assert message["media_asset_id"] == "media_cover"
    assert "文章标题" in message["title"]
    assert "Call log" not in message["content"]
    assert message["event_key"] == "stable-key"


async def test_media_failure_preserves_text_notification():
    events = []

    def handler(request):
        if request.url.path == "/api/v1/media":
            return httpx.Response(403)
        events.append(json.loads(request.content))
        return httpx.Response(202)

    settings = PublisherSettings(notify_event_url="https://notify.test/api/v1/events",
        notify_api_key=SecretStr("test-key"), notify_recipient_ids=["person_test"])
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with patch("publisher.notify_alert.httpx.AsyncClient", return_value=client):
        assert await emit_notify_hub_alert(settings, event_type="publisher.session.recovered",
            event_key="stable-key", title="old", content="old", payload={"platform": "wechat_mp"})
    assert "message_type" not in events[0]
    assert "登录已恢复" in events[0]["title"]
