"""Record provenance on related-product index entries.

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-08-18 00:00:00.000000

An index entry now says which provider/model/version produced it and which
report revision and text digest it was produced from, so a provider change or an
edited product can be detected as stale instead of served silently (#311).
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op


revision: str = "d0e1f2a3b4c5"
down_revision: str | Sequence[str] | None = "c9d0e1f2a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("reportembedding") as batch:
        batch.add_column(
            sa.Column("model", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column("model_version", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default="")
        )
        batch.add_column(sa.Column("dimensions", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("source_version", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(
            sa.Column("content_sha256", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default="")
        )
    # Existing rows carry no provenance, so they read as stale and the next
    # bounded reindex pass regenerates them. Nothing is served from a row whose
    # provenance cannot be checked.


def downgrade() -> None:
    with op.batch_alter_table("reportembedding") as batch:
        batch.drop_column("content_sha256")
        batch.drop_column("source_version")
        batch.drop_column("dimensions")
        batch.drop_column("model_version")
        batch.drop_column("model")
