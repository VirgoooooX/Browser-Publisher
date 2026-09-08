"""Security, authentication, and session cookie management."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from publisher.config import PublisherSettings

SESSION_COOKIE_NAME = "publisher_session"
SESSION_MAX_AGE = 86400  # 24 hours

http_bearer = HTTPBearer(auto_error=False)


def get_settings(request: Request) -> PublisherSettings:
    return request.app.state.settings


def _get_signer(settings: PublisherSettings) -> TimestampSigner:
    secret = settings.access_token.get_secret_value()
    return TimestampSigner(secret_key=secret, salt="publisher-console-session")


def create_session_cookie(settings: PublisherSettings) -> str:
    signer = _get_signer(settings)
    # Sign a fixed session marker
    return signer.sign("authenticated").decode("utf-8")


def verify_session_cookie(cookie_value: str, settings: PublisherSettings) -> bool:
    signer = _get_signer(settings)
    try:
        data = signer.unsign(cookie_value, max_age=SESSION_MAX_AGE).decode("utf-8")
        return data == "authenticated"
    except (BadSignature, SignatureExpired):
        return False


def verify_token(provided_token: str, settings: PublisherSettings) -> bool:
    expected = settings.access_token.get_secret_value().encode("utf-8")
    provided = provided_token.encode("utf-8")
    return hmac.compare_digest(expected, provided)


async def require_api_auth(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(http_bearer)],
    settings: Annotated[PublisherSettings, Depends(get_settings)],
) -> None:
    """Validate Bearer token for API endpoints."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Bearer authentication header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not verify_token(credentials.credentials, settings):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid access token",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def require_console_auth(
    request: Request,
    settings: Annotated[PublisherSettings, Depends(get_settings)],
) -> bool:
    """Validate either HttpOnly session cookie or Bearer token for console endpoints."""
    # 1. Check Bearer token first if provided
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if verify_token(token, settings):
            return True

    # 2. Check session cookie
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie and verify_session_cookie(cookie, settings):
        return True

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Console authentication required",
    )
