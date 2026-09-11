"""Official WeChat Official Account API client used by Browser Publisher."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import httpx
import structlog

from publisher.config import PublisherSettings
from publisher.models import PublishJob
from publisher.platforms.render import render_wechat_html

logger = structlog.get_logger()

TOKEN_INVALID_CODES = {40001, 42001}
RETRYABLE_CODES = {-1, 45009}


class WeChatApiError(RuntimeError):
    """Stable error returned by the official WeChat API."""

    def __init__(
        self,
        code: int,
        message: str,
        *,
        retryable: bool = False,
        result_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.result_unknown = result_unknown


@dataclass
class _TokenCache:
    value: str | None = None
    expires_at: datetime | None = None


class WeChatApiClient:
    """Small async client for token, permanent image, and draft APIs."""

    def __init__(
        self,
        settings: PublisherSettings,
        *,
        http_client: httpx.AsyncClient | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._now = now or (lambda: datetime.now(UTC))
        self._http = http_client or httpx.AsyncClient(
            base_url=settings.wechat_mp_api_base_url,
            timeout=httpx.Timeout(settings.wechat_mp_request_timeout_seconds),
            follow_redirects=False,
        )
        self._owns_client = http_client is None
        self._cache = _TokenCache()
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    def _cache_valid(self) -> bool:
        if self._cache.value is None or self._cache.expires_at is None:
            return False
        return self._cache.expires_at > self._now() + timedelta(
            seconds=self._settings.wechat_mp_token_refresh_skew_seconds
        )

    async def get_access_token(self, *, force: bool = False) -> str:
        if not force and self._cache_valid():
            return str(self._cache.value)

        async with self._lock:
            if not force and self._cache_valid():
                return str(self._cache.value)
            if not self._settings.wechat_mp_api_configured:
                raise WeChatApiError(0, "WeChat MP API credentials are not configured")

            try:
                response = await self._http.get(
                    "cgi-bin/token",
                    params={
                        "grant_type": "client_credential",
                        "appid": self._settings.wechat_mp_app_id,
                        "secret": self._settings.wechat_mp_app_secret.get_secret_value(),
                    },
                )
                response.raise_for_status()
                data = response.json()
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise WeChatApiError(
                    -1,
                    "WeChat token request failed",
                    retryable=True,
                ) from exc
            except httpx.HTTPStatusError as exc:
                retryable = exc.response.status_code >= 500 or exc.response.status_code == 429
                raise WeChatApiError(
                    0,
                    f"WeChat token HTTP error {exc.response.status_code}",
                    retryable=retryable,
                ) from exc
            except (TypeError, ValueError) as exc:
                raise WeChatApiError(0, "WeChat token response was invalid") from exc

            self._raise_for_api_error(data, operation="token")
            access_token = data.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                raise WeChatApiError(0, "WeChat token response did not contain access_token")
            self._cache = _TokenCache(
                value=access_token,
                expires_at=self._now()
                + timedelta(seconds=int(data.get("expires_in", 7200))),
            )
            return access_token

    async def upload_permanent_image(
        self, *, filename: str, content_type: str, content: bytes
    ) -> str:
        data, _ = await self._request_json(
            "POST",
            "cgi-bin/material/add_material",
            params={"type": "image"},
            files={"media": (filename, content, content_type)},
        )
        media_id = data.get("media_id")
        if not isinstance(media_id, str) or not media_id:
            raise WeChatApiError(0, "WeChat API did not return an image media id")
        return media_id

    async def add_draft(self, articles: list[dict[str, Any]]) -> str:
        try:
            data, _ = await self._request_json(
                "POST", "cgi-bin/draft/add", json={"articles": articles}
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            # The request may have reached WeChat even when the response was
            # lost. Retrying draft/add blindly could create a duplicate draft.
            raise WeChatApiError(
                -1,
                "DRAFT_API_RESULT_UNKNOWN: WeChat draft creation result is unknown; refusing to retry blindly",
                retryable=False,
                result_unknown=True,
            ) from exc
        except WeChatApiError as exc:
            raise WeChatApiError(
                exc.code,
                f"DRAFT_API_REJECTED: {exc}",
                retryable=exc.retryable,
            ) from exc
        media_id = data.get("media_id")
        if not isinstance(media_id, str) or not media_id:
            raise WeChatApiError(0, "WeChat API did not return a draft media id")
        return media_id

    async def create_draft(
        self,
        job: PublishJob,
        cover_path: Path,
    ) -> str:
        """Upload the cover and create the complete official API draft."""

        if not await asyncio.to_thread(cover_path.is_file):
            raise RuntimeError("COVER_FAILED: WeChat API cover file is unavailable")
        content = job.content or {}
        body_text = str(content.get("body_text") or "")
        body_html = content.get("body_html")
        if not isinstance(body_html, str) or not body_html.strip():
            body_html = render_wechat_html(
                content=body_text,
                source_url=job.source_url,
                strip_title_heading=True,
            )

        image_bytes = await asyncio.to_thread(cover_path.read_bytes)
        if not image_bytes:
            raise RuntimeError("COVER_FAILED: WeChat API cover file is empty")
        content_type = _content_type_for_path(cover_path)
        thumb_media_id = await self.upload_permanent_image(
            filename=cover_path.name,
            content_type=content_type,
            content=image_bytes,
        )

        title = str(content.get("title") or "").strip()
        author = str(content.get("author") or self._settings.wechat_mp_author).strip()
        digest = str(content.get("digest") or "").strip()
        if not digest:
            digest = " ".join(body_text.split())[:120]
        article: dict[str, Any] = {
            "title": title,
            "author": author,
            "digest": digest[:120],
            "content": body_html,
            "thumb_media_id": thumb_media_id,
            "need_open_comment": 1,
            "only_fans_can_comment": 0,
        }
        if job.source_url:
            article["content_source_url"] = job.source_url
        return await self.add_draft([article])

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> tuple[dict[str, Any], httpx.Response]:
        token = await self.get_access_token()
        request_params = {**(params or {}), "access_token": token}
        response = await self._request_http(
            method, path, params=request_params, json=json, files=files
        )
        data = self._response_json(response, path)
        if int(data.get("errcode", 0)) in TOKEN_INVALID_CODES:
            token = await self.get_access_token(force=True)
            request_params = {**(params or {}), "access_token": token}
            response = await self._request_http(
                method, path, params=request_params, json=json, files=files
            )
            data = self._response_json(response, path)
        self._raise_for_api_error(data, operation=path)
        return data, response

    async def _request_http(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any],
        json: dict[str, Any] | None,
        files: dict[str, tuple[str, bytes, str]] | None,
    ) -> httpx.Response:
        try:
            response = await self._http.request(
                method, path, params=params, json=json, files=files
            )
            response.raise_for_status()
            return response
        except (httpx.TimeoutException, httpx.NetworkError):
            raise
        except httpx.HTTPStatusError as exc:
            retryable = exc.response.status_code >= 500 or exc.response.status_code == 429
            raise WeChatApiError(
                0,
                f"WeChat API HTTP error {exc.response.status_code}",
                retryable=retryable,
            ) from exc

    @staticmethod
    def _response_json(response: httpx.Response, path: str) -> dict[str, Any]:
        try:
            data = response.json()
        except (TypeError, ValueError) as exc:
            raise WeChatApiError(0, f"WeChat {path} response was invalid") from exc
        if not isinstance(data, dict):
            raise WeChatApiError(0, f"WeChat {path} response was not an object")
        return data

    @staticmethod
    def _raise_for_api_error(data: Any, *, operation: str) -> None:
        if not isinstance(data, dict):
            raise WeChatApiError(0, f"WeChat {operation} response was not an object")
        try:
            code = int(data.get("errcode", 0))
        except (TypeError, ValueError) as exc:
            raise WeChatApiError(0, f"WeChat {operation} response had an invalid errcode") from exc
        if code == 0:
            return
        message = str(data.get("errmsg") or "WeChat API rejected the request")
        raise WeChatApiError(code, message, retryable=code in RETRYABLE_CODES)


def _content_type_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".gif": "image/gif",
        ".jpeg": "image/jpeg",
        ".jpg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(suffix, "image/jpeg")
