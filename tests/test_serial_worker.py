"""Tests for SerialWorker state transitions, recovery rules, risk handling, and throttling."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from publisher.config import PublisherSettings
from publisher.models import PlatformState, PublishJob, generate_id, utc_now
from publisher.platforms.fake import FakePublisher
from publisher.worker.serial_worker import SerialWorker


@pytest.mark.asyncio
async def test_worker_draft_mode(
    test_settings: PublisherSettings,
    test_db: tuple[object, async_sessionmaker[AsyncSession]],
) -> None:
    engine, session_factory = test_db
    pub = FakePublisher(test_settings)
    worker = SerialWorker(test_settings, session_factory, {"wechat_mp": pub})

    # Create draft job in DB
    job_id = generate_id("job")
    async with session_factory() as session:
        job = PublishJob(
            id=job_id,
            client_request_id="draft-test-01",
            platform="wechat_mp",
            mode="draft",
            status="queued",
            content={"title": "草稿测试", "body_text": "正文"},
            media=[],
            topics=[],
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        session.add(job)
        await session.commit()

    # Run one iteration
    processed = await worker._run_next_eligible_job()
    assert processed is True
    assert pub.save_draft_calls == 1
    assert pub.publish_and_confirm_calls == 0

    # Verify DB status
    async with session_factory() as session:
        j = await session.get(PublishJob, job_id)
        assert j is not None
        assert j.status == "draft_saved"
        assert j.publish_phase == "draft_saved"
        assert j.platform_draft_id == f"draft_{job_id}"


@pytest.mark.asyncio
async def test_worker_publish_mode(
    test_settings: PublisherSettings,
    test_db: tuple[object, async_sessionmaker[AsyncSession]],
) -> None:
    engine, session_factory = test_db
    pub = FakePublisher(test_settings)
    worker = SerialWorker(test_settings, session_factory, {"wechat_mp": pub})

    job_id = generate_id("job")
    async with session_factory() as session:
        job = PublishJob(
            id=job_id,
            client_request_id="publish-test-01",
            platform="wechat_mp",
            mode="publish",
            status="queued",
            content={"title": "发布测试", "body_text": "正文"},
            media=[],
            topics=[],
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        session.add(job)
        await session.commit()

    processed = await worker._run_next_eligible_job()
    assert processed is True
    assert pub.save_draft_calls == 1
    assert pub.publish_and_confirm_calls == 1
    assert pub.verify_published_calls == 1

    async with session_factory() as session:
        j = await session.get(PublishJob, job_id)
        assert j is not None
        assert j.status == "published"
        assert j.final_url == "https://example.com/item/123"


@pytest.mark.asyncio
async def test_recovery_from_publish_clicked_never_clicks_again(
    test_settings: PublisherSettings,
    test_db: tuple[object, async_sessionmaker[AsyncSession]],
) -> None:
    """If service crashed after publish_clicked, recovery must ONLY reconcile and NEVER click publish."""
    engine, session_factory = test_db
    pub = FakePublisher(test_settings)
    worker = SerialWorker(test_settings, session_factory, {"wechat_mp": pub})

    job_id = generate_id("job")
    async with session_factory() as session:
        job = PublishJob(
            id=job_id,
            client_request_id="crash-recovery-01",
            platform="wechat_mp",
            mode="publish",
            status="running",  # stuck in running
            publish_phase="publish_clicked",  # had clicked publish before crash
            attempt_count=1,
            content={"title": "崩溃恢复测试", "body_text": "正文"},
            media=[],
            topics=[],
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        session.add(job)
        await session.commit()

    # 1. Run recovery
    await worker._recover_running_jobs()

    # 2. Check that phase was transitioned to reconciling
    async with session_factory() as session:
        j = await session.get(PublishJob, job_id)
        assert j is not None
        assert j.status == "queued"
        assert j.publish_phase == "reconciling"

    # 3. Process job
    processed = await worker._run_next_eligible_job()
    assert processed is True
    assert pub.publish_and_confirm_calls == 0  # Crucial! NEVER clicked publish again
    assert pub.reconcile_calls == 1

    async with session_factory() as session:
        j = await session.get(PublishJob, job_id)
        assert j is not None
        assert j.status == "published"


@pytest.mark.asyncio
async def test_risk_control_pauses_platform(
    test_settings: PublisherSettings,
    test_db: tuple[object, async_sessionmaker[AsyncSession]],
) -> None:
    engine, session_factory = test_db
    pub = FakePublisher(test_settings)
    pub.should_trigger_security_check = True  # simulate captcha detection
    worker = SerialWorker(test_settings, session_factory, {"xiaohongshu": pub})

    job1_id = generate_id("job")
    job2_id = generate_id("job")
    async with session_factory() as session:
        job1 = PublishJob(
            id=job1_id,
            client_request_id="risk-test-01",
            platform="xiaohongshu",
            mode="draft",
            status="queued",
            content={"title": "风控测试1", "body_text": "正文"},
            media=[],
            topics=[],
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        job2 = PublishJob(
            id=job2_id,
            client_request_id="risk-test-02",
            platform="xiaohongshu",
            mode="draft",
            status="queued",
            content={"title": "风控测试2", "body_text": "正文"},
            media=[],
            topics=[],
            created_at=utc_now() + timedelta(seconds=1),
            updated_at=utc_now() + timedelta(seconds=1),
        )
        session.add_all([job1, job2])
        await session.commit()

    # Process job 1 -> should fail and pause platform
    p1 = await worker._run_next_eligible_job()
    assert p1 is True

    async with session_factory() as session:
        j1 = await session.get(PublishJob, job1_id)
        assert j1 is not None
        assert j1.status == "failed"
        assert j1.error_code == "SECURITY_CHECK_TRIGGERED"

        p_state = await session.get(PlatformState, "xiaohongshu")
        assert p_state is not None
        assert p_state.is_paused is True
        assert "SECURITY_CHECK_TRIGGERED" in (p_state.paused_reason or "")

    # Process next job -> should be skipped because platform is paused
    p2 = await worker._run_next_eligible_job()
    assert p2 is False

    async with session_factory() as session:
        j2 = await session.get(PublishJob, job2_id)
        assert j2 is not None
        assert j2.status == "queued"  # Left in queued, not failed!


@pytest.mark.asyncio
async def test_auth_expired_enters_waiting_auth(
    test_settings: PublisherSettings,
    test_db: tuple[object, async_sessionmaker[AsyncSession]],
) -> None:
    engine, session_factory = test_db
    pub = FakePublisher(test_settings, is_logged_in=False, qr_data="fake_login_qr")
    worker = SerialWorker(test_settings, session_factory, {"wechat_mp": pub})

    job_id = generate_id("job")
    async with session_factory() as session:
        job = PublishJob(
            id=job_id,
            client_request_id="auth-test-01",
            platform="wechat_mp",
            mode="publish",
            status="queued",
            content={"title": "未登录测试", "body_text": "正文"},
            media=[],
            topics=[],
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        session.add(job)
        await session.commit()

    p = await worker._run_next_eligible_job()
    assert p is False

    async with session_factory() as session:
        j = await session.get(PublishJob, job_id)
        assert j is not None
        assert j.status == "waiting_auth"
        assert j.attempt_count == 0  # No attempt penalty!

        p_state = await session.get(PlatformState, "wechat_mp")
        assert p_state is not None
        assert p_state.session_state == "auth_required"
        assert p_state.qr_code_base64 == "fake_login_qr"

    # Now simulate user scans QR code and logs in!
    pub.is_logged_in = True
    processed = await worker._run_next_eligible_job()
    assert processed is True

    async with session_factory() as session:
        j_resumed = await session.get(PublishJob, job_id)
        assert j_resumed is not None
        assert j_resumed.status == "published"
        p_state_resumed = await session.get(PlatformState, "wechat_mp")
        assert p_state_resumed is not None
        assert p_state_resumed.session_state == "ready"
        assert p_state_resumed.qr_code_base64 is None


@pytest.mark.asyncio
async def test_publish_unknown_when_unconfirmed(
    test_settings: PublisherSettings,
    test_db: tuple[object, async_sessionmaker[AsyncSession]],
) -> None:
    engine, session_factory = test_db
    pub = FakePublisher(test_settings)
    pub.published_url_to_return = None  # verification fails to find public URL
    worker = SerialWorker(test_settings, session_factory, {"wechat_mp": pub})

    job_id = generate_id("job")
    async with session_factory() as session:
        job = PublishJob(
            id=job_id,
            client_request_id="unknown-test-01",
            platform="wechat_mp",
            mode="publish",
            status="queued",
            content={"title": "未确认测试", "body_text": "正文"},
            media=[],
            topics=[],
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        session.add(job)
        await session.commit()

    processed = await worker._run_next_eligible_job()
    assert processed is True
    assert pub.publish_and_confirm_calls == 1

    async with session_factory() as session:
        j = await session.get(PublishJob, job_id)
        assert j is not None
        assert j.status == "publish_unknown"
        assert j.publish_phase == "publish_clicked"
        assert j.error_code == "PUBLISH_RESULT_UNKNOWN"

