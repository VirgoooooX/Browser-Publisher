"""Health check endpoints for container and orchestrators."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import text


router = APIRouter(prefix="/health", tags=["health"])


def get_db(request: Request) -> object:
    return request.app.state.session_factory


@router.get("/live", status_code=status.HTTP_200_OK)
async def liveness_check() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready", status_code=status.HTTP_200_OK)
async def readiness_check(
    request: Request,
    session_factory: Annotated[object, Depends(get_db)],
) -> dict[str, str]:
    try:
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database readiness check failed: {exc}",
        )

    browser_mgr = getattr(request.app.state, "browser_manager", None)
    if browser_mgr is not None and hasattr(browser_mgr, "check_health"):
        if not await browser_mgr.check_health():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Browser context is unhealthy or disconnected",
            )

    return {"status": "ok", "database": "connected"}
