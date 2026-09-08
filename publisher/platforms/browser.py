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
            self._playwright = await async_playwright().start()
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.settings.profile_dir),
                headless=self.settings.headless,
                viewport={"width": 1440, "height": 1100},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            # Grant clipboard permissions for MP
            try:
                await self._context.grant_permissions(
                    ["clipboard-read", "clipboard-write"],
                    origin="https://mp.weixin.qq.com",
                )
            except Exception:
                pass
            return self._context

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
