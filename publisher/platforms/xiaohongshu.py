"""Playwright automation adapter for Xiaohongshu Creator Center (小红书创作服务平台)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import re
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
XHS_MANAGE_URL = f"{XHS_ORIGIN}/new/home"

QR_SELECTORS = [
    "img.css-1lhmg90",
    ".qrcode-img",
    "img.qrcode",
    ".qrcode-wrapper img",
    'img[src*="qrcode"]',
    'img[src^="data:image"]',
    "canvas.qrcode",
]


class XiaohongshuPublisher(BasePlatformPublisher):
    """Automates image-text draft creation, publication, and risk detection on Xiaohongshu."""

    def __init__(
        self,
        settings: PublisherSettings,
        browser_manager: Any = None,
    ) -> None:
        super().__init__(settings)
        self._browser_manager = browser_manager
        self._playwright: Any = None
        self._context: Any = None
        self._active_page: Any = None

    async def _get_context(self) -> Any:
        if self._browser_manager is not None:
            return await self._browser_manager.get_context()
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
        user_data = self.settings.data_dir / "profiles" / "xiaohongshu"
        user_data.mkdir(parents=True, exist_ok=True)
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(user_data),
            headless=self.settings.headless,
            viewport={"width": 1440, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
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
            if "/creator/home" not in page.url and "/new/home" not in page.url and "/publish" not in page.url:
                goto_res = page.goto(
                    XHS_HOME_URL,
                    timeout=int(self.settings.navigation_timeout_seconds * 1000),
                    wait_until="domcontentloaded",
                )
                if asyncio.iscoroutine(goto_res) or hasattr(goto_res, "__await__"):
                    await goto_res
                if hasattr(page, "wait_for_timeout"):
                    try:
                        timeout_res = page.wait_for_timeout(2500)
                        if asyncio.iscoroutine(timeout_res) or hasattr(timeout_res, "__await__"):
                            await timeout_res
                    except Exception:
                        await asyncio.sleep(0.5)
                else:
                    await asyncio.sleep(2.5)
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
                ".user-info",
                ".name-box",
                ".user-name",
                ".avatar",
                ".header-user",
                'span:has-text("笔记管理")',
                ':has-text("发布笔记")',
                ':has-text("发布图文笔记")',
                'a:has-text("发布笔记")',
                'button:has-text("发布笔记")',
                'a:has-text("发布作品")',
                'button:has-text("发布作品")',
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

        # If current view is SMS login rather than QR login, click corner switch badge
        try:
            qr_title = page.get_by_text("APP扫一扫登录")
            is_qr_mode = await qr_title.count() > 0 and await qr_title.first.is_visible()
            if not is_qr_mode:
                corner = page.locator(".css-jjnw1w img, .css-wemwzq").first
                if await corner.count() > 0 and await corner.is_visible():
                    box = await corner.bounding_box()
                    if box:
                        await page.mouse.click(
                            box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2,
                        )
                        await page.wait_for_timeout(1000)
        except Exception as exc:
            logger.debug("xhs_switch_qr_mode_failed", error=str(exc))

        for selector in QR_SELECTORS:
            try:
                locs = page.locator(selector)
                count = await locs.count()
                for i in range(count):
                    loc = locs.nth(i)
                    if await loc.is_visible():
                        box = await loc.bounding_box()
                        # Real QR code is large (~160x160); skip corner switch button (64x64)
                        if box and (box["width"] < 100 or box["height"] < 100):
                            continue
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

    @staticmethod
    def extract_and_clean_topics(
        body_text: str, existing_topics: list[str] | None
    ) -> tuple[str, list[str]]:
        """Extract trailing hashtags from body_text, deduplicate with existing_topics, and return clean body."""
        topics = list(existing_topics or [])
        lines = body_text.splitlines()
        cleaned_lines = []
        for line in lines:
            stripped = line.strip()
            # If the line consists only of #words (e.g. "#OpenAI #Codex #ChatGPT #AI编程")
            if stripped and re.match(r"^(?:#[\w\u4e00-\u9fa5\-]+(?:\s+|$))+$", stripped):
                found_tags = re.findall(r"#([\w\u4e00-\u9fa5\-]+)", stripped)
                for tag in found_tags:
                    if tag.lower() not in [t.lower() for t in topics]:
                        topics.append(tag)
            else:
                cleaned_lines.append(line)
        return "\n".join(cleaned_lines).strip(), topics

    async def _insert_topics(
        self, page: Any, editor_loc: Any, topics: list[str]
    ) -> None:
        """Insert genuine Xiaohongshu interactive topic entities at the end of the editor."""
        if not topics:
            return
        try:
            # Focus editor and collapse selection to end
            await editor_loc.click()
            el_handle = await editor_loc.element_handle()
            if el_handle:
                await page.evaluate(
                    """(el) => {
                        const range = document.createRange();
                        const sel = window.getSelection();
                        range.selectNodeContents(el);
                        range.collapse(false);
                        sel.removeAllRanges();
                        sel.addRange(range);
                    }""",
                    el_handle,
                )
            if hasattr(page, "keyboard") and page.keyboard:
                await page.keyboard.press("Enter")
                await page.keyboard.press("Enter")
                await asyncio.sleep(0.3)

            for t in topics:
                clean_t = t.strip().lstrip("#").strip()
                if not clean_t:
                    continue
                topic_btn = page.locator(
                    '#topicBtn, button.topic-btn, .topic-container button'
                ).first
                if await topic_btn.count() > 0 and await topic_btn.is_visible():
                    await topic_btn.click()
                    await asyncio.sleep(0.3)
                    if hasattr(page, "keyboard") and page.keyboard:
                        await page.keyboard.type(clean_t, delay=40)
                        # Wait for Xiaohongshu topic dropdown to query and highlight
                        await asyncio.sleep(0.8)
                        await page.keyboard.press("Enter")
                        await asyncio.sleep(0.3)
                        await page.keyboard.type(" ")
                        await asyncio.sleep(0.2)
        except Exception as exc:
            logger.warning("xhs_insert_topics_failed", error=str(exc))

    async def save_draft(
        self,
        job: PublishJob,
        media_paths: list[Path],
    ) -> tuple[str, str | None]:
        """Fill Xiaohongshu image-text note and save as draft."""
        page = await self.get_page()
        content = job.content or {}
        title = (content.get("title") or "").strip()[:20]  # Max 20 chars
        raw_body_text = (content.get("body_text") or "").strip()

        # Clean trailing raw hashtags from body_text and extract all unique topic names
        body_text, all_topics = self.extract_and_clean_topics(raw_body_text, job.topics)

        # 1. Navigate to Publish page
        await page.goto(
            XHS_PUBLISH_URL,
            timeout=int(self.settings.navigation_timeout_seconds * 1000),
            wait_until="domcontentloaded",
        )
        await asyncio.sleep(1.0)
        await self.check_risk_control(page)

        # 2. Select '上传图文' tab if not already active
        with contextlib.suppress(Exception):
            tabs = page.locator(".creator-tab:not([style*='-9999'])")
            count_res = tabs.count()
            count = await count_res if (asyncio.iscoroutine(count_res) or hasattr(count_res, "__await__")) else count_res
            if isinstance(count, int) and count > 0:
                for i in range(count):
                    t = tabs.nth(i)
                    if asyncio.iscoroutine(t) or hasattr(t, "__await__"):
                        t = await t
                    text = ""
                    if hasattr(t, "inner_text"):
                        text_res = t.inner_text()
                        text = (await text_res if (asyncio.iscoroutine(text_res) or hasattr(text_res, "__await__")) else str(text_res)).strip()
                    box = None
                    if hasattr(t, "bounding_box"):
                        box_res = t.bounding_box()
                        box = await box_res if (asyncio.iscoroutine(box_res) or hasattr(box_res, "__await__")) else box_res
                    if "上传图文" in text and (not box or (isinstance(box, dict) and box.get("x", 0) > 0 and box.get("y", 0) > 0)):
                        clk = t.click(force=True)
                        if asyncio.iscoroutine(clk) or hasattr(clk, "__await__"):
                            await clk
                        await asyncio.sleep(1.0)
                        break

        # 3. Upload images (1 to 18 images)
        if not media_paths:
            raise RuntimeError("CONTENT_REJECTED: Xiaohongshu requires 1-18 images")

        file_input = page.locator(
            'input[type="file"][accept*="image"], input.upload-input, input[type="file"]'
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
            previews = page.locator(
                ".image-item, .preview-item, .upload-image-list .item, .image-preview, .img-item, [class*='preview'], .img-list"
            )
            if await previews.count() > 0:
                break
            if await page.locator('input[placeholder*="标题"], input.title-input, input.d-text').count() > 0:
                break
            await asyncio.sleep(0.5)

        # 4. Fill Title
        title_filled = False
        title_locators = [
            page.locator('input[placeholder*="填写标题"]'),
            page.locator('input.c-input_inner[placeholder*="标题"]'),
            page.locator('input.d-text[placeholder*="标题"]'),
            page.locator('input.d-text'),
            page.locator("input.title-input"),
            page.locator('input[maxlength="20"]'),
            page.locator('input[placeholder*="标题"]'),
            page.locator(".title-container input"),
            page.locator(".header-input input"),
        ]
        title_wait_deadline = time.time() + self.settings.operation_timeout_seconds
        while time.time() < title_wait_deadline:
            await self.check_risk_control(page)
            for t_loc in title_locators:
                with contextlib.suppress(Exception):
                    if await t_loc.count() > 0 and await t_loc.first.is_visible():
                        await t_loc.first.click()
                        await t_loc.first.fill(title)
                        title_filled = True
                        break
            if title_filled:
                break
            await asyncio.sleep(0.5)

        if not title_filled:
            debug_path = self.settings.artifacts_dir / f"xhs_editor_not_found_{job.id}.png"
            with contextlib.suppress(Exception):
                await page.screenshot(path=str(debug_path))
            raise RuntimeError("EDITOR_NOT_FOUND: Xiaohongshu title input not found")

        # 5. Fill Body Text
        body_filled = False
        chosen_b_loc = None
        body_locators = [
            page.locator('div.tiptap.ProseMirror[contenteditable="true"]'),
            page.locator('div.post-content[contenteditable="true"]'),
            page.locator('div[contenteditable="true"]'),
            page.locator('textarea[placeholder*="填写更全面的信息"]'),
            page.locator('textarea[placeholder*="正文"]'),
            page.locator("textarea.content-input"),
            page.locator('div.ql-editor[contenteditable="true"]'),
            page.locator('.editor-content[contenteditable="true"]'),
            page.locator("textarea"),
        ]
        for b_loc in body_locators:
            with contextlib.suppress(Exception):
                if await b_loc.count() > 0 and await b_loc.first.is_visible():
                    chosen_b_loc = b_loc.first
                    await chosen_b_loc.click()
                    with contextlib.suppress(Exception):
                        await chosen_b_loc.fill(body_text)
                        body_filled = True
                        break
                    # If fill fails on contenteditable div, evaluate textContent
                    await page.evaluate(
                        """([el, text]) => {
                            el.innerText = text;
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }""",
                        [await chosen_b_loc.element_handle(), body_text],
                    )
                    body_filled = True
                    break

        if not body_filled:
            raise RuntimeError("EDITOR_NOT_FOUND: Xiaohongshu body editor not found")

        # Insert real interactive topics into editor if any
        if all_topics and chosen_b_loc is not None:
            await self._insert_topics(page, chosen_b_loc, all_topics)

        await self.check_risk_control(page)

        # Check if visibility setting is specified (e.g. "private" / "仅自己可见")
        visibility = (job.content or {}).get("visibility")
        if visibility in ["private", "仅自己可见", "self_only"]:
            try:
                vis_btn = page.get_by_text("公开可见", exact=False).first
                if await vis_btn.count() == 0:
                    vis_btn = page.locator('*:has-text("公开可见")').last
                if await vis_btn.count() > 0 and await vis_btn.is_visible():
                    await vis_btn.click()
                    await asyncio.sleep(0.5)
                    private_opt = page.get_by_text("仅自己可见", exact=False).first
                    if await private_opt.count() > 0 and await private_opt.is_visible():
                        await private_opt.click()
                        await asyncio.sleep(0.5)
                        logger.info("xhs_visibility_set_to_private", job_id=job.id)
            except Exception as exc:
                logger.debug("xhs_set_visibility_private_failed", error=str(exc))

        # 6. Click '暂存离开' / Save Draft
        save_triggered = False
        # Priority A: call _onSave() directly on custom element xhs-publish-btn
        try:
            save_triggered = await page.evaluate('''async () => {
                const el = document.querySelector('xhs-publish-btn');
                if (el && typeof el._onSave === 'function') {
                    await el._onSave();
                    return true;
                }
                return false;
            }''')
        except Exception as exc:
            logger.debug("xhs_custom_element_on_save_failed", error=str(exc))

        if not save_triggered:
            draft_btn = page.locator(
                'button:has-text("暂存离开"), button:has-text("存草稿"), button:has-text("暂存"), .save-draft-btn, .draft-btn'
            ).first
            if await draft_btn.count() > 0 and await draft_btn.is_visible():
                await draft_btn.click(force=True)
                save_triggered = True

        if not save_triggered:
            raise RuntimeError(
                "EDITOR_NOT_FOUND: Xiaohongshu '暂存离开' button not found"
            )

        logger.info("xhs_draft_button_clicked", job_id=job.id)

        # Wait for save confirmation
        save_confirmed = False
        start_wait = time.time()
        while time.time() - start_wait < self.settings.operation_timeout_seconds:
            await self.check_risk_control(page)
            for toast_text in ["保存成功", "已存为草稿", "草稿保存成功", "成功"]:
                toast = page.get_by_text(toast_text, exact=False)
                if await toast.count() > 0 and await toast.first.is_visible():
                    save_confirmed = True
                    break
            if save_confirmed:
                break
            if "manage" in page.url or "new/home" in page.url:
                save_confirmed = True
                break
            await asyncio.sleep(0.5)

        if not save_confirmed and (isinstance(save_triggered, bool) and save_triggered):
            save_confirmed = True

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

        # Priority A: call _onPublish() directly on custom element xhs-publish-btn
        published = False
        try:
            published = await page.evaluate('''async () => {
                const el = document.querySelector('xhs-publish-btn');
                if (el && typeof el._onPublish === 'function') {
                    await el._onPublish();
                    return true;
                }
                return false;
            }''')
        except Exception as exc:
            logger.debug("xhs_custom_element_on_publish_failed", error=str(exc))

        if not published:
            publish_btn = page.locator(
                'button.publishBtn, button:has-text("发布"):not(:has-text("草稿")), .publish-btn'
            ).first
            if await publish_btn.count() == 0 or not await publish_btn.first.is_visible():
                raise RuntimeError(
                    "PUBLISH_CONFIRM_FAILED: Xiaohongshu '发布' button not found"
                )
            await publish_btn.click(force=True)

        logger.info("xhs_publish_button_clicked", job_id=job.id)
        await asyncio.sleep(1.0)
        await self.check_risk_control(page)

        # Check for optional confirmation modal
        confirm_btn = page.locator(
            '.c-modal button:has-text("确认"), [role="dialog"] button:has-text("确认")'
        ).first
        if await confirm_btn.count() > 0 and await confirm_btn.first.is_visible():
            await confirm_btn.click(force=True)

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
