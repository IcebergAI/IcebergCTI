"""retain taxonomy merge lineage

Revision ID: f3e4d5c6b7a8
Revises: e2b3c4d5e6f7
Create Date: 2026-07-11 00:00:00.000000

Merged terms remain as retired rows.  ``merged_into_tag_id`` preserves the
canonical lineage after report and stakeholder links are moved, instead of
deleting the source term and losing its historic identity.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "f3e4d5c6b7a8"
down_revision: str | Sequence[str] | None = "e2b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("tag") as batch:
        batch.add_column(sa.Column("merged_into_tag_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("merged_at", sa.DateTime(), nullable=True))
        batch.create_foreign_key(
            "fk_tag_merged_into_tag_id",
            "tag",
            ["merged_into_tag_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_tag_merged_into_tag_id", ["merged_into_tag_id"])


def downgrade() -> None:
    with op.batch_alter_table("tag") as batch:
        batch.drop_index("ix_tag_merged_into_tag_id")
        batch.drop_constraint("fk_tag_merged_into_tag_id", type_="foreignkey")
        batch.drop_column("merged_at")
        batch.drop_column("merged_into_tag_id")
