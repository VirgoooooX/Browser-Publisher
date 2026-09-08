"""Pydantic request and response schemas for Browser Publisher."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PlatformType = Literal["wechat_mp", "xiaohongshu"]
PublishMode = Literal["draft", "publish"]
JobStatus = Literal[
    "queued",
    "running",
    "waiting_auth",
    "draft_saved",
    "published",
    "failed",
    "publish_unknown",
    "cancelled",
]
PublishPhase = Literal[
    "editing",
    "draft_saved",
    "publish_intent",
    "publish_clicked",
    "reconciling",
]


class MediaItem(BaseModel):
    kind: Literal["url", "uploaded"]
    url: str | None = Field(default=None, max_length=2048)
    media_id: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_media_target(self) -> MediaItem:
        if self.kind == "url" and not self.url:
            raise ValueError("media kind 'url' requires a valid 'url'")
        if self.kind == "uploaded" and not self.media_id:
            raise ValueError("media kind 'uploaded' requires a valid 'media_id'")
        return self


class MediaUploadResponse(BaseModel):
    media_id: str
    file_name: str
    content_type: str
    size_bytes: int


class JobContent(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body_text: str = Field(default="", max_length=100000)
    body_html: str | None = Field(default=None, max_length=500000)
    author: str | None = Field(default=None, max_length=100)
    digest: str | None = Field(default=None, max_length=500)


class JobCreateRequest(BaseModel):
    client_request_id: str = Field(min_length=1, max_length=200)
    platform: PlatformType
    mode: PublishMode | None = None
    content: JobContent
    media: list[MediaItem] = Field(default_factory=list)
    source_url: str | None = Field(default=None, max_length=2048)
    topics: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_platform_rules(self) -> JobCreateRequest:
        title = self.content.title.strip()
        body_text = self.content.body_text.strip()

        if self.platform == "xiaohongshu":
            # XHS note title maximum 20 characters
            if len(title) > 20:
                raise ValueError(
                    f"Xiaohongshu note title cannot exceed 20 characters (got {len(title)}: '{title}')"
                )
            if not body_text:
                raise ValueError("Xiaohongshu publishing requires non-empty 'body_text'")
            if len(body_text) > 1000:
                raise ValueError(
                    f"Xiaohongshu note body cannot exceed 1000 characters (got {len(body_text)})"
                )
            # 1 to 18 images required for Xiaohongshu
            if not (1 <= len(self.media) <= 18):
                raise ValueError(
                    f"Xiaohongshu image-text note requires between 1 and 18 media images (got {len(self.media)})"
                )

        elif self.platform == "wechat_mp":
            if len(title) > 64:
                raise ValueError(
                    f"WeChat Official Account title cannot exceed 64 characters (got {len(title)}: '{title}')"
                )
            if not body_text and not (self.content.body_html and self.content.body_html.strip()):
                raise ValueError("WeChat Official Account requires either body_text or body_html")

        return self


class JobCreateResponse(BaseModel):
    id: str
    status: JobStatus
    effective_mode: PublishMode


class JobDetailResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    client_request_id: str
    platform: str
    mode: str
    status: str
    publish_phase: str | None = None
    content: dict[str, Any]
    media: list[dict[str, Any]]
    source_url: str | None = None
    topics: list[str]
    attempt_count: int
    max_attempts: int
    platform_draft_id: str | None = None
    publish_id: str | None = None
    final_url: str | None = None
    error_code: str | None = None
    error_summary: str | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class JobListResponse(BaseModel):
    items: list[JobDetailResponse]
    total: int
    page: int
    page_size: int


class PlatformDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    platform: str
    session_state: str
    is_paused: bool
    paused_reason: str | None = None
    paused_at: datetime | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    current_job_id: str | None = None
    last_auth_at: datetime | None = None
    last_publish_at: datetime | None = None
    updated_at: datetime
    has_qr_code: bool = False


class PlatformListResponse(BaseModel):
    platforms: list[PlatformDetail]


class ConsoleLoginRequest(BaseModel):
    access_token: str
