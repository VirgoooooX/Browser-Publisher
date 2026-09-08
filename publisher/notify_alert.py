"""Optional alert reporter forwarding incidents to Notify Hub external event API."""

from __future__ import annotations

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
) -> bool:
    """Send an alert event to Notify Hub external event API if configured."""
    if not settings.notify_event_url or not settings.notify_api_key:
        return False

    url = str(settings.notify_event_url).rstrip("/")
    api_key = settings.notify_api_key.get_secret_value()

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

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                json=data,
                headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            )
            if resp.status_code in (200, 202):
                logger.info("notify_hub_alert_emitted", event_type=event_type, event_key=event_key)
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
