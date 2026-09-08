"""Tests for Xiaohongshu platform publisher adapter and risk controls."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from publisher.config import PublisherSettings
from publisher.models import PublishJob, generate_id, utc_now
from publisher.platforms.xiaohongshu import XiaohongshuPublisher


@pytest.mark.asyncio
async def test_xhs_check_login_success(test_settings: PublisherSettings) -> None:
    pub = XiaohongshuPublisher(test_settings)
    mock_page = MagicMock()
    mock_page.url = "https://creator.xiaohongshu.com/creator/home"

    mock_loc = AsyncMock()
    mock_loc.count.return_value = 1
    mock_loc.first.is_visible.return_value = True
    mock_page.locator.return_value = mock_loc

    with patch.object(pub, "get_page", new_callable=AsyncMock) as mock_get_page:
        mock_get_page.return_value = mock_page
        logged_in = await pub.check_login()
        assert logged_in is True


@pytest.mark.asyncio
async def test_xhs_check_login_failure(test_settings: PublisherSettings) -> None:
    pub = XiaohongshuPublisher(test_settings)
    mock_page = MagicMock()
    mock_page.url = "https://creator.xiaohongshu.com/login"

    with patch.object(pub, "get_page", new_callable=AsyncMock) as mock_get_page:
        mock_get_page.return_value = mock_page
        logged_in = await pub.check_login()
        assert logged_in is False


@pytest.mark.asyncio
async def test_xhs_check_risk_control_rate_limit(test_settings: PublisherSettings) -> None:
    pub = XiaohongshuPublisher(test_settings)
    mock_page = MagicMock()

    # Match "操作过于频繁"
    notice_loc = AsyncMock()
    notice_loc.count.return_value = 1
    notice_loc.first.is_visible.return_value = True
    mock_page.get_by_text.return_value = notice_loc

    with pytest.raises(RuntimeError, match="RATE_LIMIT_TRIGGERED"):
        await pub.check_risk_control(mock_page)


@pytest.mark.asyncio
async def test_xhs_check_risk_control_security_captcha(test_settings: PublisherSettings) -> None:
    pub = XiaohongshuPublisher(test_settings)
    mock_page = MagicMock()

    empty_loc = AsyncMock()
    empty_loc.count.return_value = 0
    mock_page.get_by_text.return_value = empty_loc

    captcha_loc = AsyncMock()
    captcha_loc.count.return_value = 1
    captcha_loc.first.is_visible.return_value = True

    def locator_side_effect(selector: str) -> AsyncMock:
        if "captcha" in selector or "geetest" in selector:
            return captcha_loc
        return empty_loc

    mock_page.locator.side_effect = locator_side_effect

    with pytest.raises(RuntimeError, match="SECURITY_CHECK_TRIGGERED"):
        await pub.check_risk_control(mock_page)


@pytest.mark.asyncio
async def test_xhs_save_draft_flow(test_settings: PublisherSettings, tmp_path: Path) -> None:
    pub = XiaohongshuPublisher(test_settings)
    mock_page = MagicMock()
    mock_page.url = "https://creator.xiaohongshu.com/publish/publish"
    mock_page.goto = AsyncMock()
    mock_page.evaluate = AsyncMock()

    # Create dummy local test image
    img_file = tmp_path / "test1.jpg"
    img_file.write_bytes(b"dummy")

    # Mock locators for upload, title, body, save draft
    visible_loc = AsyncMock()
    visible_loc.count.return_value = 1
    visible_loc.first.is_visible.return_value = True
    visible_loc.first.click = AsyncMock()
    visible_loc.first.fill = AsyncMock()
    visible_loc.first.set_input_files = AsyncMock()
    visible_loc.click = AsyncMock()

    # Toast locator
    toast_loc = AsyncMock()
    toast_loc.count.return_value = 1
    toast_loc.first.is_visible.return_value = True

    mock_page.locator.return_value = visible_loc
    mock_page.get_by_text.return_value = toast_loc

    job = PublishJob(
        id=generate_id("job"),
        client_request_id="xhs-draft-unit",
        platform="xiaohongshu",
        mode="draft",
        status="running",
        content={"title": "小红书单元测试标题", "body_text": "笔记正文内容"},
        media=[{"kind": "uploaded", "media_id": "med_1"}],
        topics=["OpenAI", "Codex"],
        created_at=utc_now(),
        updated_at=utc_now(),
    )

    with patch.object(pub, "get_page", new_callable=AsyncMock) as mock_get_page, \
         patch.object(pub, "check_risk_control", new_callable=AsyncMock):
        mock_get_page.return_value = mock_page
        draft_url, draft_id = await pub.save_draft(job, [img_file])

        assert draft_id.startswith("xhs_draft_")
        assert visible_loc.first.set_input_files.awaited
        assert visible_loc.first.fill.awaited
