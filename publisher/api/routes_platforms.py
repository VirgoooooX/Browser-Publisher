"""Platform session management, QR authentication, and risk pause resumption."""

from __future__ import annotations

import base64
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from publisher.models import PlatformState, utc_now
from publisher.schemas import PlatformDetail, PlatformListResponse, PlatformType
from publisher.security import require_api_auth

router = APIRouter(prefix="/v1/platforms", tags=["platforms"])


def get_db(request: Request) -> object:
    return request.app.state.session_factory


def get_publishers(request: Request) -> dict[str, object]:
    return request.app.state.publishers


@router.get(
    "",
    response_model=PlatformListResponse,
    dependencies=[Depends(require_api_auth)],
)
async def list_platforms(
    session_factory: Annotated[object, Depends(get_db)],
) -> PlatformListResponse:
    async with session_factory() as session:
        stmt = select(PlatformState).order_by(PlatformState.platform.asc())
        items = (await session.execute(stmt)).scalars().all()
        details: list[PlatformDetail] = []
        for p in items:
            has_qr = bool(p.qr_code_base64 and len(p.qr_code_base64) > 10)
            d = PlatformDetail.model_validate(p)
            d.has_qr_code = has_qr
            details.append(d)
        return PlatformListResponse(platforms=details)


@router.post(
    "/{platform}/login",
    dependencies=[Depends(require_api_auth)],
)
async def request_platform_login(
    platform: PlatformType,
    session_factory: Annotated[object, Depends(get_db)],
    publishers: Annotated[dict[str, object], Depends(get_publishers)],
) -> dict[str, Any]:
    pub = publishers.get(platform)
    if not pub:
        raise HTTPException(status_code=404, detail=f"Platform '{platform}' not configured")

    is_logged_in = await pub.check_login()
    async with session_factory() as session:
        if is_logged_in:
            await session.execute(
                update(PlatformState)
                .where(PlatformState.platform == platform)
                .values(
                    session_state="ready",
                    qr_code_base64=None,
                    alert_incident_id=None,
                    updated_at=utc_now(),
                )
            )
            await session.commit()
            return {"platform": platform, "status": "already_authenticated"}

        qr_b64 = await pub.capture_qr()
        await session.execute(
            update(PlatformState)
            .where(PlatformState.platform == platform)
            .values(
                session_state="auth_required",
                qr_code_base64=qr_b64,
                updated_at=utc_now(),
            )
        )
        await session.commit()
        return {"platform": platform, "status": "auth_required", "has_qr": bool(qr_b64)}


@router.get(
    "/{platform}/qr.png",
    responses={200: {"content": {"image/png": {}}}},
)
async def get_platform_qr(
    platform: PlatformType,
    session_factory: Annotated[object, Depends(get_db)],
) -> Response:
    async with session_factory() as session:
        stmt = select(PlatformState).where(PlatformState.platform == platform)
        p_state = (await session.execute(stmt)).scalar_one_or_none()

        if not p_state or not p_state.qr_code_base64:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No active QR code for platform '{platform}'",
            )

        raw_b64 = p_state.qr_code_base64
        if "," in raw_b64:
            raw_b64 = raw_b64.split(",", 1)[1]

        try:
            image_bytes = base64.b64decode(raw_b64)
        except Exception:
            raise HTTPException(status_code=500, detail="Corrupted QR image data")

        return Response(
            content=image_bytes,
            media_type="image/png",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )


@router.post(
    "/{platform}/reauth",
    dependencies=[Depends(require_api_auth)],
)
async def reauth_platform(
    platform: PlatformType,
    session_factory: Annotated[object, Depends(get_db)],
    publishers: Annotated[dict[str, object], Depends(get_publishers)],
) -> dict[str, str]:
    pub = publishers.get(platform)
    if not pub:
        raise HTTPException(status_code=404, detail=f"Platform '{platform}' not configured")

    await pub.clear_auth()
    qr_b64 = await pub.capture_qr()

    async with session_factory() as session:
        await session.execute(
            update(PlatformState)
            .where(PlatformState.platform == platform)
            .values(
                session_state="auth_required",
                qr_code_base64=qr_b64,
                updated_at=utc_now(),
            )
        )
        await session.commit()

    return {"platform": platform, "status": "auth_cleared_reauth_initiated"}


@router.post(
    "/{platform}/resume",
    dependencies=[Depends(require_api_auth)],
)
async def resume_platform(
    platform: PlatformType,
    session_factory: Annotated[object, Depends(get_db)],
) -> dict[str, str]:
    """Manually clear the risk pause flag and resume execution for the platform."""
    async with session_factory() as session:
        await session.execute(
            update(PlatformState)
            .where(PlatformState.platform == platform)
            .values(
                is_paused=False,
                paused_reason=None,
                paused_at=None,
                last_error_code=None,
                last_error_message=None,
                updated_at=utc_now(),
            )
        )
        await session.commit()

    return {"platform": platform, "status": "resumed"}
