"""Add durable, reviewable feed-item IOC candidates.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-08-14 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op


revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("source") as batch:
        batch.add_column(sa.Column("feed_item_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_source_feed_item", "feeditem", ["feed_item_id"], ["id"], ondelete="SET NULL")
        batch.create_index("ix_source_feed_item_id", ["feed_item_id"], unique=False)
        batch.create_unique_constraint("uq_source_notebook_feed_item", ["notebook_id", "feed_item_id"])

    with op.batch_alter_table("feeditem") as batch:
        batch.add_column(sa.Column("indicator_content_sha256", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=""))
        batch.add_column(sa.Column("indicator_extractor_version", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=""))
        batch.add_column(sa.Column("indicator_extracted_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("indicator_extraction_error", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=""))

    op.create_table(
        "feedindicatorcandidate",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("feed_item_id", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("extractor_version", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("raw_value", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("normalized_value", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("ioc_type", sa.Enum("IP_SRC", "IP_DST", "DOMAIN", "HOSTNAME", "URL", "MD5", "SHA1", "SHA256", "EMAIL", "FILENAME", "CVE", name="ioctype"), nullable=True),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("source_excerpt", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("confidence", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("status", sa.Enum("PENDING", "ACCEPTED", "REJECTED", "DUPLICATE", name="feedcandidatestatus"), nullable=False),
        sa.Column("extraction_error", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("extracted_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("decided_by_id", sa.Integer(), nullable=True),
        sa.Column("notebook_id", sa.Integer(), nullable=True),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.Column("ioc_id", sa.Integer(), nullable=True),
        sa.Column("duplicate_ioc_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["decided_by_id"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["duplicate_ioc_id"], ["ioc.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["feed_item_id"], ["feeditem.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ioc_id"], ["ioc.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["notebook_id"], ["notebook.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_id"], ["source.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("feed_item_id", "content_sha256", "extractor_version", "start_offset", "end_offset", "raw_value", name="uq_feed_indicator_candidate_match"),
    )
    for name, columns in (
        ("ix_feedindicatorcandidate_feed_item_id", ["feed_item_id"]),
        ("ix_feedindicatorcandidate_content_sha256", ["content_sha256"]),
        ("ix_feedindicatorcandidate_status", ["status"]),
        ("ix_feedindicatorcandidate_decided_by_id", ["decided_by_id"]),
        ("ix_feedindicatorcandidate_notebook_id", ["notebook_id"]),
        ("ix_feedindicatorcandidate_source_id", ["source_id"]),
        ("ix_feedindicatorcandidate_ioc_id", ["ioc_id"]),
        ("ix_feedindicatorcandidate_duplicate_ioc_id", ["duplicate_ioc_id"]),
    ):
        op.create_index(name, "feedindicatorcandidate", columns, unique=False)


def downgrade() -> None:
    for name in (
        "ix_feedindicatorcandidate_duplicate_ioc_id", "ix_feedindicatorcandidate_ioc_id",
        "ix_feedindicatorcandidate_source_id", "ix_feedindicatorcandidate_notebook_id",
        "ix_feedindicatorcandidate_decided_by_id", "ix_feedindicatorcandidate_status",
        "ix_feedindicatorcandidate_content_sha256", "ix_feedindicatorcandidate_feed_item_id",
    ):
        op.drop_index(name, table_name="feedindicatorcandidate")
    op.drop_table("feedindicatorcandidate")
    with op.batch_alter_table("feeditem") as batch:
        batch.drop_column("indicator_extraction_error")
        batch.drop_column("indicator_extracted_at")
        batch.drop_column("indicator_extractor_version")
        batch.drop_column("indicator_content_sha256")
    with op.batch_alter_table("source") as batch:
        batch.drop_constraint("uq_source_notebook_feed_item", type_="unique")
        batch.drop_index("ix_source_feed_item_id")
        batch.drop_constraint("fk_source_feed_item", type_="foreignkey")
        batch.drop_column("feed_item_id")
