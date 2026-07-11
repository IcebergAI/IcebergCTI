"""reserve stable identities for concurrent MISP pushes

Revision ID: a5b6c7d8e9f0
Revises: b5c6d7e8f9a0
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a5b6c7d8e9f0"
down_revision: str | Sequence[str] | None = "b5c6d7e8f9a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("reportmispevent") as batch:
        batch.add_column(sa.Column("external_created", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("push_token", sa.String(), nullable=False, server_default=""))
        batch.add_column(sa.Column("push_started_at", sa.DateTime(), nullable=True))
        batch.create_index("ix_reportmispevent_push_token", ["push_token"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("reportmispevent") as batch:
        batch.drop_index("ix_reportmispevent_push_token")
        batch.drop_column("push_started_at")
        batch.drop_column("push_token")
        batch.drop_column("external_created")
