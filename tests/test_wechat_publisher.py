"""Tests for WeChat MP platform publisher adapter and HTML rendering."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from pydantic import SecretStr

from publisher.config import PublisherSettings
from publisher.models import PublishJob, utc_now
from publisher.notify_alert import emit_notify_hub_alert
from publisher.platforms.render import render_wechat_html
from publisher.platforms.wechat import CONFIRM_BUTTON_NAMES, WeChatPublisher


def test_render_wechat_html_strips_duplicate_h1_and_cleans_quotes() -> None:
    raw_markdown = """# 这是文章标题

> 这是第一行引用
> 这是第二行引用

正文第一段说明。

- 列表项一
- 列表项二
"""
    html = render_wechat_html(
        content=raw_markdown,
        source_url="https://x.com/user/status/123",
        strip_title_heading=True,
    )

    # 1. First H1 should be stripped so it does not duplicate the article title
    assert "这是文章标题" not in html

    # 2. Blockquote should be rendered as HTML without raw '>' characters
    assert "<blockquote" in html
    assert "&gt;" not in html
    assert "> 这是第一行引用" not in html
    assert "这是第一行引用" in html

    # 3. List should be rendered
    assert "<ul" in html
    assert "<li" in html
    assert "列表项一" in html

    # 4. Source URL should be appended
    assert "原文链接：" in html
    assert "https://x.com/user/status/123" in html


def test_render_wechat_html_standalone_quote_arrow() -> None:
    raw_markdown = """> 第一段引用
>
> 第二段引用包含单独一行的箭头

