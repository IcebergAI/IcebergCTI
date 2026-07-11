"""add selectable publication webhook payload format

Revision ID: e2b3c4d5e6f7
Revises: d1a4f7e6c5b4
Create Date: 2026-07-11 00:00:00.000000

``generic`` is a server default so existing deployments retain the exact
metadata JSON envelope they already receive. Slack and Teams envelopes are
strictly opt-in through the admin configuration.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "e2b3c4d5e6f7"
down_revision: str | Sequence[str] | None = "d1a4f7e6c5b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("webhooksettings") as batch:
        batch.add_column(
            sa.Column(
                "format",
                sa.String(length=16),
                nullable=False,
                server_default="generic",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("webhooksettings") as batch:
        batch.drop_column("format")
