"""Versioned dissemination policies with previewable recipient resolution.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-08-18 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op


revision: str = "c9d0e1f2a3b4"
down_revision: str | Sequence[str] | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "disseminationpolicy",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("slug", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("description", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_dissemination_policy_slug"),
    )
    op.create_index(
        "ix_disseminationpolicy_slug", "disseminationpolicy", ["slug"], unique=False
    )

    op.create_table(
        "disseminationpolicyversion",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("policy_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("note", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("rules", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("effective_from", sa.DateTime(), nullable=True),
        sa.Column("effective_to", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("approved_by_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["approved_by_id"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_id"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["policy_id"], ["disseminationpolicy.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "policy_id", "version", name="uq_dissemination_policy_version"
        ),
    )
    op.create_index(
        "ix_disseminationpolicyversion_policy_id",
        "disseminationpolicyversion",
        ["policy_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_disseminationpolicyversion_policy_id",
        table_name="disseminationpolicyversion",
    )
    op.drop_table("disseminationpolicyversion")
    op.drop_index("ix_disseminationpolicy_slug", table_name="disseminationpolicy")
    op.drop_table("disseminationpolicy")
