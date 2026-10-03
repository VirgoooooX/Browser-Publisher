"""Fresh installs and legacy create_all upgrades preserve saved jobs."""

from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command
from publisher.config import PublisherSettings
from publisher.migrations import upgrade_database


def migration_config(settings: PublisherSettings) -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.attributes["publisher_settings"] = settings
    return config


@pytest.mark.parametrize("legacy", [False, True])
def test_upgrade_preserves_state_and_is_repeatable(
    test_settings: PublisherSettings, legacy: bool
) -> None:
    test_settings.ensure_directories()
    if legacy:
        command.upgrade(migration_config(test_settings), "0001_initial_schema")
        engine = create_engine(test_settings.sync_db_url)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE platform_states SET session_state='ready', updated_at='2026-10-03' WHERE platform='wechat_mp'"
                )
            )
            connection.execute(text("DROP TABLE alembic_version"))
        engine.dispose()
    upgrade_database(test_settings)
    upgrade_database(test_settings)
    engine = create_engine(test_settings.sync_db_url)
    with engine.connect() as connection:
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("platform_states")
        }
        assert {"last_session_check_at", "auth_alert_sent_at"} <= columns
        assert (
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == "0002_session_health"
        )
        if legacy:
            assert (
                connection.execute(
                    text(
                        "SELECT session_state FROM platform_states WHERE platform='wechat_mp'"
                    )
                ).scalar_one()
                == "ready"
            )
    engine.dispose()
    command.downgrade(migration_config(test_settings), "0001_initial_schema")
    upgrade_database(test_settings)


def test_unknown_legacy_schema_is_not_stamped(test_settings: PublisherSettings) -> None:
    test_settings.ensure_directories()
    engine = create_engine(test_settings.sync_db_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE unrelated (id INTEGER)"))
    with pytest.raises(RuntimeError, match="unsupported schema"):
        upgrade_database(test_settings)
    assert "alembic_version" not in inspect(engine).get_table_names()
    engine.dispose()
