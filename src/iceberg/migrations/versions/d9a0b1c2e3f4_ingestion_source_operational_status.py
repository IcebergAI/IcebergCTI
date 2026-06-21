"""ingestion source operational status

Revision ID: d9a0b1c2e3f4
Revises: c8f0a1b2d3e4
Create Date: 2026-06-21 00:00:01.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d9a0b1c2e3f4"
down_revision: str | Sequence[str] | None = "c8f0a1b2d3e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("ingestionsource") as batch:
        batch.add_column(
            sa.Column("last_error", sa.String(), nullable=False, server_default="")
        )
        batch.add_column(sa.Column("last_status_code", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("last_item_count", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("ingestionsource") as batch:
        batch.drop_column("last_item_count")
        batch.drop_column("last_status_code")
        batch.drop_column("last_error")
