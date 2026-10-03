"""FastAPI application factory, lifespan management, and router registration."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

import structlog
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from publisher.api.health import router as health_router
from publisher.api.routes_console import router as console_router
from publisher.api.routes_jobs import router as jobs_router
from publisher.api.routes_media import router as media_router
from publisher.api.routes_platforms import router as platforms_router
from publisher.config import PublisherSettings
from publisher.database import Base, create_engine_and_sessionmaker
from publisher.migrations import upgrade_database
from publisher.models import PlatformState, utc_now
from publisher.platforms.browser import BrowserManager
from publisher.platforms.wechat import WeChatPublisher
from publisher.platforms.xiaohongshu import XiaohongshuPublisher
from publisher.worker.serial_worker import SerialWorker

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings: PublisherSettings = app.state.settings
    settings.ensure_directories()

    await asyncio.to_thread(upgrade_database, settings)

    # 1. Database engine & sessionmaker
    engine, session_factory = create_engine_and_sessionmaker(settings)
    app.state.engine = engine
    app.state.session_factory = session_factory

    # Ensure tables exist and seed platforms if needed
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        for p_name in ["wechat_mp", "xiaohongshu"]:
            stmt = select(PlatformState).where(PlatformState.platform == p_name)
            p_obj = (await session.execute(stmt)).scalar_one_or_none()
            if not p_obj:
                session.add(
                    PlatformState(
                        platform=p_name,
                        session_state="ready",  # default ready in dev/fake mode
                        is_paused=False,
                        updated_at=utc_now(),
                    )
                )
        await session.commit()

    # 2. Shared persistent browser context manager
    browser_manager = BrowserManager(settings)
    app.state.browser_manager = browser_manager

    # 3. Platform publishers registry
    if not hasattr(app.state, "publishers") or not app.state.publishers:
        app.state.publishers = {
            "wechat_mp": WeChatPublisher(settings, browser_manager=browser_manager),
            "xiaohongshu": XiaohongshuPublisher(
                settings, browser_manager=browser_manager
            ),
        }

    # 4. Serial Worker
    worker = SerialWorker(
        settings=settings,
        session_factory=session_factory,
        publishers=app.state.publishers,
    )
    app.state.worker = worker
    await worker.start()

    logger.info("publisher_application_started", host=settings.host, port=settings.port)

    try:
        yield
    finally:
        logger.info("publisher_application_shutting_down")
        await worker.stop()
        await browser_manager.close()
        await engine.dispose()


def create_app(settings: PublisherSettings | None = None) -> FastAPI:
    if settings is None:
        settings = PublisherSettings()

    app = FastAPI(
        title="Browser Publisher",
        version="0.3.0",
        description="Automated browser publishing engine for WeChat MP & Xiaohongshu",
        lifespan=lifespan,
    )
    app.state.settings = settings

    # Mount static assets
    static_dir = Path(__file__).resolve().parent / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Include routers
    app.include_router(health_router)
    app.include_router(jobs_router)
    app.include_router(media_router)
    app.include_router(platforms_router)
    app.include_router(console_router)

    return app


app = create_app()
