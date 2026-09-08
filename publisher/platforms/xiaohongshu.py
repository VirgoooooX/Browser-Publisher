"""Playwright automation adapter for Xiaohongshu Creator Center (小红书创作服务平台)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from publisher.config import PublisherSettings
from publisher.models import PublishJob
from publisher.platforms.base import BasePlatformPublisher

logger = structlog.get_logger()

# URLs and selectors
XHS_ORIGIN = "https://creator.xiaohongshu.com"
XHS_HOME_URL = f"{XHS_ORIGIN}/"
XHS_LOGIN_URL = f"{XHS_ORIGIN}/login"
XHS_PUBLISH_URL = f"{XHS_ORIGIN}/publish/publish"
XHS_MANAGE_URL = f"{XHS_ORIGIN}/manage/note"

QR_SELECTORS = [
    'img[src^="data:image"]',
    ".qrcode-img",
    "img.qrcode",
    ".qrcode-wrapper img",
    'img[src*="qrcode"]',
    "canvas.qrcode",
]


class XiaohongshuPublisher(BasePlatformPublisher):
    """Automates image-text draft creation, publication, and risk detection on Xiaohongshu."""

    def __init__(
        self, settings: PublisherSettings, browser_manager: Any = None
    ) -> None:
        super().__init__(settings)
        self._browser_manager = browser_manager
        self._playwright: Any = None
        self._context: Any = None
        self._active_page: Any = None

    async def _get_context(self) -> Any:
        if self._browser_manager is not None:
            self._context = await self._browser_manager.get_context()
            return self._context
        if self._context is None:
            await self.start()
        return self._context

    async def start(self) -> None:
        """Launch persistent browser context."""
        if self._browser_manager is not None:
            self._context = await self._browser_manager.get_context()
            return

        from playwright.async_api import async_playwright

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
            ],
        )

    async def close(self) -> None:
        """Close browser context."""
        if self._browser_manager is not None:
            return

        if self._context is not None:
            try:
                await self._context.close()
            except Exception as exc:
                logger.debug("xhs_browser_context_close_failed", error=str(exc))
            self._context = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as exc:
                logger.debug("xhs_playwright_stop_failed", error=str(exc))
            self._playwright = None
        self._active_page = None

    async def get_page(self) -> Any:
        if self._browser_manager is not None:
            return await self._browser_manager.get_page()

        if not self._context:
            raise RuntimeError("Playwright browser context is not initialized")
        pages = self._context.pages
        self._active_page = pages[0] if pages else await self._context.new_page()
        return self._active_page

    async def check_login(self) -> bool:
        """Navigate to Xiaohongshu creator center and check if session is authenticated."""
        page = await self.get_page()
        try:
            if XHS_ORIGIN not in page.url or "/login" in page.url:
                await page.goto(
                    XHS_MANAGE_URL,
                    timeout=int(self.settings.navigation_timeout_seconds * 1000),
                    wait_until="domcontentloaded",
                )
            url = page.url
            if "/login" in url:
                return False

            if hasattr(page, "title"):
                title = page.title()
                if asyncio.iscoroutine(title) or hasattr(title, "__await__"):
                    title = await title
                if isinstance(title, str) and "页面不见了" in title:
                    return False

            # Check for creator center logged-in indicators
            for sel in [
                ".user-name",
                ".avatar",
                ".header-user",
                'a:has-text("发布作品")',
                'button:has-text("发布作品")',
                'span:has-text("笔记管理")',
            ]:
                loc = page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    return True
        except Exception as exc:
            logger.debug("xhs_check_login_failed", error=str(exc))
        return False

    async def capture_qr(self) -> str | None:
        """Locate and capture the login QR code image snippet as base64 PNG."""
        page = await self.get_page()
        try:
            if "/login" not in page.url:
                await page.goto(
                    XHS_LOGIN_URL,
                    timeout=int(self.settings.navigation_timeout_seconds * 1000),
                    wait_until="domcontentloaded",
                )
        except Exception as exc:
            logger.debug("xhs_goto_login_failed", error=str(exc))

        # Dismiss agreement popup if visible
        for text in ["同意并继续", "同意"]:
            try:
                agree_btn = page.get_by_text(text, exact=True)
                if await agree_btn.count() > 0 and await agree_btn.first.is_visible():
                    await agree_btn.first.click()
                    await page.wait_for_timeout(500)
            except Exception:
                pass

        for selector in QR_SELECTORS:
            try:
                loc = page.locator(selector).first
                await loc.wait_for(state="visible", timeout=3000)
                src = await loc.get_attribute("src")
                if src and "data:image" in src and "base64," in src:
                    return src.split("base64,", 1)[1]
                png_bytes = await loc.screenshot(type="png")
                return base64.b64encode(png_bytes).decode("ascii")
            except Exception as exc:
                logger.debug(
                    "xhs_qr_screenshot_failed", selector=selector, error=str(exc)
                )
        return None

    async def clear_auth(self) -> None:
        """Clear cookies for Xiaohongshu domain only and force navigation away from authenticated state."""
        context = await self._get_context()
        if context:
            for domain in ["creator.xiaohongshu.com", ".xiaohongshu.com"]:
                with contextlib.suppress(Exception):
                    await context.clear_cookies(domain=domain)
        page = await self.get_page()
        with contextlib.suppress(Exception):
            await page.goto(
                XHS_LOGIN_URL,
                timeout=10000,
                wait_until="domcontentloaded",
            )

    async def _get_context(self) -> Any:
        if self._browser_manager is not None:
            return await self._browser_manager.get_context()
        return self._context

    async def check_risk_control(self, page: Any) -> None:
        """Inspect page for security checks, captchas, or rate limits."""
        # 1. Rate limits & account alerts
        for text in ["操作过于频繁", "操作频繁", "系统繁忙", "账号异常", "访问受限"]:
            loc = page.get_by_text(text, exact=False)
            if await loc.count() > 0 and await loc.first.is_visible():
                raise RuntimeError(
                    f"RATE_LIMIT_TRIGGERED: Xiaohongshu rate limit or account notice: '{text}'"
                )

        # 2. Captcha & security verification
        captcha_selectors = [
            'div:has-text("安全验证")',
            ".geetest_holder",
            ".geetest_radar",
            ".yidun_popup",
            'div[id*="captcha"]',
            'iframe[src*="captcha"]',
            'iframe[src*="verify"]',
            ".security-verify",
        ]
        for sel in captcha_selectors:
            loc = page.locator(sel)
            if await loc.count() > 0 and await loc.first.is_visible():
                raise RuntimeError(
                    f"SECURITY_CHECK_TRIGGERED: Xiaohongshu security captcha detected ({sel})"
                )

    async def save_draft(
        self,
        job: PublishJob,
        media_paths: list[Path],
    ) -> tuple[str, str | None]:
        """Fill Xiaohongshu image-text note and save as draft."""
        page = await self.get_page()
        content = job.content or {}
        title = (content.get("title") or "").strip()[:20]  # Max 20 chars
        body_text = (content.get("body_text") or "").strip()

        # Append topics if specified
        if job.topics:
            topics_str = " ".join(f"#{t.lstrip('#')}" for t in job.topics if t.strip())
            if topics_str:
                body_text = f"{body_text}\n\n{topics_str}".strip()

        # 1. Navigate to Publish page
        await page.goto(
            XHS_PUBLISH_URL,
            timeout=int(self.settings.navigation_timeout_seconds * 1000),
            wait_until="domcontentloaded",
        )
        await asyncio.sleep(1.0)
        await self.check_risk_control(page)

        # 2. Select '上传图文' tab if not already active
        tab_locators = [
            page.locator('div[role="tab"]:has-text("上传图文")'),
            page.locator('.tab-item:has-text("图文")'),
            page.locator('button:has-text("图文")'),
            page.get_by_text("上传图文", exact=False),
        ]
        for tab in tab_locators:
            if await tab.count() > 0 and await tab.first.is_visible():
                with contextlib.suppress(Exception):
                    await tab.first.click()
                    await asyncio.sleep(0.5)
                break

        # 3. Upload images (1 to 18 images)
        if not media_paths:
            raise RuntimeError("CONTENT_REJECTED: Xiaohongshu requires 1-18 images")

        file_input = page.locator(
            'input[type="file"][accept*="image"], input.upload-input'
        ).first
        if await file_input.count() == 0:
            raise RuntimeError(
                "EDITOR_NOT_FOUND: Xiaohongshu file upload input not found"
            )

        file_str_list = [str(p.resolve()) for p in media_paths if p.is_file()]
        if not file_str_list:
            raise RuntimeError(
                "CONTENT_REJECTED: No valid local image files found for upload"
            )

        await file_input.set_input_files(file_str_list)
        logger.info("xhs_images_uploaded", count=len(file_str_list))

        # Wait for uploaded images to render
        deadline = time.time() + self.settings.operation_timeout_seconds
        while time.time() < deadline:
            await self.check_risk_control(page)
            # Look for image previews
            previews = page.locator(
                ".image-item, .preview-item, .upload-image-list .item, .image-preview, .img-item"
            )
            if await previews.count() >= len(file_str_list):
                break
            await asyncio.sleep(0.5)

        # 4. Fill Title
        title_filled = False
        title_locators = [
            page.locator('input[placeholder*="填写标题"]'),
            page.locator('input.c-input_inner[placeholder*="标题"]'),
            page.locator('input[placeholder*="标 题"]'),
            page.locator("input.title-input"),
        ]
        for t_loc in title_locators:
            if await t_loc.count() > 0 and await t_loc.first.is_visible():
                await t_loc.first.click()
                await t_loc.first.fill(title)
                title_filled = True
                break

        if not title_filled:
            raise RuntimeError("EDITOR_NOT_FOUND: Xiaohongshu title input not found")

        # 5. Fill Body Text
        body_filled = False
        body_locators = [
            page.locator('textarea[placeholder*="填写更全面的信息"]'),
            page.locator('textarea[placeholder*="正文"]'),
            page.locator("textarea.content-input"),
            page.locator('div.post-content[contenteditable="true"]'),
            page.locator('div[contenteditable="true"]'),
        ]
        for b_loc in body_locators:
            if await b_loc.count() > 0 and await b_loc.first.is_visible():
                await b_loc.first.click()
                with contextlib.suppress(Exception):
                    await b_loc.first.fill(body_text)
                    body_filled = True
                    break
                # If fill fails on contenteditable div, evaluate textContent
                await page.evaluate(
                    """([el, text]) => {
                        el.innerText = text;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }""",
                    [await b_loc.first.element_handle(), body_text],
                )
                body_filled = True
                break

        if not body_filled:
            raise RuntimeError("EDITOR_NOT_FOUND: Xiaohongshu body editor not found")

        await asyncio.sleep(0.5)
        await self.check_risk_control(page)

        # 6. Click '存草稿' (Save Draft)
        draft_btn = page.locator(
            'button:has-text("存草稿"), button:has-text("暂存"), .save-draft-btn'
        ).first
        if await draft_btn.count() == 0 or not await draft_btn.first.is_visible():
            raise RuntimeError(
                "EDITOR_NOT_FOUND: Xiaohongshu '存草稿' button not found"
            )

        await draft_btn.click()
        logger.info("xhs_draft_button_clicked", job_id=job.id)

        # Wait for save confirmation
        save_confirmed = False
        start_wait = time.time()
        while time.time() - start_wait < self.settings.operation_timeout_seconds:
            await self.check_risk_control(page)
            for toast_text in ["保存成功", "已存为草稿", "草稿保存成功"]:
                toast = page.get_by_text(toast_text, exact=False)
                if await toast.count() > 0 and await toast.first.is_visible():
                    save_confirmed = True
                    break
            if save_confirmed:
                break
            await asyncio.sleep(0.5)

        if not save_confirmed:
            raise RuntimeError(
                "DRAFT_SAVE_FAILED: Xiaohongshu draft save confirmation not detected within timeout"
            )

        # In XHS creator web UI, saved draft stays on publish URL or redirects to manage
        draft_url = page.url
        draft_id = f"xhs_draft_{job.id}"
        return draft_url, draft_id

    async def open_draft(self, draft_url: str) -> None:
        """Open draft or navigate to notes management page."""
        page = await self.get_page()
        await page.goto(
            draft_url if "creator.xiaohongshu.com" in draft_url else XHS_MANAGE_URL,
            timeout=int(self.settings.navigation_timeout_seconds * 1000),
            wait_until="domcontentloaded",
        )

    async def publish_and_confirm(self, job: PublishJob) -> None:
        """Click the final '发布' button on Xiaohongshu and wait for confirmation."""
        page = await self.get_page()
        await self.check_risk_control(page)

        publish_btn = page.locator(
            'button.publishBtn, button:has-text("发布"):not(:has-text("草稿")), .publish-btn'
        ).first
        if await publish_btn.count() == 0 or not await publish_btn.first.is_visible():
            raise RuntimeError(
                "PUBLISH_CONFIRM_FAILED: Xiaohongshu '发布' button not found"
            )

        await publish_btn.click()
        logger.info("xhs_publish_button_clicked", job_id=job.id)
        await asyncio.sleep(1.0)
        await self.check_risk_control(page)

        # Check for optional confirmation modal
        confirm_btn = page.locator(
            '.c-modal button:has-text("确认"), [role="dialog"] button:has-text("确认")'
        ).first
        if await confirm_btn.count() > 0 and await confirm_btn.first.is_visible():
            await confirm_btn.click()

    async def verify_published(
        self, job: PublishJob, start_time: datetime
    ) -> str | None:
        """Verify note was published by inspecting the management list."""
        return await self.reconcile(job, start_time, max_seconds=30)

    async def reconcile(
        self,
        job: PublishJob,
        start_time: datetime,
        max_seconds: int = 120,
    ) -> str | None:
        """Poll the Xiaohongshu works management page to find the note title and URL."""
        del start_time
        page = await self.get_page()
        title = ((job.content or {}).get("title") or "").strip()[:20]
        deadline = asyncio.get_event_loop().time() + max_seconds

        while asyncio.get_event_loop().time() < deadline:
            try:
                if "/manage/note" not in page.url:
                    await page.goto(
                        XHS_MANAGE_URL,
                        timeout=int(self.settings.navigation_timeout_seconds * 1000),
                        wait_until="domcontentloaded",
                    )
                    await asyncio.sleep(1.0)

                await self.check_risk_control(page)

                # Search for note item with matching title
                item = page.locator(
                    f'.note-item:has-text("{title}"), '
                    f'.note-card:has-text("{title}"), '
                    f'tr:has-text("{title}"), '
                    f'div:has-text("{title}")'
                )
                if await item.count() > 0:
                    link = item.first.locator(
                        'a[href*="xiaohongshu.com"], a[href*="/explore/"]'
                    )
                    if await link.count() > 0:
                        href = await link.first.get_attribute("href")
                        if href:
                            full_url = str(href)
                            if full_url.startswith("/"):
                                full_url = f"https://www.xiaohongshu.com{full_url}"
                            return full_url
                    # Alternatively, if note card has note-id
                    card_el = item.first
                    note_id = await card_el.get_attribute("data-note-id")
                    if note_id:
                        return f"https://www.xiaohongshu.com/explore/{note_id}"
            except Exception as exc:
                logger.debug("xhs_reconcile_check_error", error=str(exc))
            await asyncio.sleep(5.0)

        return None
