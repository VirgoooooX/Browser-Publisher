"""Persist session probes and acknowledged authentication alerts.

Revision ID: 0002_session_health
Revises: 0001_initial_schema
"""

import sqlalchemy as sa

from alembic import op

revision = "0002_session_health"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "platform_states",
        sa.Column("last_session_check_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "platform_states", sa.Column("auth_alert_sent_at", sa.DateTime(timezone=True))
    )


def downgrade() -> None:
    with op.batch_alter_table("platform_states") as batch:
        batch.drop_column("auth_alert_sent_at")
        batch.drop_column("last_session_check_at")
