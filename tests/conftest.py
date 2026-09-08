"""Pytest fixtures for Browser Publisher tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from publisher.config import PublisherSettings
from publisher.database import Base, create_engine_and_sessionmaker
from publisher.main import create_app
from publisher.models import PlatformState, utc_now
from publisher.platforms.fake import FakePublisher


@pytest.fixture
def test_settings(tmp_path: Path) -> PublisherSettings:
    return PublisherSettings(
        access_token=SecretStr("test-secret-token-1234567890"),
        data_dir=tmp_path / "data",
        host="127.0.0.1",
        port=8790,
        wechat_mp_default_mode="publish",
        xhs_default_mode="draft",
        xhs_min_interval_seconds=1800,
        headless=True,
    )


@pytest_asyncio.fixture
async def test_db(
    test_settings: PublisherSettings,
) -> AsyncGenerator[tuple[object, async_sessionmaker[AsyncSession]], None]:
    engine, session_factory = create_engine_and_sessionmaker(test_settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        for p_name in ["wechat_mp", "xiaohongshu"]:
            stmt = select(PlatformState).where(PlatformState.platform == p_name)
            existing = (await session.execute(stmt)).scalar_one_or_none()
            if not existing:
                session.add(
                    PlatformState(
                        platform=p_name,
                        session_state="ready",
                        is_paused=False,
                        updated_at=utc_now(),
                    )
                )
        await session.commit()

    yield engine, session_factory
    await engine.dispose()


@pytest_asyncio.fixture
async def fake_publishers(test_settings: PublisherSettings) -> dict[str, FakePublisher]:
    return {
        "wechat_mp": FakePublisher(test_settings),
        "xiaohongshu": FakePublisher(test_settings),
    }


@pytest_asyncio.fixture
async def client(
    test_settings: PublisherSettings,
    test_db: tuple[object, async_sessionmaker[AsyncSession]],
    fake_publishers: dict[str, FakePublisher],
) -> AsyncGenerator[AsyncClient, None]:
    engine, session_factory = test_db
    app = create_app(test_settings)
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.publishers = fake_publishers

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={
            "Authorization": f"Bearer {test_settings.access_token.get_secret_value()}"
        },
    ) as ac:
        yield ac
