"""Tests for SerialWorker state transitions, recovery rules, risk handling, and throttling."""

from __future__ import annotations

import base64
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from publisher.config import PublisherSettings
from publisher.models import MediaAsset, PlatformState, PublishJob, generate_id, utc_now
from publisher.platforms.fake import FakePublisher
from publisher.worker.serial_worker import SerialWorker
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


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
async def test_missing_cover_does_not_promote_later_inline_media(
    test_settings: PublisherSettings,
    test_db: tuple[object, async_sessionmaker[AsyncSession]],
    tmp_path: Path,
) -> None:
    _engine, session_factory = test_db
    pub = FakePublisher(test_settings)
    worker = SerialWorker(test_settings, session_factory, {"wechat_mp": pub})
    inline_path = tmp_path / "inline.png"
    inline_path.write_bytes(b"inline-image")

    job_id = generate_id("job")
    async with session_factory() as session:
        session.add(
            MediaAsset(
                id="med_inline_existing",
                file_name=inline_path.name,
                file_path=str(inline_path),
                content_type="image/png",
                size_bytes=inline_path.stat().st_size,
                checksum="inline-checksum",
            )
        )
        session.add(
            PublishJob(
                id=job_id,
                client_request_id="missing-cover-no-promotion-01",
                platform="wechat_mp",
                mode="draft",
                status="queued",
                content={"title": "封面缺失", "body_text": "正文"},
                media=[
                    {"kind": "uploaded", "media_id": "med_missing_cover"},
                    {"kind": "uploaded", "media_id": "med_inline_existing"},
                ],
                topics=[],
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )
        await session.commit()

    with patch(
        "publisher.worker.serial_worker.emit_notify_hub_alert",
        new_callable=AsyncMock,
    ):
        assert await worker._run_next_eligible_job() is True

    assert pub.save_draft_calls == 0
    async with session_factory() as session:
        job = await session.get(PublishJob, job_id)
        assert job is not None
        assert job.error_code == "COVER_FAILED"
        assert job.status == "queued"


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
    assert pub.open_draft_calls == 1
    assert pub.publish_and_confirm_calls == 1
    assert pub.verify_published_calls == 1

    async with session_factory() as session:
        j = await session.get(PublishJob, job_id)
        assert j is not None
        assert j.status == "published"
        assert j.final_url == "https://example.com/item/123"


@pytest.mark.asyncio
async def test_xhs_fresh_publish_keeps_current_editor_page(
    test_settings: PublisherSettings,
    test_db: tuple[object, async_sessionmaker[AsyncSession]],
) -> None:
    _engine, session_factory = test_db
    pub = FakePublisher(test_settings)
    worker = SerialWorker(test_settings, session_factory, {"xiaohongshu": pub})

    job_id = generate_id("job")
    async with session_factory() as session:
        session.add(
            PublishJob(
                id=job_id,
                client_request_id="xhs-publish-current-page-01",
                platform="xiaohongshu",
                mode="publish",
                status="queued",
                content={"title": "小红书发布", "body_text": "正文"},
                media=[],
                topics=[],
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )
        await session.commit()

    assert await worker._run_next_eligible_job() is True
    assert pub.save_draft_calls == 1
    assert pub.open_draft_calls == 0
    assert pub.publish_and_confirm_calls == 1


@pytest.mark.asyncio
async def test_draft_saved_without_platform_id_resumes_without_new_draft(
    test_settings: PublisherSettings,
    test_db: tuple[object, async_sessionmaker[AsyncSession]],
) -> None:
    _engine, session_factory = test_db
    pub = FakePublisher(test_settings)
    worker = SerialWorker(test_settings, session_factory, {"wechat_mp": pub})

    job_id = generate_id("job")
    async with session_factory() as session:
        session.add(
            PublishJob(
                id=job_id,
                client_request_id="resume-without-draft-id-01",
                platform="wechat_mp",
                mode="publish",
                status="queued",
                publish_phase="draft_saved",
                platform_draft_id=None,
                final_url="https://mp.weixin.qq.com/cgi-bin/appmsg?id=1",
                content={"title": "恢复草稿", "body_text": "正文"},
                media=[],
                topics=[],
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )
        await session.commit()

    assert await worker._run_next_eligible_job() is True
    assert pub.save_draft_calls == 0
    assert pub.open_draft_calls == 1
    assert pub.publish_and_confirm_calls == 1


@pytest.mark.asyncio
async def test_api_draft_creation_recovery_never_retries_unknown_request(
    test_settings: PublisherSettings,
    test_db: tuple[object, async_sessionmaker[AsyncSession]],
) -> None:
    _engine, session_factory = test_db
    pub = FakePublisher(test_settings)
    worker = SerialWorker(test_settings, session_factory, {"wechat_mp": pub})

    job_id = generate_id("job")
    async with session_factory() as session:
        session.add(
            PublishJob(
                id=job_id,
                client_request_id="api-draft-unknown-recovery-01",
                platform="wechat_mp",
                mode="publish",
                status="running",
                publish_phase="api_creating_draft",
                content={"title": "未知草稿结果", "body_text": "正文"},
                media=[],
                topics=[],
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )
        await session.commit()

    await worker._recover_running_jobs()

    async with session_factory() as session:
        recovered = await session.get(PublishJob, job_id)
        assert recovered is not None
        assert recovered.status == "failed"
        assert recovered.error_code == "DRAFT_API_RESULT_UNKNOWN"
        assert recovered.publish_phase == "api_creating_draft"

    assert pub.save_draft_calls == 0


@pytest.mark.asyncio
async def test_api_draft_checkpoint_publishes_without_creating_browser_draft(
    test_settings: PublisherSettings,
    test_db: tuple[object, async_sessionmaker[AsyncSession]],
) -> None:
    _engine, session_factory = test_db
    pub = FakePublisher(test_settings)
    worker = SerialWorker(test_settings, session_factory, {"wechat_mp": pub})

    job_id = generate_id("job")
    async with session_factory() as session:
        session.add(
            PublishJob(
                id=job_id,
                client_request_id="api-draft-publish-01",
                platform="wechat_mp",
                mode="publish",
                status="queued",
                publish_phase="draft_saved",
                platform_draft_id="api-media-id-opaque-001",
                final_url=None,
                content={"title": "API 草稿发布", "body_text": "正文"},
                media=[],
                topics=[],
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )
        await session.commit()

    assert await worker._run_next_eligible_job() is True
    assert pub.save_draft_calls == 0
    assert pub.open_draft_calls == 1
    assert pub.publish_and_confirm_calls == 1


@pytest.mark.asyncio
async def test_manual_confirmation_waits_and_only_reconciles(
    test_settings: PublisherSettings,
    test_db: tuple[object, async_sessionmaker[AsyncSession]],
) -> None:
    _engine, session_factory = test_db
    pub = FakePublisher(test_settings)
    pub.publish_outcome = "waiting_manual_confirm"
    pub.published_url_to_return = None
    worker = SerialWorker(test_settings, session_factory, {"wechat_mp": pub})

    job_id = generate_id("job")
    async with session_factory() as session:
        session.add(
            PublishJob(
                id=job_id,
                client_request_id="manual-confirm-01",
                platform="wechat_mp",
                mode="publish",
                status="queued",
                publish_phase="draft_saved",
                platform_draft_id="api-media-id-manual-001",
                content={"title": "等待确认", "body_text": "正文"},
                media=[],
                topics=[],
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )
        await session.commit()

    assert await worker._run_next_eligible_job() is True
    async with session_factory() as session:
        waiting = await session.get(PublishJob, job_id)
        assert waiting is not None
        assert waiting.status == "waiting_manual_confirm"
        assert waiting.publish_phase == "waiting_manual_confirm"
        assert waiting.attempt_count == 1

    assert pub.publish_and_confirm_calls == 1
    assert pub.verify_published_calls == 0

    # A polling pass must not click publish again.
    assert await worker._run_next_eligible_job() is True
    assert pub.publish_and_confirm_calls == 1
    assert pub.verify_published_calls == 1

    pub.published_url_to_return = "https://example.com/manual-confirmed"
    assert await worker._run_next_eligible_job() is True
    assert pub.publish_and_confirm_calls == 1
    assert pub.verify_published_calls == 2
    async with session_factory() as session:
        published = await session.get(PublishJob, job_id)
        assert published is not None
        assert published.status == "published"


@pytest.mark.asyncio
async def test_publish_not_started_is_not_treated_as_post_click_reconcile(
    test_settings: PublisherSettings,
    test_db: tuple[object, async_sessionmaker[AsyncSession]],
) -> None:
    _engine, session_factory = test_db
    pub = FakePublisher(test_settings)
    pub.should_fail_publish = RuntimeError(
        "PUBLISH_NOT_STARTED: publish entry button was unavailable"
    )
    worker = SerialWorker(test_settings, session_factory, {"wechat_mp": pub})

    job_id = generate_id("job")
    async with session_factory() as session:
        session.add(
            PublishJob(
                id=job_id,
                client_request_id="publish-not-started-01",
                platform="wechat_mp",
                mode="publish",
                status="queued",
                content={"title": "未点击发表", "body_text": "正文"},
                media=[],
                topics=[],
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )
        await session.commit()

    assert await worker._run_next_eligible_job() is True
    assert pub.publish_and_confirm_calls == 1
    assert pub.reconcile_calls == 0
    async with session_factory() as session:
        failed = await session.get(PublishJob, job_id)
        assert failed is not None
        assert failed.status == "failed"
        assert failed.publish_phase == "draft_saved"
        assert failed.error_code == "PUBLISH_NOT_STARTED"


@pytest.mark.asyncio
async def test_api_draft_open_retry_preserves_phase_and_does_not_duplicate(
    test_settings: PublisherSettings,
    test_db: tuple[object, async_sessionmaker[AsyncSession]],
) -> None:
    _engine, session_factory = test_db
    pub = FakePublisher(test_settings)
    pub.should_fail_open_draft = RuntimeError(
        "DRAFT_OPEN_FAILED: draft not visible yet"
    )
    worker = SerialWorker(test_settings, session_factory, {"wechat_mp": pub})

    job_id = generate_id("job")
    async with session_factory() as session:
        session.add(
            PublishJob(
                id=job_id,
                client_request_id="retry-saved-draft-01",
                platform="wechat_mp",
                mode="publish",
                status="queued",
                publish_phase="draft_saved",
                final_url="https://mp.weixin.qq.com/cgi-bin/appmsg?id=1",
                content={"title": "重试草稿", "body_text": "正文"},
                media=[],
                topics=[],
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )
        await session.commit()

    assert await worker._run_next_eligible_job() is True
    async with session_factory() as session:
        failed_once = await session.get(PublishJob, job_id)
        assert failed_once is not None
        assert failed_once.status == "queued"
        assert failed_once.publish_phase == "draft_saved"
        assert failed_once.error_code == "DRAFT_OPEN_FAILED"

    pub.should_fail_open_draft = None
    assert await worker._run_next_eligible_job() is True
    assert pub.save_draft_calls == 0
    assert pub.open_draft_calls == 2
    assert pub.publish_and_confirm_calls == 1
    async with session_factory() as session:
        published = await session.get(PublishJob, job_id)
        assert published is not None
        assert published.status == "published"
        assert published.error_code is None
        assert published.error_summary is None


@pytest.mark.asyncio
async def test_in_flight_publish_failure_switches_to_reconcile_and_reconciles(
    test_settings: PublisherSettings,
    test_db: tuple[object, async_sessionmaker[AsyncSession]],
) -> None:
    """If publish_and_confirm throws in-flight, job must switch to reconciling, NOT fail immediately without check."""
    engine, session_factory = test_db
    pub = FakePublisher(test_settings)
    pub.should_fail_publish = RuntimeError(
        "Simulated network timeout during final click"
    )
    worker = SerialWorker(test_settings, session_factory, {"wechat_mp": pub})

    job_id = generate_id("job")
    async with session_factory() as session:
        job = PublishJob(
            id=job_id,
            client_request_id="in-flight-fail-01",
            platform="wechat_mp",
            mode="publish",
            status="queued",
            content={"title": "发布中途异常测试", "body_text": "正文"},
            media=[],
            topics=[],
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        session.add(job)
        await session.commit()

    # 1. Run first execution: fails during publish_and_confirm
    processed = await worker._run_next_eligible_job()
    assert processed is True
    assert pub.publish_and_confirm_calls == 1

    # 2. Check that job was switched to reconciling and requeued
    async with session_factory() as session:
        j = await session.get(PublishJob, job_id)
        assert j is not None
        assert j.status == "queued"
        assert j.publish_phase == "reconciling"

    # 3. Next execution runs reconciliation only (NEVER clicks publish again)
    pub.should_fail_publish = None
    processed2 = await worker._run_next_eligible_job()
    assert processed2 is True
    assert pub.publish_and_confirm_calls == 1  # Still 1! Never re-clicked
    assert pub.reconcile_calls == 1

    async with session_factory() as session:
        j = await session.get(PublishJob, job_id)
        assert j is not None
        assert j.status == "published"


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
async def test_recovery_from_publish_intent_never_clicks_again(
    test_settings: PublisherSettings,
    test_db: tuple[object, async_sessionmaker[AsyncSession]],
) -> None:
    """If service crashed after publish_intent was written, recovery must ONLY reconcile and NEVER click publish."""
    engine, session_factory = test_db
    pub = FakePublisher(test_settings)
    worker = SerialWorker(test_settings, session_factory, {"wechat_mp": pub})

    job_id = generate_id("job")
    async with session_factory() as session:
        job = PublishJob(
            id=job_id,
            client_request_id="crash-recovery-intent-01",
            platform="wechat_mp",
            mode="publish",
            status="running",  # stuck in running
            publish_phase="publish_intent",  # crashed right around final publish click
            attempt_count=1,
            content={"title": "意图崩溃恢复测试", "body_text": "正文"},
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
async def test_auth_alert_includes_login_qr_image(
    test_settings: PublisherSettings,
    test_db: tuple[object, async_sessionmaker[AsyncSession]],
    tmp_path: object,
) -> None:
    _engine, session_factory = test_db
    test_settings.data_dir = tmp_path  # type: ignore[assignment]
    qr_data = base64.b64encode(b"qr-image").decode("ascii")
    pub = FakePublisher(test_settings, is_logged_in=False, qr_data=qr_data)
    worker = SerialWorker(test_settings, session_factory, {"wechat_mp": pub})

    job_id = generate_id("job")
    async with session_factory() as session:
        session.add(
            PublishJob(
                id=job_id,
                client_request_id="auth-qr-alert-01",
                platform="wechat_mp",
                mode="publish",
                status="queued",
                content={"title": "登录二维码告警", "body_text": "正文"},
                media=[],
                topics=[],
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )
        await session.commit()

    with patch(
        "publisher.worker.serial_worker.emit_notify_hub_alert",
        new_callable=AsyncMock,
    ) as alert:
        await worker._run_next_eligible_job()
        await worker._run_next_eligible_job()

    assert alert.await_count == 1
    qr_path = alert.call_args.kwargs["image_path"]
    assert qr_path is not None
    assert qr_path.is_file()
    assert qr_path.read_bytes() == b"qr-image"


@pytest.mark.asyncio
async def test_publish_unknown_when_unconfirmed(
    test_settings: PublisherSettings,
    test_db: tuple[object, async_sessionmaker[AsyncSession]],
) -> None:
    _engine, session_factory = test_db
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

    with patch(
        "publisher.worker.serial_worker.emit_notify_hub_alert",
        new_callable=AsyncMock,
    ) as alert:
        processed = await worker._run_next_eligible_job()
        assert processed is True
        assert pub.publish_and_confirm_calls == 1
        assert pub.verify_published_calls == 1
        alert.assert_not_awaited()

        async with session_factory() as session:
            pending = await session.get(PublishJob, job_id)
            assert pending is not None
            assert pending.status == "queued"
            assert pending.publish_phase == "reconciling"
            assert pending.error_code is None

        processed = await worker._run_next_eligible_job()
        assert processed is True

    assert pub.publish_and_confirm_calls == 1
    assert pub.reconcile_calls == 1
    alert.assert_awaited_once()

    async with session_factory() as session:
        j = await session.get(PublishJob, job_id)
        assert j is not None
        assert j.status == "publish_unknown"
        assert j.publish_phase == "reconciling"
        assert j.error_code == "PUBLISH_RESULT_UNKNOWN"
