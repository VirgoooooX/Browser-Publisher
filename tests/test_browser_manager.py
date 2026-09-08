from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from publisher.config import PublisherSettings
from publisher.platforms.browser import BrowserManager


@pytest.mark.asyncio
async def test_closed_context_is_not_healthy(test_settings: PublisherSettings) -> None:
    manager = BrowserManager(test_settings)
    context = MagicMock()
    manager._context = context

    manager._mark_context_closed(context)

    assert await manager.check_health() is False
