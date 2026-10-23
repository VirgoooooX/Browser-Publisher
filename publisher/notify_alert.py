"""Optional alert reporter forwarding incidents to Notify Hub external event API."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import structlog

from publisher.config import PublisherSettings

logger = structlog.get_logger()
_COVERS = Path(__file__).resolve().parent / "static" / "notifications"


def notification_copy(
    event_type: str, title: str, content: str, payload: dict[str, Any], subject: str | None
) -> tuple[str, str, str | None]:
    platform = {"wechat_mp": "公众号", "xiaohongshu": "小红书"}.get(
        payload.get("platform"), "公众号" if event_type.startswith("publisher.wechat.") else "发布任务"
    )
    target = f"《{' '.join(subject.split())[:35]}》" if subject and subject.strip() else platform
    messages = {
        "publisher.session.auth_required": ("请扫码恢复登录", "待发布任务已保留。请扫描二维码；过期后进入控制台刷新。", "login"),
        "publisher.session.recovered": ("登录已恢复", "等待登录的任务将继续执行，无需操作。", "login"),
        "publisher.job.failed": ("发布失败", "请在发布器控制台查看失败原因，处理后再决定是否重试。", "publish-error"),
        "publisher.job.publish_unknown": ("发布结果待核对", "系统未重复发表。请先到平台核对结果，避免重复发布。", "publish-error"),
        "publisher.job.draft_unknown": ("草稿结果待核对", "系统未重复创建草稿。请先到公众号草稿箱核对。", "publish-error"),
        "publisher.platform.paused": ("发布已暂停", "检测到平台风控。请在控制台处理，确认后恢复发布。", "publish-error"),
        "publisher.wechat.manual_confirm": ("等待微信验证", "请在微信提示或公众号后台发表页面完成管理员验证，系统会继续核对结果。", "publish-confirm"),
    }
    if event_type not in messages:
        return title, content, None
    label, summary, category = messages[event_type]
    if payload.get("error_code") == "PUBLISH_NOT_STARTED":
        label, summary = "发表未开始", "尚未点击发表。请在发布器控制台查看原因并处理。"
    elif event_type == "publisher.job.failed":
        reason = {
            "EDITOR_TIMEOUT": "公众号后台响应超时。",
            "DRAFT_OPEN_FAILED": "未能打开已保存的草稿。",
        }.get(payload.get("error_code"), "")
        summary = reason + summary
    return f"【发布器】{label}｜{target}"[:40], summary, category


async def emit_notify_hub_alert(
    settings: PublisherSettings,
    *,
    event_type: str,
    event_key: str,
    title: str = "",
    content: str = "",
    level: str = "warning",
    payload: dict[str, Any] | None = None,
    image_path: Path | None = None,
    subject: str | None = None,
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
    title, content, category = notification_copy(event_type, title, content, payload or {}, subject)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            media_asset_id: str | None = None
            is_qr = image_path is not None
            upload_path = image_path if is_qr else (_COVERS / f"{category}.png" if category else None)
            if upload_path and await asyncio.to_thread(upload_path.is_file):
                media_url = url
                if media_url.endswith("/events"):
                    media_url = media_url[:-7] + "/media"
                elif media_url.endswith("/events/"):
                    media_url = media_url[:-8] + "/media"
                else:
                    media_url = f"{media_url.rstrip('/')}/media"

                try:
                    image_bytes = await asyncio.to_thread(upload_path.read_bytes)
                    upload_resp = await client.post(
                        media_url,
                        headers={"X-API-Key": api_key},
                        data={"kind": "image"},
                        files={"file": (upload_path.name, image_bytes, "image/png")},
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
                        )
                except Exception as exc:
                    logger.warning("notify_hub_media_upload_exception", error_type=type(exc).__name__)

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
                data["message_type"] = "image" if is_qr else "article"
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
                )
    except Exception as exc:
        logger.warning("notify_hub_alert_exception", error_type=type(exc).__name__)
    return False
