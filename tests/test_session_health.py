"""Regression coverage for expired sessions, durable alerts, and idle recovery."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from publisher.config import PublisherSettings
from publisher.models import PlatformState, PublishJob, utc_now
from publisher.platforms.fake import FakePublisher
from publisher.platforms.wechat import MP_HOME_URL, WeChatPublisher
from publisher.security import safe_error_summary
from publisher.session_service import SessionService
from publisher.worker.serial_worker import SerialWorker


def page_with_text(text: str) -> Any:
    page = MagicMock()
    page.url = "https://mp.weixin.qq.com/cgi-bin/appmsg?token=revoked"
    empty = MagicMock()
    empty.count = AsyncMock(return_value=0)
    visible = MagicMock()
    visible.count = AsyncMock(return_value=1)
    visible.first.is_visible = AsyncMock(return_value=True)
    page.get_by_text.side_effect = lambda value, **kwargs: (
        visible if value == text else empty
    )
    page.locator.return_value = empty
    page.goto = AsyncMock()
    return page


@pytest.mark.parametrize("text", ["登录超时", "请重新登录"])
async def test_http_200_original_cgi_url_is_not_login(
    test_settings: PublisherSettings, text: str
) -> None:
    pub = WeChatPublisher(test_settings)
    page = page_with_text(text)
    pub.get_page = AsyncMock(return_value=page)
    assert await pub.check_login() is False
    with pytest.raises(RuntimeError, match="AUTH_REQUIRED"):
        await pub._ensure_page_token(page)
    page.goto.assert_awaited_once_with(
        MP_HOME_URL, timeout=30000, wait_until="domcontentloaded"
    )


async def test_draft_token_is_refreshed_from_home(
    test_settings: PublisherSettings,
) -> None:
    pub = WeChatPublisher(test_settings)
    page = page_with_text("")

    async def navigate(*args: Any, **kwargs: Any) -> None:
        page.url = "https://mp.weixin.qq.com/cgi-bin/home?token=current"

    page.goto.side_effect = navigate
    assert await pub._ensure_page_token(page) == "current"
    page.goto.assert_awaited_once()


async def test_alert_retries_and_deduplicates_across_restart(
    test_settings: PublisherSettings,
    test_db: tuple[object, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = test_db
    pub = FakePublisher(test_settings, is_logged_in=False)
    alert = AsyncMock(side_effect=[False, True, True])
    with patch("publisher.session_service.emit_notify_hub_alert", alert):
        await SessionService(test_settings, factory).record_auth_required(
            "wechat_mp", pub
        )
        await SessionService(test_settings, factory).record_auth_required(
            "wechat_mp", pub
        )
        await SessionService(test_settings, factory).record_auth_required(
            "wechat_mp", pub
        )
        assert alert.await_count == 2
        assert (
            alert.call_args_list[0].kwargs["event_key"]
            == alert.call_args_list[1].kwargs["event_key"]
        )
        await SessionService(test_settings, factory).record_ready("wechat_mp")
        assert alert.call_args.kwargs["event_type"] == "publisher.session.recovered"
    async with factory() as session:
        state = await session.get(PlatformState, "wechat_mp")
        assert state.session_state == "ready"
        assert state.alert_incident_id is None
        assert state.qr_code_base64 is None


async def test_hourly_probe_does_not_reset_login_age_or_revoke_on_timeout(
    test_settings: PublisherSettings,
    test_db: tuple[object, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = test_db
    now = datetime(2026, 10, 3, tzinfo=UTC)
    login = now - timedelta(days=3)
    async with factory() as session:
        state = await session.get(PlatformState, "wechat_mp")
        state.last_auth_at = login
        await session.commit()
    pub = FakePublisher(test_settings)
    pub.refresh_session = AsyncMock(return_value=True)
    worker = SerialWorker(test_settings, factory, {"wechat_mp": pub}, clock=lambda: now)
    await worker._maintain_sessions()
    await worker._maintain_sessions()
    assert pub.refresh_session.await_count == 1
    now += timedelta(hours=1)
    pub.refresh_session.side_effect = TimeoutError("navigation timeout ?token=private")
    with patch("publisher.session_service.emit_notify_hub_alert", AsyncMock()) as alert:
        await worker._maintain_sessions()
        alert.assert_not_awaited()
    async with factory() as session:
        state = await session.get(PlatformState, "wechat_mp")
        assert state.session_state == "ready"
        assert state.last_auth_at == login.replace(tzinfo=None)
        assert state.last_error_code == "EDITOR_TIMEOUT"
        assert "private" not in state.last_error_message


async def test_idle_qr_recovery_and_shared_browser_lock(
    test_settings: PublisherSettings,
    test_db: tuple[object, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = test_db
    now = datetime.now(UTC)
    async with factory() as session:
        state = await session.get(PlatformState, "wechat_mp")
        state.session_state = "auth_required"
        state.last_session_check_at = now - timedelta(seconds=16)
        await session.commit()
    pub = FakePublisher(test_settings)
    worker = SerialWorker(test_settings, factory, {"wechat_mp": pub}, clock=lambda: now)

    async def probe() -> bool:
        assert pub.operation_lock.locked()
        return True

    pub.refresh_session = AsyncMock(side_effect=probe)
    await worker._maintain_sessions()
    async with factory() as session:
        state = await session.get(PlatformState, "wechat_mp")
        assert state.session_state == "ready"
        state.is_paused = True
        state.last_session_check_at = None
        await session.commit()
    await worker._maintain_sessions()
    assert pub.refresh_session.await_count == 1


def test_navigation_error_redacts_credentials() -> None:
    summary = safe_error_summary(
        RuntimeError(
            "goto https://example.test/?token=secret1&access_token=secret2&begin=0"
        )
    )
    assert "secret1" not in summary and "secret2" not in summary
    assert "begin=0" in summary


async def test_auth_expiry_after_publish_intent_only_reconciles(
    test_settings: PublisherSettings,
    test_db: tuple[object, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = test_db
    now = utc_now()
    pub = FakePublisher(test_settings, api_draft=True)
    pub.should_fail_publish = RuntimeError(
        "AUTH_REQUIRED: session revoked during publish"
    )
    worker = SerialWorker(test_settings, factory, {"wechat_mp": pub}, clock=lambda: now)
    async with factory() as session:
        session.add(
            PublishJob(
                id="job_auth_after_intent",
                client_request_id="auth-after-intent",
                platform="wechat_mp",
                mode="publish",
                status="queued",
                content={"title": "Test", "body_text": "Body"},
                media=[],
                topics=[],
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()
    with patch(
        "publisher.session_service.emit_notify_hub_alert", AsyncMock(return_value=True)
    ):
        assert await worker._run_next_eligible_job()
        async with factory() as session:
            job = await session.get(PublishJob, "job_auth_after_intent")
            assert job.status == "waiting_auth"
            assert job.publish_phase == "reconciling"
        now += timedelta(seconds=16)
        await worker._maintain_sessions()
        assert await worker._run_next_eligible_job()
    assert pub.publish_and_confirm_calls == 1
    assert pub.save_draft_calls == 1
    assert pub.reconcile_calls == 1
