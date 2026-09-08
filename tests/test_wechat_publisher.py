"""Tests for WeChat MP platform publisher adapter and HTML rendering."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from publisher.config import PublisherSettings
from publisher.platforms.render import render_wechat_html
from publisher.platforms.wechat import WeChatPublisher


def test_render_wechat_html_strips_duplicate_h1_and_cleans_quotes() -> None:
    raw_markdown = """# 这是文章标题

> 这是第一行引用
> 这是第二行引用

正文第一段说明。

- 列表项一
- 列表项二
"""
    html = render_wechat_html(
        content=raw_markdown,
        source_url="https://x.com/user/status/123",
        strip_title_heading=True,
    )

    # 1. First H1 should be stripped so it does not duplicate the article title
    assert "这是文章标题" not in html

    # 2. Blockquote should be rendered as HTML without raw '>' characters
    assert "<blockquote" in html
    assert "&gt;" not in html
    assert "> 这是第一行引用" not in html
    assert "这是第一行引用" in html

    # 3. List should be rendered
    assert "<ul" in html
    assert "<li" in html
    assert "列表项一" in html

    # 4. Source URL should be appended
    assert "原文链接：" in html
    assert "https://x.com/user/status/123" in html


def test_render_wechat_html_standalone_quote_arrow() -> None:
    raw_markdown = """> 第一段引用
>
> 第二段引用包含单独一行的箭头

