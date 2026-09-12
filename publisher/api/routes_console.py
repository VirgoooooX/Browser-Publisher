"""Server-rendered web console endpoints for operators."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from publisher.config import PublisherSettings
from publisher.models import PlatformState, PublishJob
from publisher.security import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE,
    create_session_cookie,
    get_settings,
    verify_session_cookie,
    verify_token,
)

router = APIRouter(include_in_schema=False)

templates_dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))


def get_db(request: Request) -> object:
    return request.app.state.session_factory


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request, name="login.html", context={"error": None}
    )


@router.post("/console/login")
async def console_login(
    request: Request,
    token: Annotated[str, Form()],
    settings: Annotated[PublisherSettings, Depends(get_settings)],
) -> Response:
    if not verify_token(token.strip(), settings):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "无效的 Access Token"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    cookie_val = create_session_cookie(settings)
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=cookie_val,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=False,  # Allow LAN HTTP
    )
    return response


@router.post("/console/logout")
async def console_logout() -> Response:
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@router.get("/", response_class=HTMLResponse)
async def console_dashboard(
    request: Request,
    session_factory: Annotated[object, Depends(get_db)],
    settings: Annotated[PublisherSettings, Depends(get_settings)],
) -> HTMLResponse:
    # Check session cookie
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie or not verify_session_cookie(cookie, settings):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    async with session_factory() as session:
        # 1. Fetch platform states
        p_stmt = select(PlatformState).order_by(PlatformState.platform.asc())
        platforms_db = (await session.execute(p_stmt)).scalars().all()
        platforms_list = []
        for p in platforms_db:
            has_qr = bool(p.qr_code_base64 and len(p.qr_code_base64) > 10)
            platforms_list.append(
                {
                    "platform": p.platform,
                    "session_state": p.session_state,
                    "is_paused": p.is_paused,
                    "paused_reason": p.paused_reason,
                    "current_job_id": p.current_job_id,
                    "last_publish_at": p.last_publish_at,
                    "last_auth_at": p.last_auth_at,
                    "has_qr_code": has_qr,
                }
            )

        # 2. Fetch recent jobs
        j_stmt = select(PublishJob).order_by(PublishJob.created_at.desc()).limit(50)
        jobs_db = (await session.execute(j_stmt)).scalars().all()

        # 3. Calculate job statistics
        counts_stmt = select(PublishJob.status, func.count(PublishJob.id)).group_by(
            PublishJob.status
        )
        status_counts = dict((await session.execute(counts_stmt)).all())

        stats = {
            "total": sum(status_counts.values()),
            "succeeded": status_counts.get("published", 0)
            + status_counts.get("draft_saved", 0),
            "running": (
                status_counts.get("running", 0)
                + status_counts.get("queued", 0)
                + status_counts.get("waiting_auth", 0)
                + status_counts.get("waiting_manual_confirm", 0)
            ),
            "failed": status_counts.get("failed", 0)
            + status_counts.get("publish_unknown", 0),
        }

        config_info = {
            "version": "0.2.0",
            "data_dir": str(settings.data_dir),
            "port": settings.port,
            "headless": settings.headless,
            "wechat_mp_app_id_configured": bool(settings.wechat_mp_app_id),
            "wechat_mp_secret_configured": settings.wechat_mp_app_secret is not None,
            "wechat_mp_api_base_url": settings.wechat_mp_api_base_url,
            "wechat_mp_default_mode": settings.wechat_mp_default_mode,
            "wechat_mp_author": settings.wechat_mp_author,
            "xhs_default_mode": settings.xhs_default_mode,
            "xhs_min_interval_seconds": settings.xhs_min_interval_seconds,
            "notify_alert_configured": bool(
                settings.notify_event_url and settings.notify_api_key
            ),
        }

        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "platforms": platforms_list,
                "jobs": jobs_db,
                "stats": stats,
                "config": config_info,
            },
        )
