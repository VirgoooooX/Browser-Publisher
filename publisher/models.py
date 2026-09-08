"""SQLAlchemy ORM models for Browser Publisher."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from publisher.database import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


def generate_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class PublishJob(Base):
    __tablename__ = "publish_jobs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: generate_id("job")
    )
    client_request_id: Mapped[str] = mapped_column(
        String(200), unique=True, index=True, nullable=False
    )
    platform: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)  # draft | publish
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="queued",
        index=True,
    )  # queued, running, waiting_auth, draft_saved, published, failed, publish_unknown, cancelled
    publish_phase: Mapped[str | None] = mapped_column(
        String(30),
        nullable=True,
    )  # editing, draft_saved, publish_intent, publish_clicked, reconciling

    content: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    media: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    topics: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2, nullable=False)

    platform_draft_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    publish_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    final_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class MediaAsset(Base):
    __tablename__ = "media_assets"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: generate_id("med")
    )
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )  # SHA-256

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PlatformState(Base):
    __tablename__ = "platform_states"

    platform: Mapped[str] = mapped_column(
        String(50), primary_key=True
    )  # wechat_mp | xiaohongshu
    session_state: Mapped[str] = mapped_column(
        String(30),
        default="idle",
        nullable=False,
    )  # idle, starting, ready, auth_required, error

    current_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    paused_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    alert_incident_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    qr_code_base64: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_auth_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_publish_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
