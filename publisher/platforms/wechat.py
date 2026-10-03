"""Playwright automation adapter for the WeChat Official Account web management UI."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit, urlunsplit

import httpx
import structlog

from publisher.config import PublisherSettings
from publisher.models import PublishJob
from publisher.platforms.base import BasePlatformPublisher
from publisher.platforms.render import render_wechat_html
from publisher.platforms.wechat_api import WeChatApiClient
from publisher.security import safe_error_summary

logger = structlog.get_logger()

# Selectors and constants
MP_ORIGIN = "https://mp.weixin.qq.com"
MP_HOME_URL = f"{MP_ORIGIN}/"
MP_DRAFT_LIST_URL_TEMPLATE = (
    f"{MP_ORIGIN}/cgi-bin/appmsg?begin=0&count=20&type=77&action=list_card"
    "&token={token}&lang=zh_CN"
)
QR_SELECTORS = [
    "img.js_login_qrcode",
    ".js_login_qrcode img",
    'img[src*="scanloginqrcode"]',
    ".login__type__container img",
]
CONFIRM_BUTTON_NAMES = [
    "继续发表",
    "发表",
    "继续群发",
    "群发",
    "确认",
    "确定",
]
NO_DECLARATION_BUTTON_NAMES = ["无需声明并发表", "无需声明并群发"]
DECLARATION_PROMPT_TEXT = "若你发表的内容涉及"
GROUP_NOTIFICATION_LABEL_NAMES = ["群发通知", "发送群通知"]
PUBLISH_ENTRY_BUTTON_NAMES = ["下一步", "发表", "群发"]
API_DRAFT_LIST_POLL_INTERVAL_SECONDS = 0.25
API_DRAFT_LIST_REFRESH_INTERVAL_SECONDS = 5.0
API_DRAFT_LIST_MIN_WAIT_SECONDS = 60.0
API_DRAFT_LIST_MAX_WAIT_SECONDS = 120.0
PUBLISH_ENTRY_RETRY_DELAY_SECONDS = 2.0
PUBLISH_VERIFY_MAX_WAIT_SECONDS = 120


class WeChatPublisher(BasePlatformPublisher):
    """Create drafts through the official API and publish them through Playwright."""

    def __init__(
        self,
        settings: PublisherSettings,
        browser_manager: Any = None,
        wechat_api_client: WeChatApiClient | None = None,
    ) -> None:
        super().__init__(settings)
        self._browser_manager = browser_manager
        self._wechat_api = wechat_api_client or (
            WeChatApiClient(settings) if settings.wechat_mp_api_configured else None
        )
        self._playwright: Any = None
        self._context: Any = None
        self._active_page: Any = None

    @property
    def draft_creation_phase(self) -> str:
        """Use a separate checkpoint when the official API owns draft creation."""

        return "api_creating_draft" if self._wechat_api is not None else "editing"

    @property
    def draft_requires_browser_login(self) -> bool:
        """The official draft API uses credentials independent of Playwright."""

        return self._wechat_api is None

    async def get_context(self) -> Any:
        """Return the shared or dedicated browser context."""
        if self._browser_manager is not None:
            self._context = await self._browser_manager.get_context()
            return self._context
        if self._context is None:
            await self.start()
        return self._context

    async def start(self) -> None:
        """Launch persistent browser context with required permissions."""
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
        )
        try:
            await self._context.grant_permissions(
                ["clipboard-read", "clipboard-write"],
                origin=MP_ORIGIN,
            )
        except Exception:
            logger.warning("wechat_failed_to_grant_clipboard_permissions")

    async def close(self) -> None:
        """Close browser context and stop Playwright if owned."""
        if self._browser_manager is None:
            if self._context is not None:
                try:
                    await self._context.close()
                except Exception as exc:
                    logger.debug("wechat_browser_context_close_failed", error=str(exc))
                self._context = None
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception as exc:
                    logger.debug("wechat_playwright_stop_failed", error=str(exc))
                self._playwright = None
            self._active_page = None
        if self._wechat_api is not None:
            await self._wechat_api.close()

    async def get_page(self) -> Any:
        if self._browser_manager is not None:
            if self._active_page is not None:
                try:
                    if not self._active_page.is_closed():
                        return self._active_page
                except Exception:
                    self._active_page = None
            self._active_page = await self._browser_manager.get_page()
            return self._active_page

        if not self._context:
            raise RuntimeError("Playwright browser context is not initialized")
        pages = self._context.pages
        self._active_page = pages[0] if pages else await self._context.new_page()
        return self._active_page

    async def _page_requires_login(self, page: Any) -> bool:
        if "login" in urlsplit(str(page.url)).path.lower():
            return True
        for text in (
            "登录超时",
            "请重新登录",
            "重新扫码登录",
            "微信扫一扫，选择公众平台账号登录",
        ):
            locator = page.get_by_text(text, exact=False)
            if await locator.count() and await locator.first.is_visible():
                return True
        for selector in QR_SELECTORS:
            locator = page.locator(selector)
            if await locator.count() and await locator.first.is_visible():
                return True
        return False

    async def _page_is_authenticated(self, page: Any) -> bool:
        # An expired session can return HTTP 200 on the original /cgi-bin URL.
        if await self._page_requires_login(page):
            return False
        for locator in (
            page.locator(".weui-desktop-account__info"),
            page.get_by_text("新的创作", exact=False),
            page.get_by_text("近期草稿", exact=False),
        ):
            if await locator.count() and await locator.first.is_visible():
                return True
        if "action=edit" in page.url or "appmsg_edit" in page.url:
            entry, _ = await self._find_publish_entry(page)
            return entry is not None
        return False

    async def _raise_if_auth_required(self, page: Any) -> None:
        if await self._page_requires_login(page):
            raise RuntimeError(
                "AUTH_REQUIRED: WeChat browser session expired; scan to log in again"
            )

    async def check_login(self) -> bool:
        """Inspect authenticated DOM rather than trusting the page URL."""
        page = await self.get_page()
        try:
            if urlsplit(page.url).netloc != "mp.weixin.qq.com":
                await page.goto(
                    MP_HOME_URL,
                    timeout=int(self.settings.navigation_timeout_seconds * 1000),
                    wait_until="domcontentloaded",
                )
            return await self._page_is_authenticated(page)
        except Exception as exc:
            raise RuntimeError(
                "NETWORK_ERROR: Unable to verify WeChat browser session"
            ) from exc

    async def refresh_session(self) -> bool:
        """Probe a fresh home page without replacing a login or confirmation tab."""
        context = await self.get_context()
        page = await context.new_page()
        try:
            await page.goto(
                MP_HOME_URL,
                timeout=int(self.settings.navigation_timeout_seconds * 1000),
                wait_until="domcontentloaded",
            )
            deadline = asyncio.get_running_loop().time() + 5.0
            while asyncio.get_running_loop().time() < deadline:
                if await self._page_requires_login(page):
                    return False
                if await self._page_is_authenticated(page):
                    return True
                await asyncio.sleep(0.25)
            raise RuntimeError(
                "PROVIDER_UI_CHANGED: WeChat session probe did not show a known page"
            )
        finally:
            await page.close()

    async def capture_qr(self) -> str | None:
        """Locate and capture the login QR code image snippet as base64 PNG."""
        page = await self.get_page()
        try:
            # Explicit QR refresh must renew an expired code. Background probes
            # reuse the cached QR and do not keep interrupting a pending scan.
            await page.goto(
                MP_HOME_URL,
                timeout=int(self.settings.navigation_timeout_seconds * 1000),
                wait_until="domcontentloaded",
            )
        except Exception as exc:
            logger.debug("wechat_goto_login_failed", error=safe_error_summary(exc))
            return None

        if await self._page_is_authenticated(page):
            return None

        for selector in QR_SELECTORS:
            try:
                qr_locator = page.locator(selector).first
                await qr_locator.wait_for(state="visible", timeout=4000)
                png_bytes = await qr_locator.screenshot(type="png")
                return base64.b64encode(png_bytes).decode("ascii")
            except Exception as exc:
                logger.debug(
                    "wechat_qr_screenshot_failed", selector=selector, error=str(exc)
                )
        return None

    async def clear_auth(self) -> None:
        """Clear WeChat cookies and navigate away from authenticated state."""
        context = await self.get_context()
        if context:
            for domain in ["mp.weixin.qq.com", ".weixin.qq.com", ".qq.com"]:
                with contextlib.suppress(Exception):
                    await context.clear_cookies(domain=domain)
        page = await self.get_page()
        with contextlib.suppress(Exception):
            await page.goto(
                f"{MP_ORIGIN}/cgi-bin/logout?t=wxm-logout",
                timeout=10000,
                wait_until="domcontentloaded",
            )

    async def open_editor(self, page: Any) -> Any:
        """Open the article editor from the home dashboard or direct navigation."""
        if "/cgi-bin/" not in page.url:
            await page.goto(
                MP_HOME_URL,
                timeout=int(self.settings.navigation_timeout_seconds * 1000),
                wait_until="domcontentloaded",
            )

        new_article_btn = page.locator(
            '.new-creation__menu-item:has-text("文章"), .appmsg_edit'
        )
        if await new_article_btn.count() == 0:
            new_article_btn = page.get_by_text("文章", exact=True)
        if (
            await new_article_btn.count() > 0
            and await new_article_btn.first.is_visible()
        ):
            context = await self.get_context()
            async with context.expect_page(timeout=15000) as page_info:
                await new_article_btn.first.click()
            editor_page = await page_info.value
            await editor_page.wait_for_load_state("domcontentloaded")
            self._active_page = editor_page
            with contextlib.suppress(Exception):
                conflict_btn = editor_page.locator(
                    "button:has-text('查看新草稿'), a:has-text('查看新草稿')"
                ).first
                if await conflict_btn.count() > 0 and await conflict_btn.is_visible():
                    logger.info("wechat_draft_conflict_detected_clicking_new_draft")
                    await conflict_btn.click()
                    await editor_page.wait_for_load_state("domcontentloaded")
                    await asyncio.sleep(1.0)
            return editor_page

        if "/cgi-bin/appmsg" in page.url:
            return page

        raise RuntimeError(
            "EDITOR_NOT_FOUND: Could not open article editor from dashboard"
        )

    async def fill_article(self, page: Any, job: PublishJob) -> None:
        """Fill title, author, digest, and rich text body into editor."""
        content = job.content or {}
        title = content.get("title", "")
        author = content.get("author", "")
        digest = content.get("digest", "")
        body_text = content.get("body_text", "")
        content_html = content.get("body_html", "")

        # If body_html was not provided or body_text has raw markdown headers, apply layout fix
        if not content_html:
            content_html = render_wechat_html(
                content=body_text,
                source_url=job.source_url,
                strip_title_heading=True,
            )

        # 1. Title
        title_filled = False
        title_locators = [
            page.locator('div.ProseMirror[data-placeholder*="标题"]'),
            page.locator('.ProseMirror[data-placeholder*="标题"]'),
            page.get_by_role("textbox", name="请在这里输入标题"),
            page.locator('input[placeholder*="标题"]'),
            page.locator('textarea[placeholder*="标题"]'),
            page.locator("#title"),
            page.locator("#appmsg_title"),
        ]
        for loc in title_locators:
            try:
                if await loc.count() > 0 and await loc.first.is_visible():
                    await loc.first.click()
                    await loc.first.fill(title)
                    title_filled = True
                    break
            except Exception as exc:
                logger.debug("title_locator_try_failed", error=str(exc))

        if not title_filled:
            primary_title = page.locator('div.ProseMirror[data-placeholder*="标题"]')
            try:
                await primary_title.first.wait_for(state="visible", timeout=8000)
                await primary_title.first.click()
                await primary_title.first.fill(title)
                title_filled = True
            except Exception as exc:
                logger.debug("primary_title_fill_failed", error=str(exc))

        if not title_filled:
            raise RuntimeError("EDITOR_NOT_FOUND: Title input not found")

        # 2. Author
        if author:
            author_locators = [
                page.locator("#author"),
                page.get_by_role("textbox", name="请输入作者"),
                page.locator('input[placeholder*="作者"]'),
            ]
            for loc in author_locators:
                try:
                    if await loc.count() > 0 and await loc.first.is_visible():
                        await loc.first.fill(author)
                        break
                except Exception as exc:
                    logger.debug("author_locator_try_failed", error=str(exc))

        # 3. Digest
        if digest:
            digest_locators = [
                page.locator("#js_summary"),
                page.locator(".js_description"),
                page.locator('textarea[placeholder*="摘要"]'),
            ]
            for loc in digest_locators:
                try:
                    if await loc.count() > 0 and await loc.first.is_visible():
                        await loc.first.fill(digest)
                        break
                except Exception as exc:
                    logger.debug("digest_locator_try_failed", error=str(exc))

        # 4. Rich text body (target body editor)
        editor_locators = [
            page.locator("#ueditor_0 .ProseMirror"),
            page.locator(".rich_media_content .ProseMirror"),
            page.locator('div.ProseMirror:not([data-placeholder*="标题"])'),
            page.locator("#js_editor_content"),
            page.locator("#js_editor"),
        ]
        editor_target = None
        for loc in editor_locators:
            try:
                if await loc.count() > 0 and await loc.first.is_visible():
                    editor_target = loc.first
                    break
            except Exception as exc:
                logger.debug("editor_locator_try_failed", error=str(exc))

        if editor_target is None:
            frames = page.frames
            for frame in frames:
                frame_body = frame.locator("body.view, body.uneditable, body")
                if await frame_body.count() > 0 and await frame_body.first.is_visible():
                    editor_target = frame_body.first
                    break

        if editor_target is None:
            raise RuntimeError(
                "EDITOR_NOT_FOUND: Rich text editor contenteditable not found"
            )

        await editor_target.click()
        pasted = False
        try:
            await page.evaluate(
                """([html, text]) => {
                    const item = new ClipboardItem({
                        "text/html": new Blob([html], { type: "text/html" }),
                        "text/plain": new Blob([text], { type: "text/plain" })
                    });
                    return navigator.clipboard.write([item]);
                }""",
                [content_html, body_text],
            )
            await editor_target.focus()
            await page.keyboard.press("Control+V")
            await asyncio.sleep(1.0)
            text_len = len(await editor_target.inner_text())
            if text_len > 10:
                pasted = True
        except Exception as exc:
            logger.debug("clipboard_paste_failed", error=str(exc))

        if not pasted:
            try:
                if content_html:
                    await page.evaluate(
                        """([editor, html]) => {
                            editor.innerHTML = html;
                            editor.dispatchEvent(new Event("input", { bubbles: true }));
                            editor.dispatchEvent(new Event("change", { bubbles: true }));
                        }""",
                        [await editor_target.element_handle(), content_html],
                    )
                else:
                    await editor_target.fill(body_text)
            except Exception as dom_exc:
                logger.debug("body_dom_fallback_failed", error=str(dom_exc))
                if body_text:
                    await editor_target.fill(body_text)
            await asyncio.sleep(0.5)

    def _cover_url_for_editor(self, job: PublishJob) -> str | None:
        """Prefer Notify Hub's internal origin for media when it is configured."""
        cover_url = next(
            (
                str(item["url"])
                for item in job.media
                if item.get("kind") == "url" and item.get("url")
            ),
            None,
        )
        if not cover_url or not self.settings.notify_event_url:
            return cover_url

        source = urlsplit(cover_url)
        notify_hub = urlsplit(str(self.settings.notify_event_url))
        if not source.path or not notify_hub.scheme or not notify_hub.netloc:
            return cover_url
        return urlunsplit(
            (notify_hub.scheme, notify_hub.netloc, source.path, source.query, "")
        )

    async def _download_cover_for_upload(self, job: PublishJob) -> Path | None:
        cover_url = self._cover_url_for_editor(job)
        if not cover_url:
            return None
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(cover_url)
            content_type = response.headers.get("content-type", "").lower()
            if response.status_code != 200 or not content_type.startswith("image/"):
                logger.warning(
                    "wechat_cover_download_failed",
                    status_code=response.status_code,
                    content_type=content_type,
                )
                return None
            if not response.content or len(response.content) > 20 * 1024 * 1024:
                logger.warning(
                    "wechat_cover_download_invalid_size",
                    size_bytes=len(response.content),
                )
                return None

            suffix = Path(urlsplit(cover_url).path).suffix.lower()
            if suffix not in {".gif", ".jpeg", ".jpg", ".png", ".webp"}:
                suffix = ".png" if "png" in content_type else ".jpg"
            self.settings.media_dir.mkdir(parents=True, exist_ok=True)
            cover_path = self.settings.media_dir / f"wechat_cover_{job.id}{suffix}"
            await asyncio.to_thread(cover_path.write_bytes, response.content)
            return cover_path
        except Exception as exc:
            logger.warning("wechat_cover_download_exception", error=str(exc))
            return None

    async def _insert_cover_image(self, page: Any, cover_path: Path) -> bool:
        if not await asyncio.to_thread(cover_path.is_file):
            return False

        editor = page.locator(
            "#ueditor_0 .ProseMirror, .rich_media_content .ProseMirror, "
            'div.ProseMirror:not([data-placeholder*="标题"]), '
            "#js_editor_content, #js_editor"
        ).first
        upload_input = page.locator(
            '#js_editor_insertimage input[type="file"], '
            'input[type="file"][accept*="image"]'
        ).first
        if await editor.count() == 0 or await upload_input.count() == 0:
            logger.warning("wechat_body_image_upload_control_not_found")
            return False

        uploaded_images = page.locator('img.js_insertlocalimg[data-upload="1"]')
        before_count = await uploaded_images.count()
        try:
            picture_menu = page.locator("#js_editor_insertimage").first
            if await picture_menu.count() > 0 and await picture_menu.is_visible():
                await picture_menu.click()
            await editor.click()
            await page.keyboard.press("Control+Home")
            await upload_input.set_input_files(str(cover_path))
        except Exception as exc:
            logger.warning("wechat_body_image_upload_failed", error=str(exc))
            return False

        deadline = asyncio.get_running_loop().time() + min(
            self.settings.operation_timeout_seconds, 20
        )
        while asyncio.get_running_loop().time() < deadline:
            if await uploaded_images.count() > before_count:
                uploaded = uploaded_images.last
                src = await uploaded.get_attribute("src")
                if src and await uploaded.is_visible():
                    logger.info("wechat_body_cover_uploaded")
                    return True
            await asyncio.sleep(0.5)
        logger.warning("wechat_body_image_upload_timeout")
        return False

    async def select_cover_from_content(self, page: Any) -> bool:
        """Select first body image as article cover via '从正文选择'."""
        cover_btn = page.locator(
            ".select-cover__btn, #js_cover_area, .js_cover_btn_area, #js_cover_null"
        ).first
        if await cover_btn.count() == 0:
            logger.warning("cover_selection_button_not_found_skipping")
            return False
        try:
            await cover_btn.click()
            await asyncio.sleep(1.0)
        except Exception as exc:
            logger.warning("click_cover_btn_failed", error=str(exc))
            return False

        from_content_tab = page.locator(':has-text("从正文选择")').last
        if (
            await from_content_tab.count() == 0
            or not await from_content_tab.is_visible()
        ):
            logger.warning("from_content_tab_not_found_skipping")
            return False
        try:
            await from_content_tab.click()
            await asyncio.sleep(1.5)
        except Exception as exc:
            logger.warning("click_from_content_tab_failed", error=str(exc))
            return False

        img_pick = page.locator(
            ".appmsg_content_img_item, .appmsg_content_img, "
            ".img_crop_panel .appmsg_content_img, .weui-desktop-picture-check"
        ).first
        try:
            await img_pick.wait_for(state="visible", timeout=5000)
        except Exception:
            logger.warning("no_candidate_image_found_in_tab_skipping")
            close_btn = page.locator(
                '.weui-desktop-dialog:has-text("选择图片") button:has-text("取消")'
            )
            if await close_btn.count() > 0 and await close_btn.is_visible():
                await close_btn.first.click()
            return False
        await img_pick.click()
        await asyncio.sleep(0.5)

        next_btn = page.locator(
            'button:has-text("下一步"):not(.weui-desktop-btn_disabled)'
        ).first
        if await next_btn.count() == 0 or not await next_btn.is_visible():
            logger.warning("cover_next_button_not_found_skipping")
            return False
        await next_btn.click()
        await asyncio.sleep(1.0)

        dialog_confirmed = False
        crop_dialog = page.locator(
            '.weui-desktop-dialog:has-text("编辑封面"), .weui-desktop-dialog'
        ).first
        # The crop dialog may render its confirmation button asynchronously.
        # Poll briefly, and only inspect visible buttons in the active dialog
        # so a hidden stale dialog cannot mask the live confirmation button.
        deadline = asyncio.get_running_loop().time() + min(
            self.settings.operation_timeout_seconds, 8
        )
        while asyncio.get_running_loop().time() < deadline and not dialog_confirmed:
            for btn_name in ["确认", "完成", "确定"]:
                # WeChat renders the crop action buttons in a portal that is
                # not always a descendant of the dialog element. Exact role
                # matching still lets us restrict the click to a visible,
                # actionable confirmation button.
                confirm_btns = page.get_by_role("button", name=btn_name, exact=True)
                for index in range(await confirm_btns.count()):
                    confirm_btn = confirm_btns.nth(index)
                    if await confirm_btn.is_visible():
                        await confirm_btn.click()
                        dialog_confirmed = True
                        try:
                            await crop_dialog.wait_for(state="hidden", timeout=5000)
                        except Exception as exc:
                            logger.debug(
                                "crop_dialog_hide_wait_timeout", error=str(exc)
                            )
                        break
                if dialog_confirmed:
                    break
            if not dialog_confirmed:
                await asyncio.sleep(0.25)

        if not dialog_confirmed:
            logger.warning("cover_confirmation_button_not_found")
            return False

        preview = page.locator(
            ".js_cover_preview_new, .js_cover_preview, "
            ".appmsg_cover_preview, .setting-group__cover_primary"
        )
        try:
            await preview.first.wait_for(state="visible", timeout=5000)
        except Exception as exc:
            logger.debug("cover_preview_wait_ignored", error=str(exc))
        return True

    async def check_risk_control(self, page: Any) -> None:
        """Inspect page for explicit security verification or rate limit prompts."""
        for rate_text in ["操作过于频繁", "操作频繁"]:
            loc = page.locator(f'.weui-desktop-dialog:has-text("{rate_text}")')
            if await loc.count() > 0 and await loc.first.is_visible():
                raise RuntimeError(
                    f"RATE_LIMIT_TRIGGERED: WeChat MP rate limit detected: '{rate_text}'"
                )

        sec_locators = [
            page.locator('.weui-desktop-dialog:has-text("安全验证")'),
            page.locator('iframe[src*="tcaptcha"]'),
            page.locator('iframe[src*="captcha"]'),
        ]
        for loc in sec_locators:
            if await loc.count() > 0 and await loc.first.is_visible():
                raise RuntimeError(
                    "SECURITY_CHECK_TRIGGERED: WeChat MP security verification or captcha detected"
                )

    async def save_draft(
        self,
        job: PublishJob,
        media_paths: list[Path],
    ) -> tuple[str | None, str | None]:
        """Create an API draft, or use the legacy browser editor when unconfigured."""
        if self._wechat_api is None:
            content = job.content or {}
            if "publisher-media://" in str(
                content.get("body_text") or ""
            ) or "publisher-media://" in str(content.get("body_html") or ""):
                raise RuntimeError(
                    "INLINE_IMAGE_FAILED: publisher-media markers require WeChat official API credentials"
                )
        if self._wechat_api is not None:
            temporary_cover_path: Path | None = None
            try:
                cover_path: Path | None = None
                for path in media_paths:
                    if await asyncio.to_thread(path.is_file):
                        cover_path = path
                        break
                if cover_path is None:
                    temporary_cover_path = await self._download_cover_for_upload(job)
                    cover_path = temporary_cover_path
                if cover_path is None:
                    raise RuntimeError(
                        "COVER_FAILED: Could not download the WeChat API cover image"
                    )
                draft_media_id = await self._wechat_api.create_draft(
                    job, cover_path, media_paths=media_paths
                )
                logger.info(
                    "wechat_official_api_draft_created",
                    job_id=job.id,
                )
                return None, draft_media_id
            finally:
                if temporary_cover_path is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(
                            temporary_cover_path.unlink, missing_ok=True
                        )

        page = await self.get_page()
        editor_page = await self.open_editor(page)
        temporary_cover_path: Path | None = None
        keep_editor_open = False

        try:
            await self.fill_article(editor_page, job)
            cover_path = next((path for path in media_paths if path.is_file()), None)
            if cover_path is None:
                temporary_cover_path = await self._download_cover_for_upload(job)
                cover_path = temporary_cover_path
            if cover_path is None or not await self._insert_cover_image(
                editor_page, cover_path
            ):
                raise RuntimeError(
                    "COVER_FAILED: Could not upload the cover image into article content"
                )
            if not await self.select_cover_from_content(editor_page):
                raise RuntimeError(
                    "COVER_FAILED: Could not select a cover image from article content"
                )

            # Click save draft
            save_btn = editor_page.get_by_role("button", name="保存为草稿")
            if await save_btn.count() == 0:
                save_btn = editor_page.locator('button:has-text("保存为草稿")')
            if await save_btn.count() == 0:
                save_btn = editor_page.get_by_text("保存为草稿", exact=True)
            if await save_btn.count() == 0:
                raise RuntimeError("DRAFT_SAVE_FAILED: '保存为草稿' button not found")

            draft_media_id: str | None = None
            save_confirmed: bool = False

            async def handle_response(res: Any) -> None:
                nonlocal draft_media_id, save_confirmed
                if "appmsg" in res.url and res.request.method == "POST":
                    try:
                        data = await res.json()
                        if isinstance(data, dict):
                            mid = data.get("appmsgid") or data.get("appMsgId")
                            if mid:
                                draft_media_id = str(mid)
                                save_confirmed = True
                            base_resp = data.get("base_resp") or {}
                            if (
                                isinstance(base_resp, dict)
                                and base_resp.get("ret") == 0
                            ):
                                save_confirmed = True
                    except Exception as exc:
                        logger.debug("parse_appmsg_response_failed", error=str(exc))

            editor_page.on("response", handle_response)
            try:
                await save_btn.first.click()
                start_wait = time.time()
                max_wait = self.settings.operation_timeout_seconds
                while time.time() - start_wait < max_wait:
                    if (
                        draft_media_id
                        or "appmsgid=" in editor_page.url
                        or save_confirmed
                    ):
                        save_confirmed = True
                        break
                    saved_indicator = editor_page.locator(
                        ':has-text("已保存"), :has-text("保存成功"), :has-text("手动保存")'
                    )
                    if (
                        await saved_indicator.count() > 0
                        and await saved_indicator.first.is_visible()
                    ):
                        save_confirmed = True
                        break
                    await asyncio.sleep(0.5)
            finally:
                editor_page.remove_listener("response", handle_response)

            draft_url = editor_page.url
            if not draft_media_id and "appmsgid=" in draft_url:
                qs = parse_qs(urlsplit(draft_url).query)
                appmsgid_vals = qs.get("appmsgid")
                if appmsgid_vals:
                    draft_media_id = appmsgid_vals[0]
                    save_confirmed = True

            if (
                not save_confirmed
                and not draft_media_id
                and "appmsgid=" not in draft_url
            ):
                raise RuntimeError(
                    "DRAFT_SAVE_FAILED: Save draft timed out without explicit confirmation"
                )

            keep_editor_open = job.mode == "publish"
            return draft_url, draft_media_id
        finally:
            if temporary_cover_path is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(
                        temporary_cover_path.unlink, missing_ok=True
                    )
            if editor_page != page and not keep_editor_open:
                with contextlib.suppress(Exception):
                    await editor_page.close()
                if self._active_page == editor_page:
                    self._active_page = None

    @staticmethod
    def _is_api_draft_reference(draft_url: str | None, job: PublishJob | None) -> bool:
        """Return whether the job carries an official API draft id.

        Official API ``media_id`` values are opaque strings and are not the
        numeric ``appmsgid`` used by the MP web editor.  New API-created jobs
        have no editor URL; the compatibility check also recognizes the
        synthetic URL used by older queued test jobs.
        """
        if job is None or not job.platform_draft_id:
            return False
        if not draft_url:
            return True
        appmsgid = parse_qs(urlsplit(draft_url).query).get("appmsgid", [None])[0]
        if appmsgid != job.platform_draft_id:
            return False
        return not appmsgid.isdigit() or len(appmsgid) > 32

    @staticmethod
    def _page_token(page: Any) -> str | None:
        url = getattr(page, "url", "")
        if not isinstance(url, str) or not url:
            return None
        return parse_qs(urlsplit(url).query).get("token", [None])[0]

    async def _ensure_page_token(self, page: Any) -> str:
        # Re-enter home to obtain the current session token; old tabs may carry
        # a revoked token even though their URL still looks authenticated.
        await page.goto(
            MP_HOME_URL,
            timeout=int(self.settings.navigation_timeout_seconds * 1000),
            wait_until="domcontentloaded",
        )
        await self._raise_if_auth_required(page)
        token = self._page_token(page)
        if not token:
            raise RuntimeError(
                "AUTH_REQUIRED: WeChat session token unavailable while opening draft"
            )
        return token

    async def _find_publish_entry(self, page: Any) -> tuple[Any | None, str]:
        """Find the editor entry action without clicking it."""
        for selector in ("button.mass_send", ".mass_send", '[role="button"].mass_send'):
            mass_send_btn = page.locator(selector)
            for index in range(await mass_send_btn.count()):
                candidate = (
                    mass_send_btn.first if index == 0 else mass_send_btn.nth(index)
                )
                if not await candidate.is_visible():
                    continue
                try:
                    return candidate, (await candidate.inner_text()).strip()
                except Exception:
                    return candidate, ""

        for name in PUBLISH_ENTRY_BUTTON_NAMES:
            candidate = page.get_by_role("button", name=name, exact=True)
            for index in range(await candidate.count()):
                visible_candidate = (
                    candidate.first if index == 0 else candidate.nth(index)
                )
                if await visible_candidate.is_visible():
                    return visible_candidate, name

        # Some MP editor builds style the action as an anchor or a div instead
        # of a semantic button.  Reuse the broader modal/button selectors used
        # for the later confirmation step before declaring that no click can
        # be started.
        candidate = await self._visible_button(page, PUBLISH_ENTRY_BUTTON_NAMES)
        if candidate is not None:
            try:
                return candidate, (await candidate.inner_text()).strip()
            except Exception:
                return candidate, ""
        return None, ""

    async def _editor_is_ready(
        self, page: Any, *, timeout_seconds: float = 8.0
    ) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            entry, _ = await self._find_publish_entry(page)
            if entry is not None:
                return True
            await asyncio.sleep(0.25)
        return False

    async def _open_api_draft(self, page: Any, job: PublishJob) -> None:
        """Open an API-created draft from the MP draft list by its title."""
        title = str((job.content or {}).get("title") or "").strip()
        if not title:
            raise RuntimeError(
                "DRAFT_OPEN_FAILED: API-created WeChat draft title is unavailable"
            )

        token = await self._ensure_page_token(page)
        draft_list_url = MP_DRAFT_LIST_URL_TEMPLATE.format(token=quote(token, safe=""))
        normalized_title = " ".join(title.replace("\u00a0", " ").split())
        loop = asyncio.get_running_loop()
        wait_seconds = min(
            max(
                float(self.settings.operation_timeout_seconds),
                API_DRAFT_LIST_MIN_WAIT_SECONDS,
            ),
            API_DRAFT_LIST_MAX_WAIT_SECONDS,
        )
        deadline = loop.time() + wait_seconds
        list_loaded_at: float | None = None

        async def open_from_click(trigger_page: Any, trigger: Any) -> bool:
            """Open a draft card whose editor is created in a new tab."""
            context = await self.get_context()
            try:
                known_page_ids = {id(existing) for existing in context.pages}
            except Exception:
                known_page_ids = set()

            await trigger.click(force=True)
            click_deadline = min(deadline, loop.time() + 10.0)
            while loop.time() < click_deadline:
                try:
                    pages = list(context.pages)
                except Exception:
                    pages = []
                new_pages = [
                    candidate_page
                    for candidate_page in pages
                    if id(candidate_page) not in known_page_ids
                ]

                for editor_page in [*reversed(new_pages), trigger_page]:
                    await self._raise_if_auth_required(editor_page)
                    if "login" in str(getattr(editor_page, "url", "")).lower():
                        raise RuntimeError(
                            "AUTH_REQUIRED: WeChat session expired while opening API-created draft"
                        )
                    with contextlib.suppress(Exception):
                        await editor_page.wait_for_load_state(
                            "domcontentloaded", timeout=1000
                        )
                    if await self._editor_is_ready(editor_page, timeout_seconds=1.0):
                        self._active_page = editor_page
                        return True

                await asyncio.sleep(
                    min(
                        API_DRAFT_LIST_POLL_INTERVAL_SECONDS,
                        click_deadline - loop.time(),
                    )
                )
            return False

        while True:
            now = loop.time()
            if (
                list_loaded_at is None
                or now - list_loaded_at >= API_DRAFT_LIST_REFRESH_INTERVAL_SECONDS
            ):
                await page.goto(
                    draft_list_url,
                    timeout=int(self.settings.navigation_timeout_seconds * 1000),
                    wait_until="domcontentloaded",
                )
                list_loaded_at = loop.time()
                remaining = deadline - list_loaded_at
                if remaining > 0:
                    # ``domcontentloaded`` fires before the WeChat list's
                    # asynchronous data request has populated the DOM.
                    await asyncio.sleep(min(1.0, remaining))

            if "login" in str(getattr(page, "url", "")).lower():
                raise RuntimeError(
                    "AUTH_REQUIRED: WeChat session expired while opening API-created draft"
                )

            await self._raise_if_auth_required(page)

            candidate_locators = [
                page.locator(".appmsg_item"),
                page.locator(".appmsg_title"),
                page.locator(".weui-desktop-list__item"),
                page.locator('a[href*="appmsg_edit"]'),
                page.get_by_text(title, exact=False),
            ]

            candidate_attempted = False
            for candidates in candidate_locators:
                try:
                    for index in range(await candidates.count()):
                        candidate = candidates.nth(index)
                        if not await candidate.is_visible():
                            continue
                        candidate_text = " ".join(
                            (await candidate.inner_text())
                            .replace("\u00a0", " ")
                            .split()
                        )
                        if normalized_title not in candidate_text:
                            continue

                        candidate_attempted = True
                        href = await candidate.get_attribute("href")
                        if href and not href.lower().startswith(("javascript:", "#")):
                            target_url = urljoin(MP_ORIGIN + "/", href)
                            target_parts = urlsplit(target_url)
                            if target_parts.netloc != "mp.weixin.qq.com":
                                continue
                            target_query = parse_qs(
                                target_parts.query, keep_blank_values=True
                            )
                            # The link may carry the list page's stale token.
                            # Always use the current authenticated session token.
                            target_query["token"] = [token]
                            target_url = urlunsplit(
                                (
                                    target_parts.scheme,
                                    target_parts.netloc,
                                    target_parts.path,
                                    urlencode(target_query, doseq=True),
                                    target_parts.fragment,
                                )
                            )
                            await page.goto(
                                target_url,
                                timeout=int(
                                    self.settings.navigation_timeout_seconds * 1000
                                ),
                                wait_until="domcontentloaded",
                            )
                            await self._raise_if_auth_required(page)
                            if await self._editor_is_ready(page):
                                logger.info("wechat_api_draft_opened_by_title")
                                return
                        else:
                            # In the current MP UI the title is a non-link
                            # span.  The pencil control is revealed on hover
                            # and opens the editor in a new page.
                            card = candidate.locator(
                                'xpath=ancestor::div[@id="appmsg_publish_record" or contains(@class, "publish_card_container")][1]'
                            )
                            with contextlib.suppress(Exception):
                                await card.hover()
                            edit_controls = card.locator(
                                'a.weui-desktop-icon20.weui-desktop-icon-btn:has(path[d^="M13 4"])'
                            )
                            edit_control = None
                            for edit_index in range(await edit_controls.count()):
                                possible_edit = edit_controls.nth(edit_index)
                                if await possible_edit.is_visible():
                                    edit_control = possible_edit
                                    break

                            if edit_control is not None:
                                opened = await open_from_click(page, edit_control)
                            else:
                                opened = await open_from_click(page, candidate)
                            if opened:
                                logger.info("wechat_api_draft_opened_by_title")
                                return

                            # Some list versions open a small action menu first.
                            edit_btn = page.get_by_text("编辑", exact=True)
                            if (
                                await edit_btn.count() > 0
                                and await edit_btn.first.is_visible()
                            ):
                                if await open_from_click(page, edit_btn.first):
                                    logger.info("wechat_api_draft_opened_by_title")
                                    return

                        # The candidate was found but the editor did not
                        # finish loading.  Return to the list before the next
                        # DOM poll instead of searching the editor page.
                        list_loaded_at = None
                        break

                except Exception as exc:
                    if "AUTH_REQUIRED" in str(exc):
                        raise
                    logger.debug(
                        "wechat_draft_candidate_failed", error=safe_error_summary(exc)
                    )

                if candidate_attempted:
                    break

            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(API_DRAFT_LIST_POLL_INTERVAL_SECONDS, remaining))

        raise RuntimeError(
            "DRAFT_OPEN_FAILED: API-created WeChat draft was not found in the "
            "browser draft list after waiting for synchronization"
        )

    async def open_draft(
        self, draft_url: str | None, *, job: PublishJob | None = None
    ) -> None:
        """Open a saved browser draft or an official-API draft."""
        page = await self.get_page()

        if self._is_api_draft_reference(draft_url, job):
            if job is None:
                raise RuntimeError(
                    "DRAFT_OPEN_FAILED: API draft job context is unavailable"
                )
            await self._open_api_draft(page, job)
            return

        if not draft_url:
            raise ValueError("DRAFT_OPEN_FAILED: Saved draft URL is unavailable")
        parts = urlsplit(draft_url)
        if parts.scheme != "https" or parts.netloc != "mp.weixin.qq.com":
            raise ValueError(
                f"DRAFT_OPEN_FAILED: Invalid draft URL domain: {draft_url}"
            )
        if page.url == draft_url:
            logger.info("wechat_reusing_open_draft_editor")
        else:
            await page.goto(
                draft_url,
                timeout=int(self.settings.navigation_timeout_seconds * 1000),
                wait_until="domcontentloaded",
            )
            await self._raise_if_auth_required(page)
            with contextlib.suppress(Exception):
                conflict_btn = page.locator(
                    "button:has-text('查看新草稿'), a:has-text('查看新草稿')"
                ).first
                if await conflict_btn.count() > 0 and await conflict_btn.is_visible():
                    logger.info("wechat_draft_conflict_detected_clicking_new_draft")
                    await conflict_btn.click()
                    await page.wait_for_load_state("domcontentloaded")
                    await asyncio.sleep(1.0)

        await self._raise_if_auth_required(page)
        if job is not None and not await self._editor_is_ready(page):
            raise RuntimeError(
                "DRAFT_OPEN_FAILED: WeChat draft editor did not expose a publish action"
            )

    @staticmethod
    async def _visible_button(
        page: Any,
        names: list[str],
        *,
        exclude_button: Any | None = None,
        exclude_classes: tuple[str, ...] = (),
    ) -> Any | None:
        """Return a visible matching action, optionally excluding an entry button.

        The editor keeps the initial ``button.mass_send`` element mounted after
        the first click.  Since the word ``发表`` is also used by confirmation
        controls, a global role/text lookup can otherwise return that same
        element and falsely advance the publish state machine.  Filter both by
        class and, when Playwright exposes element handles, by DOM identity.
        """

        excluded_class_names = set(exclude_classes)

        async def is_excluded(candidate: Any) -> bool:
            if exclude_button is None and not excluded_class_names:
                return False
            if exclude_button is not None and candidate is exclude_button:
                return True

            if excluded_class_names:
                with contextlib.suppress(Exception):
                    class_name = await candidate.get_attribute("class")
                    if set(str(class_name or "").split()) & excluded_class_names:
                        return True

            if exclude_button is None:
                return False

            candidate_handle = None
            excluded_handle = None
            try:
                candidate_handle = await candidate.element_handle()
                excluded_handle = await exclude_button.element_handle()
                if candidate_handle is None or excluded_handle is None:
                    return False
                return bool(
                    await candidate_handle.evaluate(
                        "(element, excluded) => element === excluded",
                        excluded_handle,
                    )
                )
            except Exception as exc:
                logger.debug("wechat_button_identity_check_failed", error=str(exc))
                return False
            finally:
                for handle in (candidate_handle, excluded_handle):
                    if handle is not None:
                        with contextlib.suppress(Exception):
                            await handle.dispose()

        async def first_visible(locator: Any) -> Any | None:
            try:
                count = await locator.count()
            except Exception as exc:
                logger.debug("wechat_button_count_failed", error=str(exc))
                return None

            for index in range(count):
                candidate = locator.first if index == 0 else locator.nth(index)
                try:
                    if not await candidate.is_visible():
                        continue
                except Exception as exc:
                    logger.debug(
                        "wechat_button_visibility_check_failed", error=str(exc)
                    )
                    continue
                if await is_excluded(candidate):
                    continue
                return candidate
            return None

        for name in names:
            button = page.locator(
                f'.weui-desktop-dialog button:text-is("{name}"), '
                f'.weui-desktop-dialog a[role="button"]:text-is("{name}"), '
                f'.weui-desktop-modal button:text-is("{name}"), '
                f'.weui-desktop-modal a[role="button"]:text-is("{name}"), '
                f'[role="dialog"] button:text-is("{name}"), '
                f'[role="dialog"] a[role="button"]:text-is("{name}")'
            )
            visible_button = await first_visible(button)
            if visible_button is not None:
                return visible_button

            button = page.get_by_role("button", name=name, exact=True)
            visible_button = await first_visible(button)
            if visible_button is not None:
                return visible_button

            button = page.locator(
                f'button:text-is("{name}"), '
                f'a[role="button"]:text-is("{name}"), '
                f'.weui-desktop-btn:text-is("{name}")'
            )
            visible_button = await first_visible(button)
            if visible_button is not None:
                return visible_button
        return None

    @staticmethod
    async def _disable_group_notification(page: Any) -> None:
        enabled_selectors = (
            'input[type="checkbox"]:checked',
            '[role="switch"][aria-checked="true"]',
            '[class*="switch"][class~="checked"]',
            '[class*="switch"][class*="is-checked"]',
            '[class*="switch"][class*="switch_on"]',
            '[class*="switch"][class*="switch--on"]',
            '[class*="switch"][class*="--on"]',
        )

        for label_name in GROUP_NOTIFICATION_LABEL_NAMES:
            label = page.get_by_text(label_name, exact=True)
            if await label.count() == 0 or not await label.first.is_visible():
                continue

            rows = [
                label.first.locator(
                    "xpath=ancestor::*[.//input[@type='checkbox'] or .//*[@role='switch'] "
                    "or .//*[contains(@class,'switch')]][1]"
                ),
                label.first.locator("xpath=.."),
                label.first,
            ]
            for row in rows:
                for selector in enabled_selectors:
                    enabled_switch = row.locator(selector).first
                    if await enabled_switch.count() == 0:
                        continue

                    # The current MP page positions the real checkbox input
                    # outside the viewport and renders a visible switch shell
                    # around it.  Playwright still reports the input as
                    # visible, but even a forced click fails while trying to
                    # scroll it into view.  Prefer the visible shell and keep
                    # a native DOM click as a fallback for hidden controls.
                    click_targets: list[Any] = []
                    if selector.startswith("input"):
                        with contextlib.suppress(Exception):
                            switch_shell = enabled_switch.locator(
                                "xpath=ancestor::*[self::label or @role='switch' "
                                "or contains(@class,'switch')][1]"
                            )
                            if await switch_shell.count() > 0:
                                click_targets.append(switch_shell.first)
                    click_targets.append(enabled_switch)

                    clicked = False
                    for click_target in click_targets:
                        try:
                            if not await click_target.is_visible():
                                continue
                            await click_target.click()
                            clicked = True
                            break
                        except Exception as exc:
                            logger.debug(
                                "wechat_group_notification_click_failed",
                                error=str(exc),
                            )

                    if not clicked:
                        try:
                            await enabled_switch.evaluate("element => element.click()")
                            clicked = True
                            logger.info(
                                "wechat_group_notification_native_click_used",
                                label=label_name,
                            )
                        except Exception as exc:
                            raise RuntimeError(
                                "PUBLISH_CONFIRM_FAILED: Could not disable WeChat "
                                "group notification"
                            ) from exc

                    for _ in range(10):
                        if await row.locator(selector).count() == 0:
                            logger.info(
                                "wechat_group_notification_disabled",
                                label=label_name,
                            )
                            return
                        await asyncio.sleep(0.1)

                    raise RuntimeError(
                        "PUBLISH_CONFIRM_FAILED: WeChat group notification "
                        "remained enabled after click"
                    )

            # The first text alias can be a static label while the actual
            # switch is exposed under the second alias in newer editor builds.
            # Keep looking rather than assuming the option was disabled.

    async def _handle_admin_verification(self, page: Any, job: PublishJob) -> bool:
        """Detect an admin verification prompt and notify without capturing it."""
        frames = getattr(page, "frames", [])
        verify_frame = None
        if isinstance(frames, list):
            for f in frames:
                f_url = getattr(f, "url", "")
                if any(k in f_url for k in ["safe", "qrcode", "verify"]):
                    verify_frame = f
                    break

        # Some MP editor versions render the verification view in the main
        # document (or an iframe URL without a stable keyword), so URL-only
        # detection misses the verification prompt. Inspect visible text as a fallback.
        if verify_frame is None and isinstance(frames, list):
            for frame in frames:
                try:
                    frame_text = await frame.locator("body").inner_text()
                except Exception as exc:
                    logger.debug(
                        "wechat_verification_frame_text_failed", error=str(exc)
                    )
                    continue
                if "微信验证" in frame_text and "扫码" in frame_text:
                    verify_frame = frame
                    break

        is_dialog_visible = False
        with contextlib.suppress(Exception):
            dialog_loc = page.locator(
                ".weui-desktop-dialog:has-text('微信验证'), "
                ".weui-desktop-dialog:has-text('管理员微信号')"
            )
            if (
                hasattr(dialog_loc, "count")
                and await dialog_loc.count() > 0
                and await dialog_loc.first.is_visible()
            ):
                dialog_text = await dialog_loc.first.inner_text()
                if "微信验证" in dialog_text or "管理员" in dialog_text:
                    is_dialog_visible = True

        # Newer pages use a portal wrapper instead of the legacy dialog class.
        # The visible body text is a reliable fallback when no stable wrapper
        # can be selected.
        if not is_dialog_visible and verify_frame is None:
            with contextlib.suppress(Exception):
                body_text = await page.inner_text("body")
                if "微信验证" in body_text and "扫码" in body_text:
                    is_dialog_visible = True

        if not is_dialog_visible and verify_frame is None:
            return False

        logger.info("wechat_admin_verification_required")
        article_title = (job.content or {}).get("title", "") or str(job.id)
        alert_title = "【微信公众号发布】需要人工确认"
        alert_content = (
            f"文章《{article_title}》已经提交发表，微信要求管理员人工确认。\n"
            "请在微信或公众号后台完成确认，系统会自动核对发表结果。\n"
            f"任务 ID：{job.id}\n控制台：{self.settings.console_public_url}"
        )
        from publisher.notify_alert import emit_notify_hub_alert

        await emit_notify_hub_alert(
            self.settings,
            event_type="publisher.wechat.manual_confirm",
            event_key=f"wechat_manual_confirm_{job.id}",
            title=alert_title,
            content=alert_content,
            level="warning",
            payload={"job_id": job.id, "platform": "wechat_mp"},
        )
        return True

    async def _wait_for_admin_verification(
        self,
        page: Any,
        job: PublishJob,
        *,
        timeout_seconds: float = 8.0,
    ) -> bool:
        """Allow the verification dialog time to appear after the final click."""
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            if await self._handle_admin_verification(page, job):
                return True
            await asyncio.sleep(0.5)
        return False

    async def publish_and_confirm(self, job: PublishJob) -> str | None:
        """Enter the publish flow and confirm the final publish action."""
        page = await self.get_page()
        await self.check_risk_control(page)

        mass_send_btn, entry_name = await self._find_publish_entry(page)
        if mass_send_btn is None:
            raise RuntimeError(
                "PUBLISH_NOT_STARTED: WeChat publish entry button not found"
            )

        loop = asyncio.get_running_loop()
        await mass_send_btn.click()
        await asyncio.sleep(0.5)
        next_step_clicked = entry_name == "下一步"
        direct_publish_entry = entry_name in ("发表", "群发")
        entry_click_count = 1
        entry_clicked_at = loop.time()
        ai_declaration_clicked = False
        publish_action_clicked = False
        publish_action_clicked_at = 0.0
        declaration_prompt_seen = False
        deadline = loop.time() + self.settings.operation_timeout_seconds

        while loop.time() < deadline:
            await self.check_risk_control(page)

            for validation_text in [
                "标题不能为空且长度不能超过64字",
                "必须插入一张图片",
            ]:
                validation_error = page.get_by_text(validation_text, exact=False)
                if (
                    await validation_error.count() > 0
                    and await validation_error.first.is_visible()
                ):
                    raise RuntimeError(
                        f"PUBLISH_CONFIRM_FAILED: Editor validation failed: {validation_text}"
                    )

            if await self._handle_admin_verification(page, job):
                return "waiting_manual_confirm"

            no_declaration_btn = await self._visible_button(
                page, NO_DECLARATION_BUTTON_NAMES
            )
            if no_declaration_btn is not None:
                await no_declaration_btn.click()
                if await self._wait_for_admin_verification(page, job):
                    return "waiting_manual_confirm"
                return

            declaration_prompt = page.get_by_text(DECLARATION_PROMPT_TEXT, exact=False)
            if (
                await declaration_prompt.count() > 0
                and await declaration_prompt.first.is_visible()
            ):
                declaration_prompt_seen = True

            if not ai_declaration_clicked:
                for option_name in ["AI 辅助生成", "AI 生成"]:
                    ai_option = page.get_by_text(option_name, exact=True)
                    if (
                        await ai_option.count() > 0
                        and await ai_option.first.is_visible()
                    ):
                        await ai_option.first.click()
                        ai_declaration_clicked = True
                        break

            if not publish_action_clicked:
                await self._disable_group_notification(page)
                confirm_btn = await self._visible_button(
                    page,
                    CONFIRM_BUTTON_NAMES,
                    exclude_button=mass_send_btn,
                    exclude_classes=("mass_send",),
                )
                if confirm_btn is not None:
                    await confirm_btn.click()
                    publish_action_clicked = True
                    publish_action_clicked_at = loop.time()
                    if await self._wait_for_admin_verification(page, job):
                        return "waiting_manual_confirm"
                    continue

            # An API-created draft can expose a direct ``发表`` entry instead
            # of a settings dialog.  WeChat may keep that same button mounted
            # for a short validation pass (the UI can show a non-fatal media
            # warning) before accepting the next physical click.  Give the
            # page time to transition, retry this entry at most once, and only
            # then treat the action as submitted if no modal is exposed.
            if (
                not publish_action_clicked
                and direct_publish_entry
                and not declaration_prompt_seen
                and loop.time() - entry_clicked_at >= PUBLISH_ENTRY_RETRY_DELAY_SECONDS
            ):
                entry_visible = False
                with contextlib.suppress(Exception):
                    entry_visible = await mass_send_btn.is_visible()

                if entry_visible and entry_click_count < 2:
                    await mass_send_btn.click(force=True)
                    entry_click_count += 1
                    entry_clicked_at = loop.time()
                    logger.info(
                        "wechat_publish_entry_retried_after_no_state_change",
                        entry_name=entry_name,
                    )
                    continue

                publish_action_clicked = True
                publish_action_clicked_at = loop.time()
                logger.info(
                    "wechat_publish_direct_entry_submitted_without_modal",
                    entry_name=entry_name,
                    entry_click_count=entry_click_count,
                )
                if await self._wait_for_admin_verification(page, job):
                    return "waiting_manual_confirm"
                continue

            # WeChat may show a second confirmation dialog after the publish
            # settings dialog (for example, “继续发表” after enabling group
            # notifications). Handle that visible follow-up exactly once
            # before considering the publish action complete.
            if publish_action_clicked:
                follow_up_btn = await self._visible_button(
                    page, ["继续发表", "继续群发", "确认", "确定"]
                )
                if follow_up_btn is not None:
                    await follow_up_btn.click()
                    publish_action_clicked_at = loop.time()
                    if await self._wait_for_admin_verification(page, job):
                        return "waiting_manual_confirm"
                    continue

            # Modern MP editor enters publish page via '下一步'
            if not next_step_clicked and not publish_action_clicked:
                next_btn = page.get_by_role("button", name="下一步", exact=True)
                if await next_btn.count() > 0 and await next_btn.first.is_visible():
                    await next_btn.first.click()
                    next_step_clicked = True
                    await asyncio.sleep(0.5)
                    continue

            if (
                publish_action_clicked
                and not declaration_prompt_seen
                and loop.time() - publish_action_clicked_at >= 2
            ):
                if await self._wait_for_admin_verification(page, job):
                    return "waiting_manual_confirm"
                # Older pages publish directly without the declaration prompt.
                return

            await asyncio.sleep(0.25)

        raise RuntimeError("PUBLISH_CONFIRM_FAILED: Final publish button not found")

    async def verify_published(
        self, job: PublishJob, start_time: datetime
    ) -> str | None:
        """Verify publication success and extract published article URL."""
        return await self.reconcile(
            job, start_time, max_seconds=PUBLISH_VERIFY_MAX_WAIT_SECONDS
        )

    async def reconcile(
        self,
        job: PublishJob,
        start_time: datetime,
        max_seconds: int = 120,
    ) -> str | None:
        """Check published list repeatedly up to max_seconds for title match."""
        del start_time
        page = await self.get_page()
        if hasattr(page, "is_closed") and page.is_closed():
            if self._context:
                pages = [p for p in self._context.pages if not p.is_closed()]
                page = pages[0] if pages else await self._context.new_page()
            self._active_page = page

        title = (job.content or {}).get("title", "")

        def normalize_title(value: object) -> str:
            """Normalize browser-rendered whitespace before comparing titles."""
            return " ".join(str(value or "").replace("\u00a0", " ").split())

        normalized_title = normalize_title(title)
        token = None
        if "token=" in page.url:
            token = parse_qs(urlsplit(page.url).query).get("token", [None])[0]

        if not token:
            with contextlib.suppress(Exception):
                await page.goto(
                    MP_HOME_URL,
                    timeout=int(self.settings.navigation_timeout_seconds * 1000),
                    wait_until="domcontentloaded",
                )
                if "token=" in page.url:
                    token = parse_qs(urlsplit(page.url).query).get("token", [None])[0]

        publish_list_url = (
            f"{MP_ORIGIN}/cgi-bin/appmsgpublish?sub=list&begin=0&count=10&token={token}&lang=zh_CN"
            if token
            else None
        )

        deadline = asyncio.get_event_loop().time() + max_seconds

        while asyncio.get_event_loop().time() < deadline:
            try:
                if publish_list_url:
                    await page.goto(
                        publish_list_url,
                        timeout=int(self.settings.navigation_timeout_seconds * 1000),
                        wait_until="domcontentloaded",
                    )
                    await asyncio.sleep(1.5)
                else:
                    published_tab = page.locator(
                        'a:has-text("已发表"), :has-text("已发表")'
                    )
                    if (
                        await published_tab.count() > 0
                        and await published_tab.first.is_visible()
                    ):
                        await published_tab.first.click()
                        await asyncio.sleep(1.0)

                with contextlib.suppress(Exception):
                    links = await page.eval_on_selector_all(
                        "a[href*='/s/']",
                        "els => els.map(e => ({text: e.innerText.trim(), href: e.href}))",
                    )
                    for item_info in links:
                        item_text = normalize_title(item_info.get("text", ""))
                        item_href = item_info.get("href", "")
                        if (
                            normalized_title
                            and item_text
                            and normalized_title in item_text
                        ):
                            if item_href.startswith("/s/"):
                                return f"{MP_ORIGIN}{item_href}"
                            if item_href.startswith("https://mp.weixin.qq.com/s/"):
                                return item_href

                item = page.locator(
                    f'.weui-desktop-mass__item:has-text("{title}"), '
                    f'.publish_item:has-text("{title}"), '
                    f'a:has-text("{title}")'
                )
                if await item.count() > 0:
                    link = item.first.locator(
                        'a[href*="/s/"], a[href*="mp.weixin.qq.com/s"]'
                    )
                    if await link.count() > 0:
                        href = await link.first.get_attribute("href")
                        if href:
                            full_url = str(href)
                            if full_url.startswith("/s/"):
                                full_url = f"https://mp.weixin.qq.com{full_url}"
                            if full_url.startswith("https://mp.weixin.qq.com/s/"):
                                return full_url
            except Exception as exc:
                logger.debug("reconcile_check_error", error=str(exc))
            await asyncio.sleep(5.0)
        return None
