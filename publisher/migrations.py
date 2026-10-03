"""Upgrade both Alembic databases and legacy databases created by create_all."""

from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command
from publisher.config import PublisherSettings


def upgrade_database(settings: PublisherSettings) -> None:
    settings.ensure_directories()
    root = Path(__file__).resolve().parent.parent
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.attributes["publisher_settings"] = settings
    engine = create_engine(settings.sync_db_url)
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            tables = set(inspector.get_table_names())
            legacy = "alembic_version" not in tables and bool(tables)
            if legacy:
                # Earlier releases used create_all without a revision table.
                # Verify the complete initial schema before stamping it.
                baseline = {"publish_jobs", "media_assets", "platform_states"}
                if tables != baseline:
                    raise RuntimeError(
                        "Unversioned publisher database has an unsupported schema"
                    )
                # Compare against a fresh initial migration, independently of current ORM models.
                import tempfile

                with tempfile.TemporaryDirectory(prefix="publisher-schema-") as temp:
                    reference = settings.model_copy(update={"data_dir": Path(temp)})
                    reference_config = Config(str(root / "alembic.ini"))
                    reference_config.set_main_option(
                        "script_location", str(root / "alembic")
                    )
                    reference_config.attributes["publisher_settings"] = reference
                    command.upgrade(reference_config, "0001_initial_schema")
                    reference_engine = create_engine(reference.sync_db_url)
                    try:
                        expected = inspect(reference_engine)
                        for table in baseline:
                            names = {c["name"] for c in inspector.get_columns(table)}
                            required = {c["name"] for c in expected.get_columns(table)}
                            if not required.issubset(names):
                                raise RuntimeError(
                                    "Unversioned publisher database is missing initial columns"
                                )
                    finally:
                        reference_engine.dispose()
        if legacy:
            command.stamp(config, "0001_initial_schema")
        command.upgrade(config, "head")
    finally:
        engine.dispose()
