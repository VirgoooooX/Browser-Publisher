"""Durable login incidents shared by the worker and console; HTTP is outside transactions."""

from __future__ import annotations

import asyncio
import base64
import binascii
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from publisher.config import PublisherSettings
from publisher.models import PlatformState
from publisher.notify_alert import emit_notify_hub_alert
from publisher.platforms.base import BasePlatformPublisher
from publisher.security import safe_error_summary

logger = structlog.get_logger()


class SessionService:
    def __init__(
        self,
        settings: PublisherSettings,
        sessions: async_sessionmaker[AsyncSession],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.clock = clock or (lambda: datetime.now(UTC))

    async def record_ready(self, platform: str) -> None:
        now = self.clock()
        async with self.sessions() as session:
            state = await session.get(PlatformState, platform)
            if state is None:
                return
            incident = state.alert_incident_id
            recovered = state.session_state == "auth_required"
            if recovered or state.last_auth_at is None:
                state.last_auth_at = now
            state.session_state = "ready"
            state.qr_code_base64 = None
            state.alert_incident_id = None
            state.auth_alert_sent_at = None
            state.last_error_code = None
            state.last_error_message = None
            state.last_session_check_at = now
            state.updated_at = now
            await session.commit()
        if recovered and incident:
            await emit_notify_hub_alert(
                self.settings,
                event_type="publisher.session.recovered",
                event_key=f"auth-recovered-{platform}-{incident}",
                title=f"【{platform}】发布器登录态已恢复",
                content="扫码登录已成功，等待登录的任务将从已保存的草稿继续执行。",
                level="info",
                payload={"platform": platform},
            )

    async def record_auth_required(
        self, platform: str, publisher: BasePlatformPublisher
    ) -> None:
        async with self.sessions() as session:
            state = await session.get(PlatformState, platform)
            if state is None:
                return
            incident = state.alert_incident_id or uuid.uuid4().hex[:12]
            qr = state.qr_code_base64
            sent = state.auth_alert_sent_at if state.alert_incident_id else None
        if not qr:
            qr = await publisher.capture_qr()
        now = self.clock()
        async with self.sessions() as session:
            await session.execute(
                update(PlatformState)
                .where(PlatformState.platform == platform)
                .values(
                    session_state="auth_required",
                    qr_code_base64=qr,
                    alert_incident_id=incident,
                    auth_alert_sent_at=sent,
                    last_error_code="AUTH_REQUIRED",
                    last_error_message="网页登录已失效，请扫码登录；已保存草稿会在登录恢复后续发。",
                    last_session_check_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
        if sent is not None:
            return
        image_path: Path | None = None
        if qr:
            try:
                raw = base64.b64decode(qr.split(",", 1)[-1], validate=True)
                if raw:
                    self.settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
                    image_path = (
                        self.settings.artifacts_dir / f"{platform}_login_{incident}.png"
                    )
                    await asyncio.to_thread(image_path.write_bytes, raw)
            except (binascii.Error, OSError, ValueError) as exc:
                image_path = None
                logger.warning(
                    "login_qr_artifact_write_failed", error=safe_error_summary(exc)
                )
        accepted = await emit_notify_hub_alert(
            self.settings,
            event_type="publisher.session.auth_required",
            event_key=f"auth-required-{platform}-{incident}",
            title=f"【{platform}】发布器需要扫码登录",
            content=(
                "发布器网页登录已失效，待发表任务会保留草稿并等待登录。"
                f"请打开控制台扫码，二维码过期时可在控制台刷新：{self.settings.console_public_url}"
            ),
            level="warning",
            payload={"platform": platform, "incident_id": incident},
            image_path=image_path,
        )
        if accepted:
            async with self.sessions() as session:
                await session.execute(
                    update(PlatformState)
                    .where(
                        PlatformState.platform == platform,
                        PlatformState.alert_incident_id == incident,
                        PlatformState.session_state == "auth_required",
                    )
                    .values(auth_alert_sent_at=self.clock())
                )
                await session.commit()