正文内容。
"""
    html = render_wechat_html(
        content=raw_markdown,
        strip_title_heading=True,
    )

    assert "<blockquote" in html
    assert "&gt;" not in html
    assert "第一段引用" in html
    assert "第二段引用包含单独一行的箭头" in html


@pytest.mark.asyncio
async def test_wechat_check_login_success(test_settings: PublisherSettings) -> None:
    publisher = WeChatPublisher(test_settings)
    mock_page = MagicMock()
    mock_page.url = "https://mp.weixin.qq.com/cgi-bin/home?t=home/index"

    with patch.object(publisher, "get_page", new_callable=AsyncMock) as mock_get_page:
        mock_get_page.return_value = mock_page
        logged_in = await publisher.check_login()
        assert logged_in is True


@pytest.mark.asyncio
async def test_wechat_check_login_failure(test_settings: PublisherSettings) -> None:
    publisher = WeChatPublisher(test_settings)
    mock_page = MagicMock()
    mock_page.url = "https://mp.weixin.qq.com/"
    mock_locator = AsyncMock()
    mock_locator.count.return_value = 0
    mock_page.locator.return_value = mock_locator
    mock_page.get_by_text.return_value = mock_locator

    with patch.object(publisher, "get_page", new_callable=AsyncMock) as mock_get_page:
        mock_get_page.return_value = mock_page
        logged_in = await publisher.check_login()
        assert logged_in is False


@pytest.mark.asyncio
async def test_wechat_check_risk_control_rate_limit(
    test_settings: PublisherSettings,
) -> None:
    publisher = WeChatPublisher(test_settings)
    mock_page = MagicMock()

    # Rate limit dialog visible
    rate_loc = AsyncMock()
    rate_loc.count.return_value = 1
    rate_loc.first.is_visible.return_value = True
    mock_page.locator.return_value = rate_loc

    with pytest.raises(RuntimeError, match="RATE_LIMIT_TRIGGERED"):
        await publisher.check_risk_control(mock_page)


@pytest.mark.asyncio
async def test_wechat_check_risk_control_security_check(
    test_settings: PublisherSettings,
) -> None:
    publisher = WeChatPublisher(test_settings)
    mock_page = MagicMock()

    # Rate limit dialog not visible
    empty_loc = AsyncMock()
    empty_loc.count.return_value = 0

    # Captcha iframe visible
    captcha_loc = AsyncMock()
    captcha_loc.count.return_value = 1
    captcha_loc.first.is_visible.return_value = True

    def locator_side_effect(selector: str) -> AsyncMock:
        if "tcaptcha" in selector or "安全验证" in selector:
            return captcha_loc
        return empty_loc

    mock_page.locator.side_effect = locator_side_effect

    with pytest.raises(RuntimeError, match="SECURITY_CHECK_TRIGGERED"):
        await publisher.check_risk_control(mock_page)


@pytest.mark.asyncio
async def test_wechat_open_editor_with_shared_browser_manager(
    test_settings: PublisherSettings,
) -> None:
    """Verify open_editor gets context from BrowserManager without AttributeError."""
    mock_browser_manager = MagicMock()
    mock_context = MagicMock()
    mock_browser_manager.get_context = AsyncMock(return_value=mock_context)

    publisher = WeChatPublisher(test_settings, browser_manager=mock_browser_manager)

    mock_editor_page = MagicMock()
    mock_editor_page.wait_for_load_state = AsyncMock()

    class AsyncPageContextManager:
        async def __aenter__(self) -> Any:
            fut: asyncio.Future[Any] = asyncio.Future()
            fut.set_result(mock_editor_page)
            val = MagicMock()
            val.value = fut
            return val

        async def __aexit__(self, *args: Any) -> None:
            pass

    mock_context.expect_page.return_value = AsyncPageContextManager()

    mock_home_page = MagicMock()
    mock_home_page.url = "https://mp.weixin.qq.com/cgi-bin/home?t=home/index"
    article_btn = AsyncMock()
    article_btn.count.return_value = 1
    article_btn.first.is_visible.return_value = True
    article_btn.first.click = AsyncMock()
    mock_home_page.locator.return_value = article_btn

    editor_page = await publisher.open_editor(mock_home_page)
    assert editor_page == mock_editor_page
    mock_context.expect_page.assert_called_once()


@pytest.mark.asyncio
async def test_wechat_publish_disables_group_notice_and_accepts_no_declaration(
    test_settings: PublisherSettings,
) -> None:
    publisher = WeChatPublisher(test_settings)
    publisher.check_risk_control = AsyncMock()  # type: ignore[method-assign]
    publisher._disable_group_notification = AsyncMock()  # type: ignore[method-assign]

    page = MagicMock()
    entry = MagicMock()
    entry.first = entry
    entry.count = AsyncMock(return_value=1)
    entry.is_visible = AsyncMock(return_value=True)
    entry.inner_text = AsyncMock(return_value="发表")
    entry.click = AsyncMock()
    page.locator.return_value = entry

    publish_button = MagicMock()
    publish_button.click = AsyncMock()
    no_declaration_button = MagicMock()
    no_declaration_button.click = AsyncMock()

    stage = 0

    async def click_publish() -> None:
        nonlocal stage
        stage = 1

    publish_button.click.side_effect = click_publish

    async def visible_button(_page: Any, names: list[str]) -> Any | None:
        if "无需声明并发表" in names and stage == 1:
            return no_declaration_button
        if "发表" in names and stage == 0:
            return publish_button
        return None

    publisher._visible_button = AsyncMock(side_effect=visible_button)  # type: ignore[method-assign]
    page.get_by_text.return_value = MagicMock(
        count=AsyncMock(return_value=0),
        first=MagicMock(is_visible=AsyncMock(return_value=False)),
    )

    with patch.object(publisher, "get_page", new_callable=AsyncMock) as get_page:
        get_page.return_value = page
        await publisher.publish_and_confirm(MagicMock())

    entry.click.assert_awaited_once()
    publisher._disable_group_notification.assert_awaited_once_with(page)
    publish_button.click.assert_awaited_once()
    no_declaration_button.click.assert_awaited_once()


@pytest.mark.asyncio
async def test_wechat_disables_enabled_group_notification() -> None:
    page = MagicMock()
    label = MagicMock()
    label.first = label
    label.count = AsyncMock(return_value=1)
    label.is_visible = AsyncMock(return_value=True)
    row = MagicMock()
    enabled_switch = MagicMock()
    enabled_switch.first = enabled_switch
    enabled_switch.count = AsyncMock(return_value=1)
    enabled_switch.click = AsyncMock()
    label.locator.return_value = row
    row.locator.return_value = enabled_switch
    page.get_by_text.return_value = label

    await WeChatPublisher._disable_group_notification(page)
    enabled_switch.click.assert_awaited_once_with(force=True)


@pytest.mark.asyncio
async def test_wechat_handle_admin_verification_flow(
    test_settings: PublisherSettings, tmp_path: Any
) -> None:
    from pathlib import Path
    from publisher.models import PublishJob

    test_settings.data_dir = Path(tmp_path)
    publisher = WeChatPublisher(test_settings)

    page = MagicMock()
    page.frames = []
    page.is_closed.return_value = False

    dialog_loc = MagicMock()
    dialog_loc.count = AsyncMock(return_value=1)
    dialog_loc.first = dialog_loc
    dialog_loc.is_visible = AsyncMock(side_effect=[True, True, False])
    dialog_loc.inner_text = AsyncMock(return_value="微信验证\n请使用管理员微信号扫码")
    dialog_loc.screenshot = AsyncMock()

    page.locator.return_value = dialog_loc
    page.inner_text = AsyncMock(return_value="已发表成功")

    job = PublishJob(
        platform="wechat_mp",
        mode="publish",
        content={"title": "测试文章标题"},
    )

    with patch(
        "publisher.notify_alert.emit_notify_hub_alert", new_callable=AsyncMock
    ) as mock_alert:
        mock_alert.return_value = True
        handled = await publisher._handle_admin_verification(page, job)
        assert handled is True
        dialog_loc.screenshot.assert_awaited_once()
        mock_alert.assert_awaited_once()
        call_kwargs = mock_alert.call_args.kwargs
        assert call_kwargs["event_type"] == "publisher.wechat.verify_qr"
        assert "测试文章标题" in call_kwargs["content"]
        assert call_kwargs["image_path"] is not None


@pytest.mark.asyncio
async def test_emit_notify_hub_alert_with_image(tmp_path: Any) -> None:
    from pathlib import Path
    from pydantic import SecretStr
    import httpx
    from publisher.notify_alert import emit_notify_hub_alert

    test_img = Path(tmp_path) / "test_qr.png"
    test_img.write_bytes(b"dummy-png-data")

    settings = PublisherSettings(
        notify_event_url="http://hub.test/api/v1/events",
        notify_api_key=SecretStr("test-key"),
        notify_recipient_ids=["admin_user"],
    )

    upload_called = False
    event_called = False

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal upload_called, event_called
        if request.url.path == "/api/v1/media":
            upload_called = True
            assert request.headers.get("X-API-Key") == "test-key"
            return httpx.Response(201, json={"id": "media_test_qr_123"})
        elif request.url.path == "/api/v1/events":
            event_called = True
            body = request.read().decode()
            assert "media_test_qr_123" in body
            assert "image" in body
            return httpx.Response(202, json={"status": "accepted"})
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)
    with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        res = await emit_notify_hub_alert(
            settings,
            event_type="publisher.wechat.verify_qr",
            event_key="test_key_1",
            title="验证码",
            content="请扫码",
            image_path=test_img,
        )
        assert res is True
        assert upload_called is True
        assert event_called is True
