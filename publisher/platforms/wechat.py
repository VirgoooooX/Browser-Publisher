"""Playwright automation adapter for the WeChat Official Account web management UI."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import structlog

from publisher.config import PublisherSettings
from publisher.models import PublishJob
from publisher.platforms.base import BasePlatformPublisher
from publisher.platforms.render import render_wechat_html

logger = structlog.get_logger()

# Selectors and constants
MP_ORIGIN = "https://mp.weixin.qq.com"
MP_HOME_URL = f"{MP_ORIGIN}/"
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


class WeChatPublisher(BasePlatformPublisher):
    """Automates article draft creation, cover selection, and publication on WeChat MP."""

    def __init__(
        self, settings: PublisherSettings, browser_manager: Any = None
    ) -> None:
        super().__init__(settings)
        self._browser_manager = browser_manager
        self._playwright: Any = None
        self._context: Any = None
        self._active_page: Any = None

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
        if self._browser_manager is not None:
            return

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

    async def get_page(self) -> Any:
        if self._browser_manager is not None:
            return await self._browser_manager.get_page()

        if not self._context:
            raise RuntimeError("Playwright browser context is not initialized")
        pages = self._context.pages
        self._active_page = pages[0] if pages else await self._context.new_page()
        return self._active_page

    async def check_login(self) -> bool:
        """Navigate to MP home and check if session is authenticated."""
        page = await self.get_page()
        try:
            if MP_ORIGIN not in page.url:
                await page.goto(
                    MP_HOME_URL,
                    timeout=int(self.settings.navigation_timeout_seconds * 1000),
                    wait_until="domcontentloaded",
                )
            url = page.url
            if "/cgi-bin/" in url and "login" not in url:
                return True

            account_info = page.locator(".weui-desktop-account__info")
            if await account_info.count() > 0 and await account_info.first.is_visible():
                return True

            new_create = page.get_by_text("新的创作", exact=False)
            if await new_create.count() > 0 and await new_create.first.is_visible():
                return True

            draft_hint = page.get_by_text("近期草稿", exact=False)
            if await draft_hint.count() > 0 and await draft_hint.first.is_visible():
                return True
        except Exception as exc:
            logger.debug("wechat_check_login_failed", error=str(exc))
        return False

    async def capture_qr(self) -> str | None:
        """Locate and capture the login QR code image snippet as base64 PNG."""
        page = await self.get_page()
        # If already on home dashboard, there is no QR code
        if "/cgi-bin/" in page.url and "login" not in page.url:
            return None

        try:
            if MP_ORIGIN not in page.url or "login" not in page.url:
                await page.goto(
                    MP_HOME_URL,
                    timeout=int(self.settings.navigation_timeout_seconds * 1000),
                    wait_until="domcontentloaded",
                )
        except Exception as exc:
            logger.debug("wechat_goto_login_failed", error=str(exc))

        # Re-check if it redirected to authenticated home
        if "/cgi-bin/" in page.url and "login" not in page.url:
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
        """Clear cookies for WeChat domain only and force navigation away from authenticated state."""
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

    async def select_cover_from_content(self, page: Any) -> None:
        """Select first body image as article cover via '从正文选择'."""
        cover_btn = page.locator(
            ".select-cover__btn, #js_cover_area, .js_cover_btn_area, #js_cover_null"
        ).first
        if await cover_btn.count() == 0:
            logger.warning("cover_selection_button_not_found_skipping")
            return
        try:
            await cover_btn.click()
            await asyncio.sleep(1.0)
        except Exception as exc:
            logger.warning("click_cover_btn_failed", error=str(exc))
            return

        from_content_tab = page.locator(':has-text("从正文选择")').last
        if (
            await from_content_tab.count() == 0
            or not await from_content_tab.is_visible()
        ):
            logger.warning("from_content_tab_not_found_skipping")
            return
        try:
            await from_content_tab.click()
            await asyncio.sleep(1.5)
        except Exception as exc:
            logger.warning("click_from_content_tab_failed", error=str(exc))
            return

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
            return
        await img_pick.click()
        await asyncio.sleep(0.5)

        next_btn = page.locator(
            'button:has-text("下一步"):not(.weui-desktop-btn_disabled)'
        ).first
        if await next_btn.count() == 0 or not await next_btn.is_visible():
            logger.warning("cover_next_button_not_found_skipping")
            return
        await next_btn.click()
        await asyncio.sleep(1.0)

        dialog_confirmed = False
        crop_dialog = page.locator(
            '.weui-desktop-dialog:has-text("编辑封面"), .weui-desktop-dialog'
        ).first
        for btn_name in ["确认", "完成", "确定"]:
            confirm_btn = page.locator(
                f'.weui-desktop-dialog:has-text("编辑封面") button:has-text("{btn_name}"), '
                f'.weui-desktop-dialog:not([style*="display: none"]) button:has-text("{btn_name}")'
            ).first
            if await confirm_btn.count() > 0 and await confirm_btn.is_visible():
                await confirm_btn.click()
                dialog_confirmed = True
                try:
                    await crop_dialog.wait_for(state="hidden", timeout=5000)
                except Exception as exc:
                    logger.debug("crop_dialog_hide_wait_timeout", error=str(exc))
                break

        if not dialog_confirmed:
            logger.warning("cover_confirmation_button_not_found")
            return

        preview = page.locator(
            ".js_cover_preview_new, .js_cover_preview, "
            ".appmsg_cover_preview, .setting-group__cover_primary"
        )
        try:
            await preview.first.wait_for(state="visible", timeout=5000)
        except Exception as exc:
            logger.debug("cover_preview_wait_ignored", error=str(exc))

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
    ) -> tuple[str, str | None]:
        """Fill article, optionally pick cover, and save draft."""
        page = await self.get_page()
        editor_page = await self.open_editor(page)

        try:
            await self.fill_article(editor_page, job)
            await self.select_cover_from_content(editor_page)

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

            return draft_url, draft_media_id
        finally:
            if editor_page != page:
                with contextlib.suppress(Exception):
                    await editor_page.close()

    async def open_draft(self, draft_url: str) -> None:
        """Open previously saved draft by URL with domain validation."""
        parts = urlsplit(draft_url)
        if parts.scheme != "https" or parts.netloc != "mp.weixin.qq.com":
            raise ValueError(
                f"DRAFT_SAVE_FAILED: Invalid draft URL domain: {draft_url}"
            )
        page = await self.get_page()
        await page.goto(
            draft_url,
            timeout=int(self.settings.navigation_timeout_seconds * 1000),
            wait_until="domcontentloaded",
        )

    @staticmethod
    async def _visible_button(page: Any, names: list[str]) -> Any | None:
        for name in names:
            button = page.locator(
                f'.weui-desktop-dialog button:text-is("{name}"), '
                f'.weui-desktop-dialog a[role="button"]:text-is("{name}"), '
                f'.weui-desktop-modal button:text-is("{name}"), '
                f'.weui-desktop-modal a[role="button"]:text-is("{name}"), '
                f'[role="dialog"] button:text-is("{name}"), '
                f'[role="dialog"] a[role="button"]:text-is("{name}")'
            )
            if await button.count() > 0 and await button.first.is_visible():
                return button.first

            button = page.get_by_role("button", name=name, exact=True)
            if await button.count() > 0 and await button.first.is_visible():
                return button.first

            button = page.locator(
                f'button:text-is("{name}"), '
                f'a[role="button"]:text-is("{name}"), '
                f'.weui-desktop-btn:text-is("{name}")'
            )
            if await button.count() > 0 and await button.first.is_visible():
                return button.first
        return None

    @staticmethod
    async def _disable_group_notification(page: Any) -> None:
        label = page.get_by_text("群发通知", exact=True)
        if await label.count() == 0 or not await label.first.is_visible():
            return

        row = label.first.locator(
            "xpath=ancestor::*[.//input[@type='checkbox'] or .//*[@role='switch'] "
            "or .//*[contains(@class,'switch')]][1]"
        )
        enabled_switch = row.locator(
            'input[type="checkbox"]:checked, '
            '[role="switch"][aria-checked="true"], '
            '[class*="switch"][class~="checked"], '
            '[class*="switch"][class*="is-checked"], '
            '[class*="switch"][class*="switch_on"], '
            '[class*="switch"][class*="switch--on"]'
        ).first
        if await enabled_switch.count() > 0:
            await enabled_switch.click(force=True)
            logger.info("wechat_group_notification_disabled")

    async def publish_and_confirm(self, job: PublishJob) -> None:
        """Enter the publish flow and confirm the final publish action."""
        page = await self.get_page()
        await self.check_risk_control(page)

        mass_send_btn = page.locator("button.mass_send")
        entry_name = ""
        if (
            await mass_send_btn.count() == 0
            or not await mass_send_btn.first.is_visible()
        ):
            for name in ["下一步", "发表", "群发"]:
                candidate = page.get_by_role("button", name=name, exact=True)
                if await candidate.count() > 0 and await candidate.first.is_visible():
                    mass_send_btn = candidate
                    entry_name = name
                    break
        if (
            await mass_send_btn.count() == 0
            or not await mass_send_btn.first.is_visible()
        ):
            raise RuntimeError(
                "PUBLISH_CONFIRM_FAILED: Publish / mass_send button not found"
            )

        if not entry_name:
            try:
                entry_name = (await mass_send_btn.first.inner_text()).strip()
            except Exception:
                entry_name = ""

        await mass_send_btn.first.click()
        await asyncio.sleep(0.5)
        next_step_clicked = entry_name == "下一步"
        ai_declaration_clicked = False
        publish_action_clicked = False
        publish_action_clicked_at = 0.0
        declaration_prompt_seen = False
        deadline = (
            asyncio.get_running_loop().time() + self.settings.operation_timeout_seconds
        )

        while asyncio.get_running_loop().time() < deadline:
            await self.check_risk_control(page)

            no_declaration_btn = await self._visible_button(
                page, NO_DECLARATION_BUTTON_NAMES
            )
            if no_declaration_btn is not None:
                await no_declaration_btn.click()
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
                confirm_btn = await self._visible_button(page, CONFIRM_BUTTON_NAMES)
                if confirm_btn is not None:
                    await confirm_btn.click()
                    publish_action_clicked = True
                    publish_action_clicked_at = asyncio.get_running_loop().time()
                    await asyncio.sleep(0.5)
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
                and asyncio.get_running_loop().time() - publish_action_clicked_at >= 2
            ):
                # Older pages publish directly without the declaration prompt.
                return

            await asyncio.sleep(0.25)

        raise RuntimeError("PUBLISH_CONFIRM_FAILED: Final publish button not found")

    async def verify_published(
        self, job: PublishJob, start_time: datetime
    ) -> str | None:
        """Verify publication success and extract published article URL."""
        return await self.reconcile(job, start_time, max_seconds=30)

    async def reconcile(
        self,
        job: PublishJob,
        start_time: datetime,
        max_seconds: int = 120,
    ) -> str | None:
        """Check published list repeatedly up to max_seconds for title match."""
        del start_time
        page = await self.get_page()
        title = (job.content or {}).get("title", "")
        deadline = asyncio.get_event_loop().time() + max_seconds

        while asyncio.get_event_loop().time() < deadline:
            try:
                published_tab = page.locator(
                    'a:has-text("已发表"), :has-text("已发表")'
                )
                if (
                    await published_tab.count() > 0
                    and await published_tab.first.is_visible()
                ):
                    await published_tab.first.click()
                    await asyncio.sleep(1.0)

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
