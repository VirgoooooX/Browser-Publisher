"""Initial schema for publish_jobs, media_assets, platform_states.

Revision ID: 0001_initial_schema
Revises: 
Create Date: 2026-09-08 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. publish_jobs
    op.create_table(
        "publish_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("client_request_id", sa.String(length=200), nullable=False),
        sa.Column("platform", sa.String(length=50), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("publish_phase", sa.String(length=30), nullable=True),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("media", sa.JSON(), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=True),
        sa.Column("topics", sa.JSON(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("platform_draft_id", sa.String(length=200), nullable=True),
        sa.Column("publish_id", sa.String(length=200), nullable=True),
        sa.Column("final_url", sa.String(length=2048), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_publish_jobs_client_request_id", "publish_jobs", ["client_request_id"], unique=True)
    op.create_index("ix_publish_jobs_platform", "publish_jobs", ["platform"], unique=False)
    op.create_index("ix_publish_jobs_status", "publish_jobs", ["status"], unique=False)
    op.create_index("ix_publish_jobs_created_at", "publish_jobs", ["created_at"], unique=False)

    # 2. media_assets
    op.create_table(
        "media_assets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_media_assets_checksum", "media_assets", ["checksum"], unique=False)
    op.create_index("ix_media_assets_created_at", "media_assets", ["created_at"], unique=False)

    # 3. platform_states
    op.create_table(
        "platform_states",
        sa.Column("platform", sa.String(length=50), nullable=False),
        sa.Column("session_state", sa.String(length=30), nullable=False, server_default="idle"),
        sa.Column("current_job_id", sa.String(length=36), nullable=True),
        sa.Column("last_job_id", sa.String(length=36), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("is_paused", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("paused_reason", sa.Text(), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("alert_incident_id", sa.String(length=100), nullable=True),
        sa.Column("qr_code_base64", sa.Text(), nullable=True),
        sa.Column("last_auth_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_publish_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("platform"),
    )

    # Seed initial platform states
    now_utc = sa.text("CURRENT_TIMESTAMP")
    op.execute(
        sa.text(
            "INSERT INTO platform_states (platform, session_state, is_paused, updated_at) "
            "VALUES ('wechat_mp', 'idle', 0, CURRENT_TIMESTAMP), ('xiaohongshu', 'idle', 0, CURRENT_TIMESTAMP);"
        )
    )


def downgrade() -> None:
    op.drop_table("platform_states")
    op.drop_table("media_assets")
    op.drop_table("publish_jobs")
