"""Tests for WeChat MP platform publisher adapter and HTML rendering."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from publisher.config import PublisherSettings
from publisher.models import PublishJob, generate_id, utc_now
from publisher.platforms.render import render_body, render_wechat_html
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
async def test_wechat_check_risk_control_rate_limit(test_settings: PublisherSettings) -> None:
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
async def test_wechat_check_risk_control_security_check(test_settings: PublisherSettings) -> None:
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
