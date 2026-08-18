"""Governed evidence references from adjacent systems.

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-08-18 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "f2a3b4c5d6e7"
down_revision: str | Sequence[str] | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TLP_VALUES = ("RED", "AMBER_STRICT", "AMBER", "GREEN", "CLEAR")
_STATE = sa.Enum(
    "PENDING", "ACCEPTED", "REJECTED", "SUPERSEDED", "REVOKED", name="evidencestate"
)


def _tlp(bind):
    """Reference the TLP enum the earlier marking migration created."""
    if bind.dialect.name == "postgresql":
        return postgresql.ENUM(*_TLP_VALUES, name="tlp", create_type=False)
    return sa.Enum(*_TLP_VALUES, name="tlp")


def upgrade() -> None:
    bind = op.get_bind()
    op.create_table(
        "evidencereference",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("notebook_id", sa.Integer(), nullable=False),
        sa.Column("schema_version", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=""),
        sa.Column("source_system", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("external_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("revision", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=""),
        sa.Column("evidence_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=""),
        sa.Column("title", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=""),
        sa.Column("summary", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=""),
        sa.Column("deep_link", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=""),
        sa.Column("tlp", _tlp(bind), nullable=False),
        sa.Column("content_sha256", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=""),
        sa.Column("provenance", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("state", _STATE, nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("received_by_id", sa.Integer(), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("decided_by_id", sa.Integer(), nullable=True),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_reason", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=""),
        sa.ForeignKeyConstraint(["decided_by_id"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["notebook_id"], ["notebook.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["received_by_id"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_id"], ["source.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_system", "external_id", "revision", name="uq_evidence_identity"
        ),
    )
    for name, columns in (
        ("ix_evidencereference_notebook_id", ["notebook_id"]),
        ("ix_evidencereference_source_system", ["source_system"]),
        ("ix_evidencereference_external_id", ["external_id"]),
        ("ix_evidencereference_state", ["state"]),
        ("ix_evidencereference_source_id", ["source_id"]),
    ):
        op.create_index(name, "evidencereference", columns, unique=False)


def downgrade() -> None:
    for name in (
        "ix_evidencereference_source_id",
        "ix_evidencereference_state",
        "ix_evidencereference_external_id",
        "ix_evidencereference_source_system",
        "ix_evidencereference_notebook_id",
    ):
        op.drop_index(name, table_name="evidencereference")
    op.drop_table("evidencereference")
    _STATE.drop(op.get_bind(), checkfirst=True)
