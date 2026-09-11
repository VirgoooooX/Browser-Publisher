"""Base abstract class for platform publisher adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

from publisher.config import PublisherSettings
from publisher.models import PublishJob


class BasePlatformPublisher(ABC):
    """Abstract interface for all browser platform adapters."""

    def __init__(self, settings: PublisherSettings) -> None:
        self.settings = settings

    @abstractmethod
    async def start(self) -> None:
        """Initialize browser resources or context."""

    @abstractmethod
    async def close(self) -> None:
        """Close browser resources."""

    @abstractmethod
    async def check_login(self) -> bool:
        """Return True if session is authenticated, False otherwise."""

    @abstractmethod
    async def capture_qr(self) -> str | None:
        """Capture QR code as base64 PNG string if present."""

    @abstractmethod
    async def clear_auth(self) -> None:
        """Clear session cookies/storage to force re-login."""

    @abstractmethod
    async def save_draft(
        self,
        job: PublishJob,
        media_paths: list[Path],
    ) -> tuple[str | None, str | None]:
        """Fill content, upload media, save draft, and return (draft_url, platform_draft_id)."""

    @property
    def draft_creation_phase(self) -> str:
        """Return the durable phase used while creating a fresh draft."""

        return "editing"

    @abstractmethod
    async def open_draft(
        self, draft_url: str | None, *, job: PublishJob | None = None
    ) -> None:
        """Open an existing saved draft by URL."""

    @abstractmethod
    async def publish_and_confirm(self, job: PublishJob) -> str | None:
        """Click publish and return ``waiting_manual_confirm`` if needed."""

    @abstractmethod
    async def verify_published(
        self, job: PublishJob, start_time: datetime
    ) -> str | None:
        """Verify publication success after publish click and return its URL."""

    @abstractmethod
    async def reconcile(self, job: PublishJob, start_time: datetime) -> str | None:
        """Query published articles/notes list for this job's title and return URL if found."""
