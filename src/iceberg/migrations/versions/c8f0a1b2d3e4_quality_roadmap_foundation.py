"""quality roadmap foundation

Revision ID: c8f0a1b2d3e4
Revises: b2c5de1928db
Create Date: 2026-06-21 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8f0a1b2d3e4"
down_revision: str | Sequence[str] | None = "b2c5de1928db"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("user") as batch:
        batch.add_column(sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("department", sa.String(), nullable=False, server_default=""))
        batch.add_column(sa.Column("job_title", sa.String(), nullable=False, server_default=""))
        batch.add_column(sa.Column("company_name", sa.String(), nullable=False, server_default=""))
        batch.add_column(sa.Column("office_location", sa.String(), nullable=False, server_default=""))

    with op.batch_alter_table("source") as batch:
        batch.add_column(sa.Column("content_md", sa.String(), nullable=False, server_default=""))
        batch.add_column(sa.Column("ai_provenance", sa.JSON(), nullable=False, server_default="{}"))

    with op.batch_alter_table("report") as batch:
        batch.add_column(sa.Column("ai_provenance", sa.JSON(), nullable=False, server_default="{}"))

    op.create_table(
        "audiencegroup",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_audience_group_slug"),
    )
    op.create_index(op.f("ix_audiencegroup_slug"), "audiencegroup", ["slug"], unique=False)

    op.create_table(
        "usertagsubscription",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("tag_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["tag_id"], ["tag.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "tag_id"),
    )
    op.create_table(
        "useraudiencegroup",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["audiencegroup.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "group_id"),
    )
    op.create_table(
        "reportaudiencegroup",
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["audiencegroup.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["report_id"], ["report.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("report_id", "group_id"),
    )

    op.create_table(
        "ingestionsource",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "ingesteditem",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.Column("external_id", sa.String(), nullable=False, server_default=""),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False, server_default=""),
        sa.Column("summary", sa.String(), nullable=False, server_default=""),
        sa.Column("content_md", sa.String(), nullable=False, server_default=""),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("promoted_notebook_id", sa.Integer(), nullable=True),
        sa.Column("promoted_source_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["promoted_notebook_id"], ["notebook.id"]),
        sa.ForeignKeyConstraint(["promoted_source_id"], ["source.id"]),
        sa.ForeignKeyConstraint(["source_id"], ["ingestionsource.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "external_id", name="uq_ingested_item_source_external"),
    )
    op.create_index(op.f("ix_ingesteditem_source_id"), "ingesteditem", ["source_id"], unique=False)
    op.create_index(op.f("ix_ingesteditem_status"), "ingesteditem", ["status"], unique=False)

    op.create_table(
        "reportembedding",
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("backend", sa.String(), nullable=False, server_default=""),
        sa.Column("vector", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["report_id"], ["report.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("report_id"),
    )


def downgrade() -> None:
    op.drop_table("reportembedding")
    op.drop_index(op.f("ix_ingesteditem_status"), table_name="ingesteditem")
    op.drop_index(op.f("ix_ingesteditem_source_id"), table_name="ingesteditem")
    op.drop_table("ingesteditem")
    op.drop_table("ingestionsource")
    op.drop_table("reportaudiencegroup")
    op.drop_table("useraudiencegroup")
    op.drop_table("usertagsubscription")
    op.drop_index(op.f("ix_audiencegroup_slug"), table_name="audiencegroup")
    op.drop_table("audiencegroup")

    with op.batch_alter_table("report") as batch:
        batch.drop_column("ai_provenance")
    with op.batch_alter_table("source") as batch:
        batch.drop_column("ai_provenance")
        batch.drop_column("content_md")
    with op.batch_alter_table("user") as batch:
        batch.drop_column("office_location")
        batch.drop_column("company_name")
        batch.drop_column("job_title")
        batch.drop_column("department")
        batch.drop_column("token_version")
