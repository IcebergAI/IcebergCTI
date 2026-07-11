"""track taxonomy object version timestamps

Revision ID: a6b7c8d9e0f1
Revises: a5b6c7d8e9f0
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a6b7c8d9e0f1"
down_revision: str | Sequence[str] | None = "a5b6c7d8e9f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("tag") as batch:
        batch.add_column(sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()))
        batch.create_index("ix_tag_updated_at", ["updated_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("tag") as batch:
        batch.drop_index("ix_tag_updated_at")
        batch.drop_column("updated_at")
