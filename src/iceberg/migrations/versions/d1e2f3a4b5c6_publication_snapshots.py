"""immutable publication snapshots and delivery uniqueness

Revision ID: d1e2f3a4b5c6
Revises: c9f8a7e6d5c4
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, Sequence[str], None] = "c9f8a7e6d5c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("report", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("version", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.add_column(
            sa.Column("publication_snapshot_hash", sa.String(), nullable=False, server_default="")
        )
        batch_op.create_index("ix_report_publication_snapshot_hash", ["publication_snapshot_hash"], unique=False)
    with op.batch_alter_table("renderedproduct", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("snapshot_hash", sa.String(), nullable=False, server_default="")
        )
        batch_op.create_index("ix_renderedproduct_snapshot_hash", ["snapshot_hash"], unique=False)
    op.create_table(
        "publicationsnapshot",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("snapshot_hash", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["report_id"], ["report.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("report_id", name="uq_publication_snapshot_report"),
    )
    op.create_index("ix_publicationsnapshot_report_id", "publicationsnapshot", ["report_id"], unique=False)
    op.create_index("ix_publicationsnapshot_snapshot_hash", "publicationsnapshot", ["snapshot_hash"], unique=False)
    with op.batch_alter_table("disseminationevent", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_dissemination_report_stakeholder", ["report_id", "stakeholder_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("disseminationevent", schema=None) as batch_op:
        batch_op.drop_constraint("uq_dissemination_report_stakeholder", type_="unique")
    op.drop_index("ix_publicationsnapshot_snapshot_hash", table_name="publicationsnapshot")
    op.drop_index("ix_publicationsnapshot_report_id", table_name="publicationsnapshot")
    op.drop_table("publicationsnapshot")
    with op.batch_alter_table("renderedproduct", schema=None) as batch_op:
        batch_op.drop_index("ix_renderedproduct_snapshot_hash")
        batch_op.drop_column("snapshot_hash")
    with op.batch_alter_table("report", schema=None) as batch_op:
        batch_op.drop_index("ix_report_publication_snapshot_hash")
        batch_op.drop_column("publication_snapshot_hash")
        batch_op.drop_column("version")
