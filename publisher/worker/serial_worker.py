"""Single serial worker loop handling job claiming, execution, recovery, and risk control."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import structlog
from publisher.config import PublisherSettings
from publisher.models import MediaAsset, PlatformState, PublishJob
from publisher.notify_alert import emit_notify_hub_alert
from publisher.platforms.base import BasePlatformPublisher
from publisher.worker.clean_artifacts import clean_screenshots
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger()


def parse_error_code(exc: Exception) -> str:
    msg = f"{type(exc).__name__} {exc}".upper()
    known = [
        "SECURITY_CHECK_TRIGGERED",
        "RATE_LIMIT_TRIGGERED",
        "AUTH_REQUIRED",
        "EDITOR_NOT_FOUND",
        "EDITOR_TIMEOUT",
        "CONTENT_REJECTED",
        "COVER_FAILED",
        "DRAFT_SAVE_FAILED",
        "DRAFT_API_RESULT_UNKNOWN",
        "DRAFT_API_REJECTED",
        "DRAFT_OPEN_FAILED",
        "PUBLISH_QUOTA_EXHAUSTED",
        "PUBLISH_NOT_STARTED",
        "PUBLISH_CONFIRM_FAILED",
        "PUBLISH_RESULT_UNKNOWN",
        "PROVIDER_UI_CHANGED",
        "NETWORK_ERROR",
        "MAX_ATTEMPTS_EXCEEDED",
    ]
    for code in known:
        if code in msg:
            return code
    if "TIMEOUT" in msg or "TIMED OUT" in msg:
        return "EDITOR_TIMEOUT"
    if "HTTP" in msg or "CONNECTION" in msg or "NETWORK" in msg:
        return "NETWORK_ERROR"
    return "PROVIDER_UI_CHANGED"


class SerialWorker:
    """Serial worker enforcing single active browser task, state recovery, and safety rules."""

    def __init__(
        self,
        settings: PublisherSettings,
        session_factory: async_sessionmaker[AsyncSession],
        publishers: dict[str, BasePlatformPublisher],
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.publishers = publishers
        self._running = False
        self._loop_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the background serial worker and run recovery."""
        self.settings.ensure_directories()
        clean_screenshots(self.settings.artifacts_dir)
        self._running = True

        for name, pub in self.publishers.items():
            try:
                await pub.start()
            except Exception as exc:
                logger.error("publisher_start_failed", platform=name, error=str(exc))

        # Perform startup recovery
        await self._recover_running_jobs()

        self._loop_task = asyncio.create_task(self._main_loop())
        logger.info("serial_worker_started")

    async def stop(self) -> None:
        """Stop worker and close all browser resources."""
        self._running = False
        if self._loop_task:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task

        for name, pub in self.publishers.items():
            try:
                await pub.close()
            except Exception as exc:
                logger.debug("publisher_close_failed", platform=name, error=str(exc))
        logger.info("serial_worker_stopped")

    async def _recover_running_jobs(self) -> None:
        """Scan jobs stuck in 'running' upon service restart and apply recovery rules."""
        async with self.session_factory() as session:
            stmt = select(PublishJob).where(PublishJob.status == "running")
            res = await session.execute(stmt)
            running_jobs = res.scalars().all()

            for job in running_jobs:
                logger.warning(
                    "recovering_stuck_running_job",
                    job_id=job.id,
                    phase=job.publish_phase,
                    attempts=job.attempt_count,
                )
                if job.publish_phase == "waiting_manual_confirm":
                    # A human-confirmation prompt may have been open when the
                    # worker stopped. Preserve the checkpoint and resume with
                    # reconciliation only after restart.
                    job.status = "waiting_manual_confirm"
                elif job.publish_phase in (
                    "publish_intent",
                    "publish_clicked",
                    "reconciling",
                ):
                    # NEVER click publish again. Switch to reconciling only
                    job.publish_phase = "reconciling"
                    job.status = "queued"
                elif job.publish_phase == "draft_saved":
                    job.status = "queued"
                elif job.publish_phase == "api_creating_draft":
                    # draft/add has no idempotency key. A crash in this phase
                    # leaves the remote result ambiguous, so never call it a
                    # second time automatically.
                    job.status = "failed"
                    job.error_code = "DRAFT_API_RESULT_UNKNOWN"
                    job.error_summary = (
                        "Worker stopped while creating a WeChat API draft; "
                        "inspect the公众号草稿箱 before retrying manually"
                    )
                    job.finished_at = datetime.now(UTC)
                elif job.publish_phase in ("editing", None):
                    if job.attempt_count < job.max_attempts:
                        job.status = "queued"
                        job.publish_phase = None
                    else:
                        job.status = "failed"
                        job.error_code = "MAX_ATTEMPTS_EXCEEDED"
                        job.error_summary = (
                            "Worker crashed during editing and max attempts reached"
                        )
                        job.finished_at = datetime.now(UTC)

            # Clear current_job_id on platforms
            await session.execute(update(PlatformState).values(current_job_id=None))
            await session.commit()

    async def _main_loop(self) -> None:
        while self._running:
            try:
                processed = await self._run_next_eligible_job()
                if not processed:
                    await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("worker_main_loop_error", error=str(exc))
                await asyncio.sleep(3.0)

    async def _run_next_eligible_job(self) -> bool:
        """Find the next eligible queued or waiting job and process it."""
        async with self.session_factory() as session:
            # 1. Check all platform states
            p_res = await session.execute(select(PlatformState))
            platforms = {p.platform: p for p in p_res.scalars().all()}

            # 2. Select eligible jobs
            stmt = (
                select(PublishJob)
                .where(
                    PublishJob.status.in_(
                        ["queued", "waiting_auth", "waiting_manual_confirm"]
                    )
                )
                .order_by(PublishJob.created_at.asc())
            )
            j_res = await session.execute(stmt)
            candidates = j_res.scalars().all()

            target_job: PublishJob | None = None
            now = datetime.now(UTC)

            for job in candidates:
                p_state = platforms.get(job.platform)
                if p_state and p_state.is_paused:
                    # Platform is paused due to risk control; skip
                    continue

                if (
                    job.platform == "xiaohongshu"
                    and p_state
                    and p_state.last_publish_at
                ):
                    cooldown = timedelta(seconds=self.settings.xhs_min_interval_seconds)
                    if (
                        now
                        - p_state.last_publish_at.replace(
                            tzinfo=UTC
                            if p_state.last_publish_at.tzinfo is None
                            else p_state.last_publish_at.tzinfo
                        )
                        < cooldown
                    ):
                        # Xiaohongshu 1800s cooldown active; skip
                        continue

                target_job = job
                break

            if target_job is None:
                return False

            # Lock and update target job to running
            job_id = target_job.id
            platform_name = target_job.platform
            publisher = self.publishers.get(platform_name)

            if publisher is None:
                target_job.status = "failed"
                target_job.error_code = "PLATFORM_NOT_SUPPORTED"
                target_job.error_summary = (
                    f"No publisher adapter registered for {platform_name}"
                )
                target_job.finished_at = datetime.now(UTC)
                await session.commit()
                return True

            # Check authentication on target platform
            is_logged_in = await publisher.check_login()
            if not is_logged_in:
                target_job.status = "waiting_auth"
                p_state = platforms.get(platform_name)
                incident_id = p_state.alert_incident_id if p_state else None
                is_new_incident = not incident_id
                if not incident_id:
                    incident_id = uuid.uuid4().hex[:12]
                qr_b64 = (
                    p_state.qr_code_base64
                    if p_state and incident_id and p_state.qr_code_base64
                    else await publisher.capture_qr()
                )

                await session.execute(
                    update(PlatformState)
                    .where(PlatformState.platform == platform_name)
                    .values(
                        session_state="auth_required",
                        qr_code_base64=qr_b64,
                        alert_incident_id=incident_id,
                        last_error_code="AUTH_REQUIRED",
                        last_error_message="Platform session expired or not authenticated",
                        updated_at=datetime.now(UTC),
                    )
                )
                await session.commit()

                qr_image_path: Path | None = None
                if qr_b64:
                    try:
                        raw_qr = qr_b64.split(",", 1)[-1]
                        qr_bytes = base64.b64decode(raw_qr, validate=True)
                        if qr_bytes:
                            self.settings.artifacts_dir.mkdir(
                                parents=True, exist_ok=True
                            )
                            qr_image_path = (
                                self.settings.artifacts_dir
                                / f"{platform_name}_login_{incident_id}.png"
                            )
                            qr_image_path.write_bytes(qr_bytes)
                    except (binascii.Error, OSError, ValueError) as exc:
                        logger.debug("login_qr_artifact_write_failed", error=str(exc))

                # Emit one alert per auth incident; the worker polls waiting_auth
                # jobs and must not resend the same QR on every loop.
                if is_new_incident:
                    await emit_notify_hub_alert(
                        self.settings,
                        event_type="publisher.session.auth_required",
                        event_key=f"auth-required-{platform_name}-{incident_id}",
                        title=f"【{platform_name}】发布器需要扫码登录",
                        content=(
                            f"平台 {platform_name} 会话已过期，当前任务已暂停并进入 waiting_auth。"
                            f"请打开控制台扫码登录：{self.settings.console_public_url}"
                        ),
                        level="warning",
                        payload={"platform": platform_name, "incident_id": incident_id},
                        image_path=qr_image_path,
                    )
                return False

            # Authenticated! If platform state was auth_required, transition to ready
            p_state = platforms.get(platform_name)
            if p_state and p_state.session_state == "auth_required":
                old_incident = p_state.alert_incident_id
                await session.execute(
                    update(PlatformState)
                    .where(PlatformState.platform == platform_name)
                    .values(
                        session_state="ready",
                        qr_code_base64=None,
                        alert_incident_id=None,
                        last_error_code=None,
                        last_error_message=None,
                        last_auth_at=datetime.now(UTC),
                        updated_at=datetime.now(UTC),
                    )
                )
                if old_incident:
                    await emit_notify_hub_alert(
                        self.settings,
                        event_type="publisher.session.recovered",
                        event_key=f"auth-recovered-{platform_name}-{old_incident}",
                        title=f"【{platform_name}】发布器登录态已恢复",
                        content=(
                            f"平台 {platform_name} 扫码成功，会话恢复为 ready，"
                            "继续执行发布队列。"
                        ),
                        level="info",
                        payload={"platform": platform_name},
                    )

            # Mark job running and increment attempt count if not already in reconciliation
            if target_job.publish_phase not in ("reconciling", "waiting_manual_confirm"):
                target_job.attempt_count += 1
            target_job.status = "running"
            if not target_job.started_at:
                target_job.started_at = datetime.now(UTC)

            await session.execute(
                update(PlatformState)
                .where(PlatformState.platform == platform_name)
                .values(current_job_id=job_id, updated_at=datetime.now(UTC))
            )
            await session.commit()

        # Execute job outside transaction
        await self._execute_job(job_id, platform_name, publisher)
        return True

    async def _execute_job(
        self,
        job_id: str,
        platform_name: str,
        publisher: BasePlatformPublisher,
    ) -> None:
        async with self.session_factory() as session:
            job = await session.get(PublishJob, job_id)
            if not job:
                return

            start_time = job.started_at or datetime.now(UTC)

            temp_media_files: list[Path] = []
            try:
                # 1. Resolve media paths only for a new browser-created draft.
                # API-created drafts and reconciliation do not need to fetch
                # the cover again.
                media_paths: list[Path] = []
                if (
                    not job.platform_draft_id
                    and job.publish_phase
                    not in ("reconciling", "waiting_manual_confirm")
                ):
                    for item in job.media:
                        if item.get("kind") == "uploaded" and item.get("media_id"):
                            m_id = str(item["media_id"])
                            m_asset = await session.get(MediaAsset, m_id)
                            if m_asset:
                                media_paths.append(Path(m_asset.file_path))
                        elif item.get("kind") == "url" and item.get("url"):
                            # Download URL to temp media directory
                            dl_path = await self._download_temp_media(str(item["url"]))
                            if dl_path:
                                media_paths.append(dl_path)
                                temp_media_files.append(dl_path)

                # 2. Check if we are waiting for a human confirmation. This
                # branch is strictly verify/reconcile and never clicks again.
                if job.publish_phase == "waiting_manual_confirm":
                    logger.info("executing_manual_confirmation_reconcile", job_id=job.id)
                    published_url = await publisher.verify_published(job, start_time)
                    if published_url:
                        job.status = "published"
                        job.final_url = published_url
                        job.finished_at = datetime.now(UTC)
                        job.error_code = None
                        job.error_summary = None
                        await self._record_platform_success(
                            session, platform_name, job.id
                        )
                    else:
                        job.status = "waiting_manual_confirm"
                        await self._record_platform_finish(
                            session, platform_name, job.id
                        )
                    await session.commit()
                    return

                # 3. Check if we are resuming from reconciling
                if job.publish_phase == "reconciling":
                    logger.info("executing_reconciling_only", job_id=job.id)
                    published_url = await publisher.reconcile(job, start_time)
                    if published_url:
                        job.status = "published"
                        job.final_url = published_url
                        job.finished_at = datetime.now(UTC)
                        await self._record_platform_success(
                            session, platform_name, job.id
                        )
                    else:
                        job.status = "publish_unknown"
                        job.error_code = "PUBLISH_RESULT_UNKNOWN"
                        job.error_summary = (
                            "Reconciliation could not locate published article "
                            "in management list"
                        )
                        job.finished_at = datetime.now(UTC)
                        await self._record_platform_finish(
                            session, platform_name, job.id
                        )
                        await emit_notify_hub_alert(
                            self.settings,
                            event_type="publisher.job.publish_unknown",
                            event_key=f"job-unknown-{job.id}",
                            title=f"【{platform_name}】发布结果未知 (publish_unknown)",
                            content=(
                                f"任务 {job.id} 标题《{job.content.get('title')}》已点击最终发布，"
                                f"但作品列表核对超时，未自动重新提交以防重复群发。详情：{self.settings.console_public_url}"
                            ),
                            level="critical",
                            payload={"job_id": job.id, "platform": platform_name},
                        )
                    await session.commit()
                    return

                # 3. Check if resuming from draft_saved
                if job.publish_phase == "draft_saved":
                    if job.mode == "draft":
                        job.status = "draft_saved"
                        job.finished_at = datetime.now(UTC)
                        await self._record_platform_finish(
                            session, platform_name, job.id
                        )
                        await session.commit()
                        return

                    # mode is publish: open draft and proceed to publish
                    logger.info(
                        "resuming_from_saved_draft",
                        job_id=job.id,
                        draft_id=job.platform_draft_id,
                    )
                    if not job.final_url and not job.platform_draft_id:
                        raise RuntimeError(
                            "DRAFT_OPEN_FAILED: Saved draft URL or platform draft id is unavailable"
                        )
                    await publisher.open_draft(job.final_url, job=job)
                    job.publish_phase = "publish_intent"
                    await session.commit()

                    publish_outcome = await publisher.publish_and_confirm(job)
                    if publish_outcome == "waiting_manual_confirm":
                        job.publish_phase = "waiting_manual_confirm"
                        job.status = "waiting_manual_confirm"
                        job.error_code = None
                        job.error_summary = None
                        await self._record_platform_finish(
                            session, platform_name, job.id
                        )
                        await session.commit()
                        return
                    job.publish_phase = "publish_clicked"
                    await session.commit()

                    published_url = await publisher.verify_published(job, start_time)
                    if published_url:
                        job.status = "published"
                        job.final_url = published_url
                        job.finished_at = datetime.now(UTC)
                        await self._record_platform_success(
                            session, platform_name, job.id
                        )
                    else:
                        job.status = "publish_unknown"
                        job.error_code = "PUBLISH_RESULT_UNKNOWN"
                        job.error_summary = (
                            "Publish confirmed but published URL could not be retrieved"
                        )
                        job.finished_at = datetime.now(UTC)
                        await self._record_platform_finish(
                            session, platform_name, job.id
                        )
                    await session.commit()
                    return

                # 4. Fresh execution: API creation or browser editing -> draft_saved -> publish
                job.publish_phase = publisher.draft_creation_phase
                await session.commit()

                draft_url, draft_id = await publisher.save_draft(job, media_paths)
                job.publish_phase = "draft_saved"
                job.platform_draft_id = draft_id
                job.final_url = draft_url
                await session.commit()

                if job.mode == "draft":
                    job.status = "draft_saved"
                    job.finished_at = datetime.now(UTC)
                    await self._record_platform_finish(session, platform_name, job.id)
                    await session.commit()
                    return

                # mode == "publish"
                if (job.final_url or job.platform_draft_id) and job.platform != "xiaohongshu":
                    await publisher.open_draft(job.final_url, job=job)

                job.publish_phase = "publish_intent"
                await session.commit()

                publish_outcome = await publisher.publish_and_confirm(job)
                if publish_outcome == "waiting_manual_confirm":
                    job.publish_phase = "waiting_manual_confirm"
                    job.status = "waiting_manual_confirm"
                    job.error_code = None
                    job.error_summary = None
                    await self._record_platform_finish(
                        session, platform_name, job.id
                    )
                    await session.commit()
                    return
                job.publish_phase = "publish_clicked"
                await session.commit()

                published_url = await publisher.verify_published(job, start_time)
                if published_url:
                    job.status = "published"
                    job.final_url = published_url
                    job.finished_at = datetime.now(UTC)
                    await self._record_platform_success(session, platform_name, job.id)
                else:
                    job.status = "publish_unknown"
                    job.error_code = "PUBLISH_RESULT_UNKNOWN"
                    job.error_summary = (
                        "Publish confirmed but public URL could not be retrieved "
                        "immediately"
                    )
                    job.finished_at = datetime.now(UTC)
                    await self._record_platform_finish(session, platform_name, job.id)
                    await emit_notify_hub_alert(
                        self.settings,
                        event_type="publisher.job.publish_unknown",
                        event_key=f"job-unknown-{job.id}",
                        title=f"【{platform_name}】发布结果未知 (publish_unknown)",
                        content=(
                            f"任务 {job.id} 标题《{job.content.get('title')}》已点击最终发布，"
                            f"但核对超时未确认。请人工在平台检查。控制台：{self.settings.console_public_url}"
                        ),
                        level="critical",
                        payload={"job_id": job.id, "platform": platform_name},
                    )
                await session.commit()

            except Exception as exc:
                await self._handle_job_failure(session, job, platform_name, exc)
            finally:
                for temp_file in temp_media_files:
                    try:
                        if temp_file.exists():
                            temp_file.unlink()
                    except Exception as e:
                        logger.debug(
                            "cleanup_temp_media_failed",
                            path=str(temp_file),
                            error=str(e),
                        )

    async def _handle_job_failure(
        self,
        session: AsyncSession,
        job: PublishJob,
        platform_name: str,
        exc: Exception,
    ) -> None:
        err_code = parse_error_code(exc)
        err_msg = str(exc)[:1000]
        logger.error(
            "job_execution_failed",
            job_id=job.id,
            platform=platform_name,
            code=err_code,
            error=err_msg,
        )

        job.error_code = err_code
        job.error_summary = err_msg

        # Check risk triggers (security check, captcha, rate limit)
        if err_code in ("SECURITY_CHECK_TRIGGERED", "RATE_LIMIT_TRIGGERED"):
            job.status = "failed"
            job.finished_at = datetime.now(UTC)
            incident_id = uuid.uuid4().hex[:12]

            await session.execute(
                update(PlatformState)
                .where(PlatformState.platform == platform_name)
                .values(
                    is_paused=True,
                    paused_reason=f"{err_code}: {err_msg}",
                    paused_at=datetime.now(UTC),
                    alert_incident_id=incident_id,
                    last_error_code=err_code,
                    last_error_message=err_msg,
                    current_job_id=None,
                    last_job_id=job.id,
                    updated_at=datetime.now(UTC),
                )
            )
            await session.commit()

            # Emit platform paused alert
            await emit_notify_hub_alert(
                self.settings,
                event_type="publisher.platform.paused",
                event_key=f"platform-paused-{platform_name}-{incident_id}",
                title=f"【{platform_name}】触发风控，平台已自动暂停！",
                content=(
                    f"平台 {platform_name} 在执行任务 {job.id} 时检测到【{err_code}】异常。\n"
                    f"错误摘要：{err_msg}\n"
                    f"后续任务已暂停执行。请在控制台人工处理并点击【恢复发布】：{self.settings.console_public_url}"
                ),
                level="critical",
                payload={
                    "platform": platform_name,
                    "job_id": job.id,
                    "error_code": err_code,
                },
            )
            return

        if err_code == "AUTH_REQUIRED":
            # Session expired during execution: release back to waiting_auth without attempt penalty
            job.status = "waiting_auth"
            job.attempt_count = max(0, job.attempt_count - 1)
            await session.execute(
                update(PlatformState)
                .where(PlatformState.platform == platform_name)
                .values(
                    session_state="auth_required",
                    current_job_id=None,
                    last_error_code="AUTH_REQUIRED",
                    last_error_message=err_msg,
                    updated_at=datetime.now(UTC),
                )
            )
            await session.commit()
            return

        # Check retryability
        non_retryable = {
            "CONTENT_REJECTED",
            "PUBLISH_QUOTA_EXHAUSTED",
            "DRAFT_OPEN_FAILED",
            "PUBLISH_NOT_STARTED",
            "PUBLISH_CONFIRM_FAILED",
            "PROVIDER_UI_CHANGED",
            "DRAFT_API_RESULT_UNKNOWN",
            "DRAFT_API_REJECTED",
        }
        if job.publish_phase == "waiting_manual_confirm":
            # Reconciliation errors while waiting for the administrator are
            # safe to retry; never return to the publish click path.
            job.status = "waiting_manual_confirm"
            await self._record_platform_finish(session, platform_name, job.id)
            await session.commit()
            return
        if err_code == "PUBLISH_NOT_STARTED":
            # No publish action was clicked.  Keep the saved-draft checkpoint
            # and report a real failure instead of entering post-click
            # reconciliation, which is reserved for an ambiguous click.
            job.publish_phase = "draft_saved"
            job.status = "failed"
            job.finished_at = datetime.now(UTC)
            await emit_notify_hub_alert(
                self.settings,
                event_type="publisher.job.failed",
                event_key=f"job-failed-{job.id}",
                title=f"【{platform_name}】发表动作未开始",
                content=(
                    f"任务 {job.id} 标题《{job.content.get('title')}》尚未点击发表。\n"
                    f"错误信息：{err_msg}\n控制台：{self.settings.console_public_url}"
                ),
                level="warning",
                payload={"job_id": job.id, "error_code": err_code},
            )
            await self._record_platform_finish(session, platform_name, job.id)
            await session.commit()
            return
        if job.publish_phase == "api_creating_draft":
            if err_code == "DRAFT_API_RESULT_UNKNOWN":
                job.status = "failed"
                job.finished_at = datetime.now(UTC)
                await emit_notify_hub_alert(
                    self.settings,
                    event_type="publisher.job.draft_unknown",
                    event_key=f"job-draft-unknown-{job.id}",
                    title=f"【{platform_name}】草稿创建结果未知",
                    content=(
                        f"任务 {job.id} 标题《{job.content.get('title')}》调用微信官方 API 后未收到确定响应，"
                        "为避免重复创建草稿，系统不会自动重试。请在公众号草稿箱核对。"
                        f"控制台：{self.settings.console_public_url}"
                    ),
                    level="critical",
                    payload={"job_id": job.id, "platform": platform_name},
                )
                await self._record_platform_finish(session, platform_name, job.id)
                await session.commit()
                return
            # A provider rejection happened before draft/add was accepted, so
            # it is safe to retry according to the normal attempt budget.
            job.publish_phase = None
        # Once publish_intent or publish_clicked, switch to reconciling instead
        # of aborting without a check.
        if job.publish_phase in ("publish_intent", "publish_clicked"):
            job.publish_phase = "reconciling"
            job.status = "queued"
            logger.warning(
                "job_failed_during_publish_switched_to_reconcile",
                job_id=job.id,
                phase=job.publish_phase,
                error=err_msg,
            )
        elif job.publish_phase == "reconciling":
            job.status = "publish_unknown"
            job.error_code = "PUBLISH_RESULT_UNKNOWN"
            job.error_summary = f"Reconciliation failed after exception: {err_msg}"
            job.finished_at = datetime.now(UTC)
            await emit_notify_hub_alert(
                self.settings,
                event_type="publisher.job.publish_unknown",
                event_key=f"job-unknown-{job.id}",
                title=f"【{platform_name}】发布结果未知 (publish_unknown)",
                content=(
                    f"任务 {job.id} 标题《{job.content.get('title')}》在作品核对期间失败。\n"
                    f"错误信息：{err_msg}\n控制台：{self.settings.console_public_url}"
                ),
                level="critical",
                payload={"job_id": job.id, "platform": platform_name},
            )
        elif err_code not in non_retryable and job.attempt_count < job.max_attempts:
            # Retryable network/timeout error before publish_clicked
            job.status = "queued"
            if job.publish_phase != "draft_saved":
                job.publish_phase = None
            logger.info(
                "job_scheduled_for_retry", job_id=job.id, attempt=job.attempt_count
            )
        else:
            job.status = "failed"
            job.finished_at = datetime.now(UTC)
            await emit_notify_hub_alert(
                self.settings,
                event_type="publisher.job.failed",
                event_key=f"job-failed-{job.id}",
                title=f"【{platform_name}】任务发布失败",
                content=(
                    f"任务 {job.id} 标题《{job.content.get('title')}》执行失败（{err_code}）。\n"
                    f"错误信息：{err_msg}\n控制台：{self.settings.console_public_url}"
                ),
                level="warning",
                payload={"job_id": job.id, "error_code": err_code},
            )

        await self._record_platform_finish(session, platform_name, job.id)
        await session.commit()

    async def _record_platform_success(
        self,
        session: AsyncSession,
        platform_name: str,
        job_id: str,
    ) -> None:
        now = datetime.now(UTC)
        await session.execute(
            update(PlatformState)
            .where(PlatformState.platform == platform_name)
            .values(
                current_job_id=None,
                last_job_id=job_id,
                last_publish_at=now,
                last_error_code=None,
                last_error_message=None,
                updated_at=now,
            )
        )

    async def _record_platform_finish(
        self,
        session: AsyncSession,
        platform_name: str,
        job_id: str,
    ) -> None:
        now = datetime.now(UTC)
        await session.execute(
            update(PlatformState)
            .where(PlatformState.platform == platform_name)
            .values(
                current_job_id=None,
                last_job_id=job_id,
                updated_at=now,
            )
        )

    async def _download_temp_media(self, url: str) -> Path | None:
        try:
            # Notify Hub may expose media through an internal HTTPS endpoint
            # with a locally managed certificate; preserve that compatibility.
            async with httpx.AsyncClient(timeout=15.0, verify=False) as client:  # noqa: S501
                resp = await client.get(url)
                if resp.status_code == 200:
                    ext = ".jpg"
                    ct = resp.headers.get("Content-Type", "")
                    if "png" in ct:
                        ext = ".png"
                    elif "webp" in ct:
                        ext = ".webp"
                    temp_file = (
                        self.settings.media_dir / f"temp_{uuid.uuid4().hex[:12]}{ext}"
                    )
                    temp_file.write_bytes(resp.content)
                    return temp_file
        except Exception as exc:
            logger.debug("download_temp_media_failed", url=url, error=str(exc))
        return None
