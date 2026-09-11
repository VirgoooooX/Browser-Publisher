"""Optional alert reporter forwarding incidents to Notify Hub external event API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import structlog

from publisher.config import PublisherSettings

logger = structlog.get_logger()


async def emit_notify_hub_alert(
    settings: PublisherSettings,
    *,
    event_type: str,
    event_key: str,
    title: str,
    content: str,
    level: str = "warning",
    payload: dict[str, Any] | None = None,
    image_path: Path | None = None,
) -> bool:
    """Send an alert event to Notify Hub external event API if configured."""
    if not settings.notify_event_url or not settings.notify_api_key:
        logger.warning(
            "notify_hub_alert_skipped_not_configured",
            event_url_configured=bool(settings.notify_event_url),
            api_key_configured=bool(settings.notify_api_key),
            event_type=event_type,
        )
        return False

    url = str(settings.notify_event_url).rstrip("/")
    api_key = settings.notify_api_key.get_secret_value()

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            media_asset_id: str | None = None
            if image_path and Path(image_path).is_file():
                media_url = url
                if media_url.endswith("/events"):
                    media_url = media_url[:-7] + "/media"
                elif media_url.endswith("/events/"):
                    media_url = media_url[:-8] + "/media"
                else:
                    media_url = f"{media_url.rstrip('/')}/media"

                try:
                    with open(image_path, "rb") as f:
                        upload_resp = await client.post(
                            media_url,
                            headers={"X-API-Key": api_key},
                            data={"kind": "image"},
                            files={"file": (Path(image_path).name, f, "image/png")},
                        )
                    if upload_resp.status_code == 201:
                        media_asset_id = upload_resp.json().get("id")
                        logger.info(
                            "notify_hub_media_uploaded",
                            media_asset_id=media_asset_id,
                        )
                    else:
                        logger.warning(
                            "notify_hub_media_upload_failed",
                            status_code=upload_resp.status_code,
                            body=upload_resp.text[:200],
                        )
                except Exception as exc:
                    logger.warning("notify_hub_media_upload_exception", error=str(exc))

            data: dict[str, Any] = {
                "event_type": event_type,
                "event_key": event_key,
                "title": title,
                "content": content,
                "level": level,
                "url": settings.console_public_url,
                "recipients": settings.notify_recipient_ids or None,
                "payload": payload or {},
            }
            if media_asset_id:
                data["message_type"] = "image"
                data["media_asset_id"] = media_asset_id

            resp = await client.post(
                url,
                json=data,
                headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            )
            if resp.status_code in (200, 202):
                logger.info(
                    "notify_hub_alert_emitted",
                    event_type=event_type,
                    event_key=event_key,
                )
                return True
            else:
                logger.warning(
                    "notify_hub_alert_failed",
                    status_code=resp.status_code,
                    body=resp.text[:200],
                )
    except Exception as exc:
        logger.warning("notify_hub_alert_exception", error=str(exc))
    return False
