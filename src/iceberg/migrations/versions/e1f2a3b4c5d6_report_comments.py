"""Section-anchored editorial comments and suggested edits.

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-08-18 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op


revision: str = "e1f2a3b4c5d6"
down_revision: str | Sequence[str] | None = "d0e1f2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SECTION = sa.Enum(
    "BODY",
    "KEY_JUDGEMENTS",
    "KEY_ASSUMPTIONS",
    "INTELLIGENCE_GAPS",
    "SOURCES",
    "ATTACHMENTS",
    "INDICATORS",
    "FIGURES",
    name="reportsection",
)
_STATUS = sa.Enum("OPEN", "RESOLVED", "ACCEPTED", "REJECTED", name="commentstatus")


def upgrade() -> None:
    op.create_table(
        "reportcomment",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("thread_id", sa.Integer(), nullable=True),
        sa.Column("author_id", sa.Integer(), nullable=True),
        sa.Column("section", _SECTION, nullable=False),
        sa.Column("anchor_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("anchor_text", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=""),
        sa.Column("anchor_sha256", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=""),
        sa.Column("body", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=""),
        sa.Column("suggestion", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=""),
        sa.Column("blocking", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", _STATUS, nullable=False),
        sa.Column("mentions", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_by_id", sa.Integer(), nullable=True),
        sa.Column("applied_version", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["author_id"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["report_id"], ["report.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resolved_by_id"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["thread_id"], ["reportcomment.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for name, columns in (
        ("ix_reportcomment_report_id", ["report_id"]),
        ("ix_reportcomment_thread_id", ["thread_id"]),
        ("ix_reportcomment_author_id", ["author_id"]),
        ("ix_reportcomment_section", ["section"]),
        ("ix_reportcomment_status", ["status"]),
    ):
        op.create_index(name, "reportcomment", columns, unique=False)


def downgrade() -> None:
    for name in (
        "ix_reportcomment_status",
        "ix_reportcomment_section",
        "ix_reportcomment_author_id",
        "ix_reportcomment_thread_id",
        "ix_reportcomment_report_id",
    ):
        op.drop_index(name, table_name="reportcomment")
    op.drop_table("reportcomment")
    bind = op.get_bind()
    _STATUS.drop(bind, checkfirst=True)
    _SECTION.drop(bind, checkfirst=True)
