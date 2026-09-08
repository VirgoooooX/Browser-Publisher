"""Shared persistent browser context manager for WeChat MP and Xiaohongshu."""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from publisher.config import PublisherSettings

logger = structlog.get_logger()


class BrowserManager:
    """Manages a single shared Chromium persistent context across all platform adapters."""

    def __init__(self, settings: PublisherSettings) -> None:
        self.settings = settings
        self._playwright: Any = None
        self._context: Any = None
        self._lock = asyncio.Lock()

    async def get_context(self) -> Any:
        async with self._lock:
            if self._context is not None:
                return self._context

            from playwright.async_api import async_playwright

            self.settings.ensure_directories()
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception as exc:
                    logger.debug("stale_playwright_stop_failed", error=str(exc))
                self._playwright = None
            self._playwright = await async_playwright().start()
            try:
                context = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.settings.profile_dir),
                    headless=self.settings.headless,
                    viewport={"width": 1440, "height": 1100},
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                )
                self._context = context
                context.on("close", lambda *_: self._mark_context_closed(context))
                # Grant clipboard permissions for MP
                try:
                    await self._context.grant_permissions(
                        ["clipboard-read", "clipboard-write"],
                        origin="https://mp.weixin.qq.com",
                    )
                except Exception:
                    pass
                return self._context
            except Exception as exc:
                logger.error("browser_context_launch_failed", error=str(exc))
                if self._playwright is not None:
                    try:
                        await self._playwright.stop()
                    except Exception as stop_exc:
                        logger.debug("cleanup_playwright_failed", error=str(stop_exc))
                    self._playwright = None
                self._context = None
                raise

    def _mark_context_closed(self, context: Any) -> None:
        if self._context is context:
            self._context = None

    async def get_page(self) -> Any:
        context = await self.get_context()
        pages = context.pages
        return pages[0] if pages else await context.new_page()

    async def close(self) -> None:
        async with self._lock:
            if self._context is not None:
                try:
                    await self._context.close()
                except Exception as exc:
                    logger.debug("shared_browser_context_close_failed", error=str(exc))
                self._context = None
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception as exc:
                    logger.debug("shared_playwright_stop_failed", error=str(exc))
                self._playwright = None

    async def check_health(self) -> bool:
        """Check if browser context is alive and operational."""
        async with self._lock:
            if self._context is None:
                return False
            try:
                _ = self._context.pages
                return True
            except Exception:
                return False
