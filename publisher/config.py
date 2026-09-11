"""Configuration settings for Browser Publisher."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_WECHAT_MP_API_BASE_URL = "https://api.weixin.qq.com"


class PublisherSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PUBLISHER_",
        env_file=".env",
        extra="ignore",
    )

    access_token: SecretStr = Field(
        default=SecretStr("dev-local-insecure-token-1234567890"),
        description="High strength secret token for API and console access",
    )
    data_dir: Path = Field(
        default=Path("./data"),
        description="Root directory for SQLite DB, browser profile, media, and artifacts",
    )
    host: str = Field(default="0.0.0.0", description="API listen host")
    port: int = Field(default=8790, description="API listen port")

    wechat_mp_default_mode: Literal["draft", "publish"] = Field(
        default="publish",
        description="Default publish mode for WeChat MP when mode is not specified",
    )
    wechat_mp_app_id: str | None = Field(
        default=None,
        description="WeChat Official Account AppID used by Browser Publisher",
    )
    wechat_mp_app_secret: SecretStr | None = Field(
        default=None,
        description="WeChat Official Account AppSecret used by Browser Publisher",
    )
    wechat_mp_api_base_url: str = Field(
        default=DEFAULT_WECHAT_MP_API_BASE_URL,
        description="WeChat Official Account API base URL",
    )
    wechat_mp_request_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        le=60,
        description="Timeout for a single WeChat Official Account API request",
    )
    wechat_mp_token_refresh_skew_seconds: int = Field(
        default=120,
        ge=0,
        description="Refresh the WeChat API token this many seconds before expiry",
    )
    wechat_mp_author: str = Field(
        default="Notify Hub",
        max_length=100,
        description="Default author sent to the WeChat draft API",
    )
    xhs_default_mode: Literal["draft", "publish"] = Field(
        default="draft",
        description="Default publish mode for Xiaohongshu when mode is not specified",
    )
    xhs_min_interval_seconds: int = Field(
        default=1800,
        description="Minimum cooldown period in seconds between Xiaohongshu jobs",
    )

    headless: bool = Field(default=True, description="Run browser in headless mode")
    operation_timeout_seconds: int = Field(
        default=45,
        description="Timeout in seconds for single UI actions and element wait",
    )
    navigation_timeout_seconds: int = Field(
        default=30,
        description="Timeout in seconds for page navigation",
    )

    console_public_url: str = Field(
        default="http://192.168.31.100:8790",
        description="Public base URL of the publisher console for alert links",
    )

    notify_event_url: str | None = Field(
        default=None,
        description="Notify Hub external event endpoint for alerts (e.g. http://hub:8000/api/v1/events)",
    )
    notify_api_key: SecretStr | None = Field(
        default=None,
        description="API key for calling Notify Hub external event endpoint",
    )
    notify_recipient_ids: list[str] = Field(
        default_factory=list,
        description="Recipient IDs in Notify Hub to receive publisher alerts",
    )

    @field_validator("notify_recipient_ids", mode="before")
    @classmethod
    def parse_recipient_ids(cls, v: object) -> list[str]:
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            if v.startswith("[") and v.endswith("]"):
                try:
                    parsed = json.loads(v)
                    if isinstance(parsed, list):
                        return [str(x) for x in parsed]
                except Exception:
                    pass
            return [x.strip() for x in v.split(",") if x.strip()]
        if isinstance(v, list):
            return [str(x) for x in v]
        return []

    @field_validator("wechat_mp_api_base_url")
    @classmethod
    def validate_wechat_mp_api_url(cls, value: str) -> str:
        value = value.strip()
        parsed = urlsplit(value)
        if parsed.scheme != "https":
            raise ValueError("WeChat MP API base URL must use HTTPS")
        if not parsed.hostname:
            raise ValueError("WeChat MP API base URL must include a host")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("WeChat MP API base URL must not include credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("WeChat MP API base URL must not include a query or fragment")
        # Keep a trailing slash so httpx preserves a reverse-proxy path prefix
        # when joining relative endpoint paths such as ``cgi-bin/token``.
        return value.rstrip("/") + "/"

    @property
    def wechat_mp_api_configured(self) -> bool:
        """Return whether Browser Publisher can call the official MP API."""

        return bool(
            self.wechat_mp_app_id
            and self.wechat_mp_app_id.strip()
            and self.wechat_mp_app_secret
            and self.wechat_mp_app_secret.get_secret_value()
        )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "publisher.db"

    @property
    def db_url(self) -> str:
        # SQLite URL for aiosqlite
        return f"sqlite+aiosqlite:///{self.db_path.resolve().as_posix()}"

    @property
    def sync_db_url(self) -> str:
        # SQLite URL for synchronous Alembic migrations
        return f"sqlite:///{self.db_path.resolve().as_posix()}"

    @property
    def profile_dir(self) -> Path:
        return self.data_dir / "profile"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    def ensure_directories(self) -> None:
        """Ensure all storage directories exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
