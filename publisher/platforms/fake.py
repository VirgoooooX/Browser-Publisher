"""Mock publisher adapter for testing state transitions, timeouts, and risk handling."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from publisher.config import PublisherSettings
from publisher.models import PublishJob
from publisher.platforms.base import BasePlatformPublisher


class FakePublisher(BasePlatformPublisher):
    def __init__(
        self,
        settings: PublisherSettings,
        *,
        is_logged_in: bool = True,
        qr_data: str | None = "fake_qr_data_base64",
    ) -> None:
        super().__init__(settings)
        self.is_logged_in = is_logged_in
        self.qr_data = qr_data
        self.started = False
        self.closed = False

        # Injected behaviors for testing
        self.should_fail_save_draft: Exception | None = None
        self.should_fail_publish: Exception | None = None
        self.should_trigger_security_check: bool = False
        self.should_trigger_rate_limit: bool = False
        self.published_url_to_return: str | None = "https://example.com/item/123"

        # Tracking calls
        self.save_draft_calls = 0
        self.open_draft_calls = 0
        self.publish_and_confirm_calls = 0
        self.verify_published_calls = 0
        self.reconcile_calls = 0

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def check_login(self) -> bool:
        return self.is_logged_in

    async def capture_qr(self) -> str | None:
        return self.qr_data

    async def clear_auth(self) -> None:
        self.is_logged_in = False

    async def save_draft(
        self,
        job: PublishJob,
        media_paths: list[Path],
    ) -> tuple[str, str | None]:
        self.save_draft_calls += 1
        if self.should_trigger_security_check:
            raise RuntimeError("SECURITY_CHECK_TRIGGERED: Fake security verification required")
        if self.should_trigger_rate_limit:
            raise RuntimeError("RATE_LIMIT_TRIGGERED: Fake rate limit exceeded")
        if self.should_fail_save_draft:
            raise self.should_fail_save_draft

        draft_id = f"draft_{job.id}"
        draft_url = f"https://example.com/drafts/{draft_id}"
        return draft_url, draft_id

    async def open_draft(self, draft_url: str) -> None:
        self.open_draft_calls += 1

    async def publish_and_confirm(self, job: PublishJob) -> None:
        self.publish_and_confirm_calls += 1
        if self.should_trigger_security_check:
            raise RuntimeError("SECURITY_CHECK_TRIGGERED: Fake security verification required")
        if self.should_trigger_rate_limit:
            raise RuntimeError("RATE_LIMIT_TRIGGERED: Fake rate limit exceeded")
        if self.should_fail_publish:
            raise self.should_fail_publish

    async def verify_published(self, job: PublishJob, start_time: datetime) -> str | None:
        self.verify_published_calls += 1
        return self.published_url_to_return

    async def reconcile(self, job: PublishJob, start_time: datetime) -> str | None:
        self.reconcile_calls += 1
        return self.published_url_to_return
