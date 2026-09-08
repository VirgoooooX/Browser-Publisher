"""Artifacts and media assets retention and cleanup."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from publisher.models import MediaAsset, PublishJob

logger = structlog.get_logger()
MAX_SCREENSHOTS = 20


def clean_screenshots(artifacts_dir: Path, max_saved: int = MAX_SCREENSHOTS) -> None:
    """Keep only the most recent max_saved screenshots to prevent disk exhaustion."""
    try:
        if not artifacts_dir.exists():
            return
        png_files = sorted(
            [p for p in artifacts_dir.iterdir() if p.is_file() and p.suffix.lower() == ".png"],
            key=lambda p: p.stat().st_mtime,
        )
        if len(png_files) > max_saved:
            to_delete = png_files[:-max_saved]
            for f in to_delete:
                try:
                    f.unlink()
                except OSError as exc:
                    logger.debug("delete_old_screenshot_failed", path=str(f), error=str(exc))
    except Exception as exc:
        logger.debug("clean_screenshots_failed", error=str(exc))


async def clean_expired_media(
    session: AsyncSession,
    media_dir: Path,
    days: int = 7,
) -> None:
    """Clean up media files of finished jobs older than specified days."""
    try:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        # Find terminal jobs finished before cutoff
        stmt = (
            select(PublishJob)
            .where(PublishJob.status.in_(["published", "failed", "cancelled", "draft_saved"]))
            .where(PublishJob.finished_at < cutoff)
        )
        res = await session.execute(stmt)
        old_jobs = res.scalars().all()

        media_ids_to_check: set[str] = set()
        for job in old_jobs:
            for item in job.media:
                if item.get("kind") == "uploaded" and item.get("media_id"):
                    media_ids_to_check.add(str(item["media_id"]))

        if not media_ids_to_check:
            return

        # Check if these media IDs are used by any active or newer jobs
        active_stmt = select(PublishJob).where(
            PublishJob.status.in_(["queued", "running", "waiting_auth"])
            | (PublishJob.finished_at >= cutoff)
        )
        active_res = await session.execute(active_stmt)
        active_jobs = active_res.scalars().all()
        active_media_ids: set[str] = set()
        for aj in active_jobs:
            for item in aj.media:
                if item.get("kind") == "uploaded" and item.get("media_id"):
                    active_media_ids.add(str(item["media_id"]))

        deletable_ids = media_ids_to_check - active_media_ids
        for m_id in deletable_ids:
            m_res = await session.execute(select(MediaAsset).where(MediaAsset.id == m_id))
            asset = m_res.scalar_one_or_none()
            if asset:
                p = Path(asset.file_path)
                if p.is_file():
                    try:
                        p.unlink()
                    except OSError:
                        pass
                await session.delete(asset)
        await session.commit()
    except Exception as exc:
        logger.debug("clean_expired_media_failed", error=str(exc))