正文内容。
"""
    html = render_wechat_html(
        content=raw_markdown,
        strip_title_heading=True,
    )

    assert "<blockquote" in html
    assert "&gt;" not in html
    assert "第一段引用" in html
    assert "第二段引用包含单独一行的箭头" in html


def test_render_wechat_html_renders_markdown_images() -> None:
    html = render_wechat_html(
        content="正文前\n\n![内图](https://mmbiz.qpic.cn/inline/1)\n\n正文后",
        strip_title_heading=True,
    )

    assert "![内图]" not in html
    assert '<img src="https://mmbiz.qpic.cn/inline/1"' in html
    assert "正文前" in html
    assert "正文后" in html


def test_render_wechat_html_supports_markdown_image_titles_and_escaped_urls() -> None:
    html = render_wechat_html(
        content='![内图](<https://img.example.com/a.png?x=1&y=2> "图片标题")',
        strip_title_heading=True,
    )

    assert '<img src="https://img.example.com/a.png?x=1&amp;y=2"' in html
    assert 'alt="内图"' in html
    assert "&amp;amp;" not in html


@pytest.mark.asyncio
async def test_wechat_reconcile_requires_full_nonempty_title_match(
    test_settings: PublisherSettings,
) -> None:
    publisher = WeChatPublisher(test_settings)
    page = MagicMock()
    page.url = "https://mp.weixin.qq.com/cgi-bin/home?t=home/index&token=test-token"
    page.is_closed.return_value = False
    page.goto = AsyncMock()
    page.eval_on_selector_all = AsyncMock(
        return_value=[
            {"text": "", "href": "https://mp.weixin.qq.com/s/empty-old"},
            {"text": "额度重置", "href": "https://mp.weixin.qq.com/s/short-old"},
            {
                "text": "额度重置已全量生效 · 已发表",
                "href": "https://mp.weixin.qq.com/s/current",
            },
        ]
    )
    publisher.get_page = AsyncMock(return_value=page)  # type: ignore[method-assign]
    job = PublishJob(
        id="job_reconcile_exact_title",
        platform="wechat_mp",
        mode="publish",
        content={"title": "额度重置已全量生效", "body_text": "正文"},
    )

    with patch("publisher.platforms.wechat.asyncio.sleep", new_callable=AsyncMock):
        published_url = await publisher.reconcile(job, utc_now(), max_seconds=1)

    assert published_url == "https://mp.weixin.qq.com/s/current"


def test_wechat_cover_uses_notify_hub_internal_origin(
    test_settings: PublisherSettings,
) -> None:
    test_settings.notify_event_url = "http://notify-hub:8000/api/v1/events"
    publisher = WeChatPublisher(test_settings)
    job = PublishJob(
        platform="wechat_mp",
        mode="publish",
        content={"title": "测试"},
        media=[
            {
                "kind": "url",
                "url": "https://notify.example.test:37891/codex_wechat_cover.png",
            }
        ],
    )

    assert (
        publisher._cover_url_for_editor(job)
        == "http://notify-hub:8000/codex_wechat_cover.png"
    )


@pytest.mark.asyncio
async def test_wechat_downloads_cover_from_notify_hub_internal_origin(
    test_settings: PublisherSettings,
) -> None:
    test_settings.notify_event_url = "http://notify-hub:8000/api/v1/events"
    publisher = WeChatPublisher(test_settings)
    job = PublishJob(
        id="job_cover_test",
        platform="wechat_mp",
        mode="publish",
        content={"title": "测试"},
        media=[
            {
                "kind": "url",
                "url": "https://notify.example.test:37891/codex_wechat_cover.png",
            }
        ],
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://notify-hub:8000/codex_wechat_cover.png"
        return httpx.Response(
            200,
            headers={"content-type": "image/png"},
            content=b"png-image",
        )

    transport = httpx.MockTransport(handler)
    with patch(
        "publisher.platforms.wechat.httpx.AsyncClient",
        return_value=httpx.AsyncClient(transport=transport),
    ):
        cover_path = await publisher._download_cover_for_upload(job)

    assert cover_path is not None
    assert cover_path.read_bytes() == b"png-image"


@pytest.mark.asyncio
async def test_wechat_draft_requires_uploaded_cover(
    test_settings: PublisherSettings,
) -> None:
    publisher = WeChatPublisher(test_settings)
    page = MagicMock()
    publisher.get_page = AsyncMock(return_value=page)  # type: ignore[method-assign]
    publisher.open_editor = AsyncMock(return_value=page)  # type: ignore[method-assign]
    publisher.fill_article = AsyncMock()  # type: ignore[method-assign]
    publisher._insert_cover_image = AsyncMock(  # type: ignore[method-assign]
        return_value=False
    )
    publisher.select_cover_from_content = AsyncMock(  # type: ignore[method-assign]
        return_value=False
    )
    test_settings.ensure_directories()
    cover_path = test_settings.media_dir / "cover.png"
    cover_path.write_bytes(b"image")
    job = PublishJob(
        platform="wechat_mp",
        mode="draft",
        content={"title": "测试", "body_text": "正文"},
        media=[],
    )

    with pytest.raises(RuntimeError, match="COVER_FAILED"):
        await publisher.save_draft(job, [cover_path])
    publisher._insert_cover_image.assert_awaited_once_with(page, cover_path)
    publisher.select_cover_from_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_wechat_legacy_mode_rejects_api_media_marker(
    test_settings: PublisherSettings,
) -> None:
    publisher = WeChatPublisher(test_settings)
    job = PublishJob(
        platform="wechat_mp",
        mode="draft",
        content={
            "title": "marker",
            "body_text": "正文",
            "body_html": '<p><img src="publisher-media://med_inline_1"></p>',
        },
        media=[],
    )

    with pytest.raises(RuntimeError, match="official API credentials"):
        await publisher.save_draft(job, [])


@pytest.mark.asyncio
async def test_wechat_check_login_success(test_settings: PublisherSettings) -> None:
    publisher = WeChatPublisher(test_settings)
    mock_page = MagicMock()
    mock_page.url = "https://mp.weixin.qq.com/cgi-bin/home?t=home/index"
    empty = MagicMock()
    empty.count = AsyncMock(return_value=0)
    account = MagicMock()
    account.count = AsyncMock(return_value=1)
    account.first.is_visible = AsyncMock(return_value=True)
    mock_page.get_by_text.return_value = empty
    mock_page.locator.side_effect = lambda selector: (
        account if selector == ".weui-desktop-account__info" else empty
    )

    with patch.object(publisher, "get_page", new_callable=AsyncMock) as mock_get_page:
        mock_get_page.return_value = mock_page
        logged_in = await publisher.check_login()
        assert logged_in is True


@pytest.mark.asyncio
async def test_wechat_check_login_failure(test_settings: PublisherSettings) -> None:
    publisher = WeChatPublisher(test_settings)
    mock_page = MagicMock()
    mock_page.url = "https://mp.weixin.qq.com/"
    mock_locator = AsyncMock()
    mock_locator.count.return_value = 0
    mock_page.locator.return_value = mock_locator
    mock_page.get_by_text.return_value = mock_locator

    with patch.object(publisher, "get_page", new_callable=AsyncMock) as mock_get_page:
        mock_get_page.return_value = mock_page
        logged_in = await publisher.check_login()
        assert logged_in is False


@pytest.mark.asyncio
async def test_wechat_check_risk_control_rate_limit(
    test_settings: PublisherSettings,
) -> None:
    publisher = WeChatPublisher(test_settings)
    mock_page = MagicMock()

    # Rate limit dialog visible
    rate_loc = AsyncMock()
    rate_loc.count.return_value = 1
    rate_loc.first.is_visible.return_value = True
    mock_page.locator.return_value = rate_loc

    with pytest.raises(RuntimeError, match="RATE_LIMIT_TRIGGERED"):
        await publisher.check_risk_control(mock_page)


@pytest.mark.asyncio
async def test_wechat_check_risk_control_security_check(
    test_settings: PublisherSettings,
) -> None:
    publisher = WeChatPublisher(test_settings)
    mock_page = MagicMock()

    # Rate limit dialog not visible
    empty_loc = AsyncMock()
    empty_loc.count.return_value = 0

    # Captcha iframe visible
    captcha_loc = AsyncMock()
    captcha_loc.count.return_value = 1
    captcha_loc.first.is_visible.return_value = True

    def locator_side_effect(selector: str) -> AsyncMock:
        if "tcaptcha" in selector or "安全验证" in selector:
            return captcha_loc
        return empty_loc

    mock_page.locator.side_effect = locator_side_effect

    with pytest.raises(RuntimeError, match="SECURITY_CHECK_TRIGGERED"):
        await publisher.check_risk_control(mock_page)


@pytest.mark.asyncio
async def test_wechat_open_editor_with_shared_browser_manager(
    test_settings: PublisherSettings,
) -> None:
    """Verify open_editor gets context from BrowserManager without AttributeError."""
    mock_browser_manager = MagicMock()
    mock_context = MagicMock()
    mock_browser_manager.get_context = AsyncMock(return_value=mock_context)

    publisher = WeChatPublisher(test_settings, browser_manager=mock_browser_manager)

    mock_editor_page = MagicMock()
    mock_editor_page.wait_for_load_state = AsyncMock()

    class AsyncPageContextManager:
        async def __aenter__(self) -> Any:
            fut: asyncio.Future[Any] = asyncio.Future()
            fut.set_result(mock_editor_page)
            val = MagicMock()
            val.value = fut
            return val

        async def __aexit__(self, *args: Any) -> None:
            pass

    mock_context.expect_page.return_value = AsyncPageContextManager()

    mock_home_page = MagicMock()
    mock_home_page.url = "https://mp.weixin.qq.com/cgi-bin/home?t=home/index"
    article_btn = AsyncMock()
    article_btn.count.return_value = 1
    article_btn.first.is_visible.return_value = True
    article_btn.first.click = AsyncMock()
    mock_home_page.locator.return_value = article_btn

    editor_page = await publisher.open_editor(mock_home_page)
    assert editor_page == mock_editor_page
    assert publisher._active_page == mock_editor_page
    mock_context.expect_page.assert_called_once()


@pytest.mark.asyncio
async def test_wechat_open_draft_reuses_current_editor(
    test_settings: PublisherSettings,
) -> None:
    publisher = WeChatPublisher(test_settings)
    draft_url = "https://mp.weixin.qq.com/cgi-bin/appmsg?action=edit&isNew=1"
    page = MagicMock()
    page.url = draft_url
    page.goto = AsyncMock()
    publisher.get_page = AsyncMock(return_value=page)  # type: ignore[method-assign]

    publisher._page_requires_login = AsyncMock(return_value=False)
    await publisher.open_draft(draft_url)

    page.goto.assert_not_awaited()


@pytest.mark.asyncio
async def test_wechat_api_draft_uses_platform_id_and_title_lookup(
    test_settings: PublisherSettings,
) -> None:
    publisher = WeChatPublisher(test_settings)
    page = MagicMock()
    page.url = "https://mp.weixin.qq.com/cgi-bin/home?t=home/index&token=token-1"
    publisher.get_page = AsyncMock(return_value=page)  # type: ignore[method-assign]
    publisher._open_api_draft = AsyncMock()  # type: ignore[method-assign]
    job = PublishJob(
        id="job_api_draft",
        platform="wechat_mp",
        mode="publish",
        platform_draft_id="api-media-id-opaque-001",
        content={"title": "API 草稿标题", "body_text": "正文"},
    )

    await publisher.open_draft(None, job=job)

    publisher._open_api_draft.assert_awaited_once_with(page, job)


@pytest.mark.asyncio
async def test_wechat_api_draft_opens_matching_title_and_adds_editor_token(
    test_settings: PublisherSettings,
) -> None:
    publisher = WeChatPublisher(test_settings)
    publisher._ensure_page_token = AsyncMock(return_value="token-1")
    page = MagicMock()
    page.url = "https://mp.weixin.qq.com/cgi-bin/home?t=home/index&token=token-1"
    page.goto = AsyncMock(side_effect=lambda url, **_: setattr(page, "url", url))

    def collection(items: list[Any]) -> MagicMock:
        loc = MagicMock()
        loc.count = AsyncMock(return_value=len(items))
        loc.first = items[0] if items else MagicMock()
        loc.nth.side_effect = lambda index: items[index]
        return loc

    candidate = MagicMock()
    candidate.is_visible = AsyncMock(return_value=True)
    candidate.inner_text = AsyncMock(return_value="API 草稿标题")
    candidate.get_attribute = AsyncMock(
        return_value="/cgi-bin/appmsg?t=media/appmsg_edit_v2&action=edit"
    )

    entry = MagicMock()
    entry.is_visible = AsyncMock(return_value=True)
    entry.inner_text = AsyncMock(return_value="发表")

    def locator(selector: str) -> MagicMock:
        if selector == ".appmsg_item":
            return collection([candidate])
        if "mass_send" in selector:
            return collection([entry])
        return collection([])

    page.locator.side_effect = locator
    page.get_by_role.return_value = collection([])
    page.get_by_text.return_value = collection([])

    job = PublishJob(
        id="job_api_draft_lookup",
        platform="wechat_mp",
        mode="publish",
        platform_draft_id="api-media-id-opaque-002",
        content={"title": "API 草稿标题", "body_text": "正文"},
    )

    await publisher._open_api_draft(page, job)

    assert page.goto.await_count == 2
    assert "type=77" in page.goto.await_args_list[0].args[0]
    assert "action=list_card" in page.goto.await_args_list[0].args[0]
    assert "action=edit" in page.goto.await_args_list[-1].args[0]
    assert "token=token-1" in page.goto.await_args_list[-1].args[0]


@pytest.mark.asyncio
async def test_wechat_api_draft_opens_hover_edit_control_in_new_page(
    test_settings: PublisherSettings,
) -> None:
    publisher = WeChatPublisher(test_settings)
    publisher._ensure_page_token = AsyncMock(return_value="token-1")
    page = MagicMock()
    page.url = "https://mp.weixin.qq.com/cgi-bin/home?t=home/index&token=token-1"
    page.goto = AsyncMock(side_effect=lambda url, **_: setattr(page, "url", url))

    def collection(items: list[Any]) -> MagicMock:
        loc = MagicMock()
        loc.count = AsyncMock(return_value=len(items))
        loc.first = items[0] if items else MagicMock()
        loc.nth.side_effect = lambda index: items[index]
        return loc

    editor_entry = MagicMock()
    editor_entry.is_visible = AsyncMock(return_value=True)
    editor_entry.inner_text = AsyncMock(return_value="发表")

    editor_page = MagicMock()
    editor_page.url = "https://mp.weixin.qq.com/cgi-bin/appmsg?action=edit"
    editor_page.wait_for_load_state = AsyncMock()
    editor_page.locator.side_effect = lambda selector: (
        collection([editor_entry]) if "mass_send" in selector else collection([])
    )
    editor_page.get_by_role.return_value = collection([])
    editor_page.get_by_text.return_value = collection([])

    class FakeContext:
        def __init__(self) -> None:
            self.pages = [page]

    context = FakeContext()
    publisher._context = context

    edit_control = MagicMock()
    edit_control.is_visible = AsyncMock(return_value=True)
    edit_control.click = AsyncMock(
        side_effect=lambda **_: context.pages.append(editor_page)
    )
    card = MagicMock()
    card.hover = AsyncMock()
    card.locator.return_value = collection([edit_control])

    candidate = MagicMock()
    candidate.is_visible = AsyncMock(return_value=True)
    candidate.inner_text = AsyncMock(return_value="悬停打开的草稿标题")
    candidate.get_attribute = AsyncMock(return_value="javascript:void(0);")
    candidate.locator.return_value = card

    empty = collection([])

    def locator(selector: str) -> MagicMock:
        return collection([candidate]) if selector == ".appmsg_item" else empty

    page.locator.side_effect = locator
    page.get_by_role.return_value = empty
    page.get_by_text.side_effect = lambda name, **_: (
        collection([candidate]) if name == "悬停打开的草稿标题" else empty
    )

    job = PublishJob(
        id="job_api_draft_new_page",
        platform="wechat_mp",
        mode="publish",
        platform_draft_id="api-media-id-opaque-004",
        content={"title": "悬停打开的草稿标题", "body_text": "正文"},
    )

    await publisher._open_api_draft(page, job)

    assert publisher._active_page is editor_page
    assert page.goto.await_count == 1
    card.hover.assert_awaited_once()
    edit_control.click.assert_awaited_once_with(force=True)


@pytest.mark.asyncio
async def test_wechat_api_draft_waits_for_async_list_hydration(
    test_settings: PublisherSettings,
) -> None:
    publisher = WeChatPublisher(test_settings)
    publisher._ensure_page_token = AsyncMock(return_value="token-1")
    page = MagicMock()
    page.url = "https://mp.weixin.qq.com/cgi-bin/home?t=home/index&token=token-1"

    hydration_polls = 0

    def goto(url: str, **_: Any) -> None:
        nonlocal hydration_polls
        page.url = url
        if "action=list" in url:
            hydration_polls = 0

    page.goto = AsyncMock(side_effect=goto)

    candidate = MagicMock()
    candidate.is_visible = AsyncMock(return_value=True)
    candidate.inner_text = AsyncMock(return_value="异步加载的草稿标题")
    candidate.get_attribute = AsyncMock(
        return_value="/cgi-bin/appmsg?t=media/appmsg_edit_v2&action=edit"
    )

    entry = MagicMock()
    entry.is_visible = AsyncMock(return_value=True)
    entry.inner_text = AsyncMock(return_value="发表")

    def collection(items: list[Any], count: AsyncMock | None = None) -> MagicMock:
        loc = MagicMock()
        loc.count = count or AsyncMock(return_value=len(items))
        loc.first = items[0] if items else MagicMock()
        loc.nth.side_effect = lambda index: items[index]
        return loc

    async_list = collection(
        [candidate],
        AsyncMock(side_effect=lambda: 1 if hydration_polls >= 2 else 0),
    )
    empty = collection([])
    editor_entry = collection([entry])

    def locator(selector: str) -> MagicMock:
        if selector == ".appmsg_item":
            return async_list
        if "mass_send" in selector:
            return editor_entry
        return empty

    page.locator.side_effect = locator
    page.get_by_role.return_value = empty
    page.get_by_text.return_value = empty

    class FakeLoop:
        current_time = 0.0

        def time(self) -> float:
            return self.current_time

    fake_loop = FakeLoop()

    async def advance_time(delay: float) -> None:
        fake_loop.current_time += delay
        nonlocal hydration_polls
        hydration_polls += 1

    job = PublishJob(
        id="job_api_draft_async_list",
        platform="wechat_mp",
        mode="publish",
        platform_draft_id="api-media-id-opaque-003",
        content={"title": "异步加载的草稿标题", "body_text": "正文"},
    )

    with (
        patch(
            "publisher.platforms.wechat.asyncio.get_running_loop",
            return_value=fake_loop,
        ),
        patch("publisher.platforms.wechat.asyncio.sleep", side_effect=advance_time),
    ):
        await publisher._open_api_draft(page, job)

    # The list must remain mounted while its async data request hydrates it;
    # only the final editor navigation should be the second page.goto call.
    assert page.goto.await_count == 2
    assert "action=edit" in page.goto.await_args_list[-1].args[0]


@pytest.mark.asyncio
async def test_wechat_publish_rejects_editor_validation_error(
    test_settings: PublisherSettings,
) -> None:
    publisher = WeChatPublisher(test_settings)
    publisher.check_risk_control = AsyncMock()  # type: ignore[method-assign]

    page = MagicMock()
    publish_button = MagicMock()
    publish_button.first = publish_button
    publish_button.count = AsyncMock(return_value=1)
    publish_button.is_visible = AsyncMock(return_value=True)
    publish_button.inner_text = AsyncMock(return_value="发表")
    publish_button.click = AsyncMock()
    page.locator.return_value = publish_button

    validation = MagicMock()
    validation.first = validation
    validation.count = AsyncMock(return_value=1)
    validation.is_visible = AsyncMock(return_value=True)
    empty = MagicMock()
    empty.first = empty
    empty.count = AsyncMock(return_value=0)

    def get_by_text(text: str, **_: Any) -> MagicMock:
        if text == "标题不能为空且长度不能超过64字":
            return validation
        return empty

    page.get_by_text.side_effect = get_by_text
    publisher.get_page = AsyncMock(return_value=page)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="Editor validation failed"):
        await publisher.publish_and_confirm(MagicMock())


@pytest.mark.asyncio
async def test_wechat_publish_disables_group_notice_and_accepts_no_declaration(
    test_settings: PublisherSettings,
) -> None:
    publisher = WeChatPublisher(test_settings)
    publisher.check_risk_control = AsyncMock()  # type: ignore[method-assign]
    publisher._disable_group_notification = AsyncMock()  # type: ignore[method-assign]
    publisher._wait_for_admin_verification = AsyncMock(  # type: ignore[method-assign]
        return_value=False
    )

    page = MagicMock()
    entry = MagicMock()
    entry.first = entry
    entry.count = AsyncMock(return_value=1)
    entry.is_visible = AsyncMock(return_value=True)
    entry.inner_text = AsyncMock(return_value="发表")
    entry.click = AsyncMock()
    page.locator.return_value = entry

    publish_button = MagicMock()
    publish_button.click = AsyncMock()
    no_declaration_button = MagicMock()
    no_declaration_button.click = AsyncMock()

    stage = 0

    async def click_publish() -> None:
        nonlocal stage
        stage = 1

    publish_button.click.side_effect = click_publish

    async def visible_button(_page: Any, names: list[str], **_: Any) -> Any | None:
        if "无需声明并发表" in names and stage == 1:
            return no_declaration_button
        if "发表" in names and stage == 0:
            return publish_button
        return None

    publisher._visible_button = AsyncMock(side_effect=visible_button)  # type: ignore[method-assign]
    page.get_by_text.return_value = MagicMock(
        count=AsyncMock(return_value=0),
        first=MagicMock(is_visible=AsyncMock(return_value=False)),
    )

    with patch.object(publisher, "get_page", new_callable=AsyncMock) as get_page:
        get_page.return_value = page
        await publisher.publish_and_confirm(MagicMock())

    entry.click.assert_awaited_once()
    publisher._disable_group_notification.assert_awaited_once_with(page)
    publish_button.click.assert_awaited_once()
    no_declaration_button.click.assert_awaited_once()


@pytest.mark.asyncio
async def test_wechat_publish_does_not_reuse_entry_as_confirmation(
    test_settings: PublisherSettings,
) -> None:
    """A direct publish entry may need a second click after UI validation."""
    test_settings.operation_timeout_seconds = 10
    publisher = WeChatPublisher(test_settings)
    publisher.check_risk_control = AsyncMock()  # type: ignore[method-assign]
    publisher._disable_group_notification = AsyncMock()  # type: ignore[method-assign]
    publisher._wait_for_admin_verification = AsyncMock(  # type: ignore[method-assign]
        return_value=False
    )

    page = MagicMock()
    empty = MagicMock()
    empty.first = empty
    empty.count = AsyncMock(return_value=0)
    page.get_by_role.return_value = empty
    page.get_by_text.return_value = empty
    publisher.get_page = AsyncMock(return_value=page)  # type: ignore[method-assign]

    entry = MagicMock()
    entry.is_visible = AsyncMock(return_value=True)
    entry.click = AsyncMock()
    publisher._find_publish_entry = AsyncMock(  # type: ignore[method-assign]
        return_value=(entry, "发表")
    )
    publisher._visible_button = AsyncMock(return_value=None)  # type: ignore[method-assign]

    class FakeLoop:
        current_time = 0.0

        def time(self) -> float:
            return self.current_time

    fake_loop = FakeLoop()

    async def advance_time(delay: float) -> None:
        fake_loop.current_time += delay

    with (
        patch(
            "publisher.platforms.wechat.asyncio.get_running_loop",
            return_value=fake_loop,
        ),
        patch("publisher.platforms.wechat.asyncio.sleep", side_effect=advance_time),
    ):
        await publisher.publish_and_confirm(MagicMock())

    assert entry.click.await_count == 2
    assert entry.click.await_args_list[0].args == ()
    assert entry.click.await_args_list[1].kwargs == {"force": True}
    confirm_calls = [
        call
        for call in publisher._visible_button.await_args_list
        if call.args[1] == CONFIRM_BUTTON_NAMES
    ]
    assert confirm_calls
    assert confirm_calls[0].kwargs["exclude_button"] is entry
    assert confirm_calls[0].kwargs["exclude_classes"] == ("mass_send",)


@pytest.mark.asyncio
async def test_wechat_waits_for_delayed_admin_verification(
    test_settings: PublisherSettings,
) -> None:
    publisher = WeChatPublisher(test_settings)
    publisher._handle_admin_verification = AsyncMock(  # type: ignore[method-assign]
        side_effect=[False, False, True]
    )

    with patch("publisher.platforms.wechat.asyncio.sleep", new_callable=AsyncMock):
        handled = await publisher._wait_for_admin_verification(
            MagicMock(), MagicMock(), timeout_seconds=8
        )

    assert handled is True
    assert publisher._handle_admin_verification.await_count == 3


@pytest.mark.asyncio
async def test_wechat_disables_enabled_group_notification() -> None:
    page = MagicMock()
    label = MagicMock()
    label.first = label
    label.count = AsyncMock(return_value=1)
    label.is_visible = AsyncMock(return_value=True)
    row = MagicMock()
    switch_enabled = True

    async def enabled_count() -> int:
        return int(switch_enabled)

    async def disable_switch() -> None:
        nonlocal switch_enabled
        switch_enabled = False

    enabled_switch = MagicMock()
    enabled_switch.first = enabled_switch
    enabled_switch.count = AsyncMock(side_effect=enabled_count)
    enabled_switch.is_visible = AsyncMock(return_value=True)
    enabled_switch.click = AsyncMock()
    switch_shell = MagicMock()
    switch_shell.first = switch_shell
    switch_shell.count = AsyncMock(return_value=1)
    switch_shell.is_visible = AsyncMock(return_value=True)
    switch_shell.click = AsyncMock(side_effect=disable_switch)
    enabled_switch.locator.return_value = switch_shell
    label.locator.return_value = row
    row.locator.return_value = enabled_switch
    empty_label = MagicMock()
    empty_label.count = AsyncMock(return_value=0)
    page.get_by_text.side_effect = lambda name, **_: (
        empty_label if name == "群发通知" else label
    )

    await WeChatPublisher._disable_group_notification(page)
    switch_shell.click.assert_awaited_once_with()
    enabled_switch.click.assert_not_awaited()


@pytest.mark.asyncio
async def test_wechat_disables_offscreen_group_notification_with_native_click() -> None:
    page = MagicMock()
    label = MagicMock()
    label.first = label
    label.count = AsyncMock(return_value=1)
    label.is_visible = AsyncMock(return_value=True)
    row = MagicMock()
    switch_enabled = True

    async def enabled_count() -> int:
        return int(switch_enabled)

    async def disable_switch(_: str) -> None:
        nonlocal switch_enabled
        switch_enabled = False

    enabled_switch = MagicMock()
    enabled_switch.first = enabled_switch
    enabled_switch.count = AsyncMock(side_effect=enabled_count)
    enabled_switch.is_visible = AsyncMock(return_value=True)
    enabled_switch.click = AsyncMock(
        side_effect=RuntimeError("Element is outside of the viewport")
    )
    enabled_switch.evaluate = AsyncMock(side_effect=disable_switch)
    empty_shell = MagicMock()
    empty_shell.first = empty_shell
    empty_shell.count = AsyncMock(return_value=0)
    enabled_switch.locator.return_value = empty_shell
    label.locator.return_value = row
    row.locator.return_value = enabled_switch
    page.get_by_text.return_value = label

    await WeChatPublisher._disable_group_notification(page)

    enabled_switch.click.assert_awaited_once_with()
    enabled_switch.evaluate.assert_awaited_once_with("element => element.click()")


@pytest.mark.asyncio
async def test_wechat_handles_follow_up_publish_confirmation(
    test_settings: PublisherSettings,
) -> None:
    publisher = WeChatPublisher(test_settings)
    publisher.check_risk_control = AsyncMock()  # type: ignore[method-assign]
    publisher._disable_group_notification = AsyncMock()  # type: ignore[method-assign]

    page = MagicMock()
    entry = MagicMock()
    entry.first = entry
    entry.count = AsyncMock(return_value=1)
    entry.is_visible = AsyncMock(return_value=True)
    entry.inner_text = AsyncMock(return_value="发表")
    entry.click = AsyncMock()
    page.locator.return_value = entry

    first_confirm = MagicMock()
    first_confirm.click = AsyncMock()
    follow_up = MagicMock()
    follow_up.click = AsyncMock()
    state = 0

    async def visible_button(_page: Any, names: list[str], **_: Any) -> Any | None:
        if names == CONFIRM_BUTTON_NAMES and state == 0:
            return first_confirm
        if "继续发表" in names and state == 1:
            return follow_up
        return None

    async def click_first() -> None:
        nonlocal state
        state = 1

    async def click_follow_up() -> None:
        nonlocal state
        state = 2

    first_confirm.click.side_effect = click_first
    follow_up.click.side_effect = click_follow_up
    publisher._visible_button = AsyncMock(side_effect=visible_button)  # type: ignore[method-assign]
    publisher._wait_for_admin_verification = AsyncMock(  # type: ignore[method-assign]
        side_effect=[False, True]
    )
    page.get_by_text.return_value = MagicMock(
        count=AsyncMock(return_value=0),
        first=MagicMock(is_visible=AsyncMock(return_value=False)),
    )

    with patch.object(publisher, "get_page", new_callable=AsyncMock) as get_page:
        get_page.return_value = page
        await publisher.publish_and_confirm(MagicMock())

    first_confirm.click.assert_awaited_once()
    follow_up.click.assert_awaited_once()


@pytest.mark.asyncio
async def test_wechat_handle_admin_verification_flow(
    test_settings: PublisherSettings, tmp_path: Any
) -> None:
    from publisher.models import PublishJob

    del tmp_path
    publisher = WeChatPublisher(test_settings)

    page = MagicMock()
    page.frames = []
    page.is_closed.return_value = False

    dialog_loc = MagicMock()
    dialog_loc.count = AsyncMock(return_value=1)
    dialog_loc.first = dialog_loc
    dialog_loc.is_visible = AsyncMock(side_effect=[True, True, False])
    dialog_loc.inner_text = AsyncMock(return_value="微信验证\n请使用管理员微信号扫码")

    page.locator.return_value = dialog_loc
    page.inner_text = AsyncMock(return_value="")

    job = PublishJob(
        platform="wechat_mp",
        mode="publish",
        content={"title": "测试文章标题"},
    )

    with patch(
        "publisher.notify_alert.emit_notify_hub_alert", new_callable=AsyncMock
    ) as mock_alert:
        mock_alert.return_value = True
        handled = await publisher._handle_admin_verification(page, job)
        assert handled is True
        assert mock_alert.await_count == 1
        alert_kwargs = mock_alert.await_args.kwargs
        assert alert_kwargs["event_type"] == "publisher.wechat.manual_confirm"
        assert "测试文章标题" in alert_kwargs["content"]
        assert "二维码" not in alert_kwargs["content"]
        assert alert_kwargs.get("image_path") is None


@pytest.mark.asyncio
async def test_wechat_detects_portal_verification_by_visible_body_text(
    test_settings: PublisherSettings,
) -> None:
    publisher = WeChatPublisher(test_settings)
    page = MagicMock()
    page.frames = []
    page.is_closed.return_value = False
    dialog_loc = MagicMock()
    dialog_loc.count = AsyncMock(return_value=0)
    page.locator.return_value = dialog_loc
    page.screenshot = AsyncMock()
    page.inner_text = AsyncMock(
        side_effect=["微信验证\n扫码后，请联系管理员进行验证", "已发表成功"]
    )

    job = PublishJob(
        platform="wechat_mp",
        mode="publish",
        content={"title": "门户验证测试"},
    )

    with patch(
        "publisher.notify_alert.emit_notify_hub_alert", new_callable=AsyncMock
    ) as mock_alert:
        mock_alert.return_value = True
        handled = await publisher._handle_admin_verification(page, job)

    assert handled is True
    page.screenshot.assert_not_awaited()
    assert mock_alert.await_count == 1
    assert (
        mock_alert.await_args.kwargs["event_type"] == "publisher.wechat.manual_confirm"
    )
    assert mock_alert.await_args.kwargs.get("image_path") is None


@pytest.mark.asyncio
async def test_emit_notify_hub_alert_with_image(tmp_path: Any) -> None:
    test_img = Path(tmp_path) / "test_qr.png"
    test_img.write_bytes(b"dummy-png-data")

    settings = PublisherSettings(
        notify_event_url="http://hub.test/api/v1/events",
        notify_api_key=SecretStr("test-key"),
        notify_recipient_ids=["admin_user"],
    )

    upload_called = False
    event_called = False

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal upload_called, event_called
        if request.url.path == "/api/v1/media":
            upload_called = True
            assert request.headers.get("X-API-Key") == "test-key"
            return httpx.Response(201, json={"id": "media_test_qr_123"})
        elif request.url.path == "/api/v1/events":
            event_called = True
            body = request.read().decode()
            assert "media_test_qr_123" in body
            assert "image" in body
            return httpx.Response(202, json={"status": "accepted"})
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)
    with patch(
        "httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)
    ):
        res = await emit_notify_hub_alert(
            settings,
            event_type="publisher.wechat.verify_qr",
            event_key="test_key_1",
            title="验证码",
            content="请扫码",
            image_path=test_img,
        )
        assert res is True
        assert upload_called is True
        assert event_called is True
