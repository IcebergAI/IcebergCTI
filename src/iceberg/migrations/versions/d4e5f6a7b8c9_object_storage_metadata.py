"""Add immutable persistent-object metadata.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_object_columns(table: str, *, rendered: bool = False) -> None:
    op.add_column(
        table,
        sa.Column(
            "storage_backend", sa.String(), nullable=False, server_default="local"
        ),
    )
    op.add_column(
        table,
        sa.Column("storage_key", sa.String(), nullable=False, server_default=""),
    )
    op.add_column(
        table,
        sa.Column("content_sha256", sa.String(), nullable=False, server_default=""),
    )
    if rendered:
        op.add_column(
            table,
            sa.Column("file_size", sa.Integer(), nullable=False, server_default="0"),
        )
    op.add_column(
        table, sa.Column("storage_finalized_at", sa.DateTime(), nullable=True)
    )
    op.create_index(f"ix_{table}_storage_backend", table, ["storage_backend"])
    op.create_index(f"ix_{table}_storage_key", table, ["storage_key"])
    op.create_index(f"ix_{table}_content_sha256", table, ["content_sha256"])
    op.create_index(
        f"uq_{table}_storage_object",
        table,
        ["storage_backend", "storage_key"],
        unique=True,
        sqlite_where=sa.text("storage_key <> ''"),
        postgresql_where=sa.text("storage_key <> ''"),
    )


def upgrade() -> None:
    _add_object_columns("attachment")
    _add_object_columns("figure")
    _add_object_columns("renderedproduct", rendered=True)
    # Existing local attachment/figure keys already are immutable UUID names.
    # Digests and finalized timestamps are deliberately left empty until the
    # resumable storage migration has verified the actual bytes.
    op.execute("UPDATE attachment SET storage_key = stored_filename")
    op.execute("UPDATE figure SET storage_key = stored_filename")
    op.create_table(
        "storagedeletion",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("backend", sa.String(), nullable=False),
        sa.Column("object_key", sa.String(), nullable=False),
        sa.Column("content_sha256", sa.String(), nullable=False, server_default=""),
        sa.Column("status", sa.String(), nullable=False, server_default="PENDING"),
        sa.Column("not_before", sa.DateTime(), nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("leased_at", sa.DateTime(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("lease_token", sa.String(), nullable=False, server_default=""),
        sa.Column("leased_by", sa.String(), nullable=False, server_default=""),
        sa.Column("last_error", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "kind", "backend", "object_key", name="uq_storage_deletion_object"
        ),
        sa.CheckConstraint(
            "kind IN ('attachment', 'figure', 'render')",
            name="ck_storage_deletion_kind",
        ),
        sa.CheckConstraint(
            "backend IN ('local', 's3')", name="ck_storage_deletion_backend"
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="ck_storage_deletion_status",
        ),
        sa.CheckConstraint(
            "length(content_sha256) IN (0, 64)",
            name="ck_storage_deletion_digest_length",
        ),
    )
    for column in (
        "kind",
        "backend",
        "status",
        "not_before",
        "available_at",
        "lease_expires_at",
    ):
        op.create_index(f"ix_storagedeletion_{column}", "storagedeletion", [column])
    op.create_table(
        "storagemaintenancestate",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("task", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False, server_default="default"),
        sa.Column("cursor", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("last_result", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(), nullable=False, server_default="SUCCESS"),
        sa.Column("last_run_at", sa.DateTime(), nullable=False),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('SUCCESS', 'FAILED')",
            name="ck_storage_maintenance_status",
        ),
    )
    op.create_index(
        "ix_storagemaintenancestate_task", "storagemaintenancestate", ["task"]
    )


def _drop_object_columns(table: str, *, rendered: bool = False) -> None:
    op.drop_index(f"uq_{table}_storage_object", table_name=table)
    op.drop_index(f"ix_{table}_content_sha256", table_name=table)
    op.drop_index(f"ix_{table}_storage_key", table_name=table)
    op.drop_index(f"ix_{table}_storage_backend", table_name=table)
    op.drop_column(table, "storage_finalized_at")
    if rendered:
        op.drop_column(table, "file_size")
    op.drop_column(table, "content_sha256")
    op.drop_column(table, "storage_key")
    op.drop_column(table, "storage_backend")


def downgrade() -> None:
    connection = op.get_bind()
    object_tables = {
        name: sa.table(
            name,
            sa.column("storage_backend"),
            sa.column("storage_key"),
            sa.column("stored_filename"),
        )
        for name in ("attachment", "figure")
    }
    rendered_table = sa.table(
        "renderedproduct",
        sa.column("storage_backend"),
        sa.column("storage_key"),
        sa.column("pdf_path"),
    )
    remote_references = sum(
        int(
            connection.execute(
                sa.select(sa.func.count())
                .select_from(table)
                .where(table.c.storage_backend != "local")
            ).scalar()
            or 0
        )
        for table in (*object_tables.values(), rendered_table)
    )
    pending_deletions = int(
        connection.execute(
            sa.text(
                "SELECT count(*) FROM storagedeletion "
                "WHERE status NOT IN ('SUCCEEDED', 'CANCELLED')"
            )
        ).scalar()
        or 0
    )
    incompatible_local = sum(
        int(
            connection.execute(
                sa.select(sa.func.count())
                .select_from(table)
                .where(
                    table.c.storage_backend == "local",
                    table.c.storage_key != table.c.stored_filename,
                )
            ).scalar()
            or 0
        )
        for table in object_tables.values()
    )
    incompatible_local += int(
        connection.execute(
            sa.select(sa.func.count())
            .select_from(rendered_table)
            .where(
                rendered_table.c.storage_backend == "local",
                rendered_table.c.storage_key != "",
                rendered_table.c.pdf_path == "",
            )
        ).scalar()
        or 0
    )
    if remote_references or pending_deletions or incompatible_local:
        raise RuntimeError(
            "Cannot downgrade object-storage metadata while remote references, "
            "unfinished storage deletions, or legacy-incompatible local rows exist"
        )
    op.drop_index(
        "ix_storagemaintenancestate_task", table_name="storagemaintenancestate"
    )
    op.drop_table("storagemaintenancestate")
    op.drop_table("storagedeletion")
    _drop_object_columns("renderedproduct", rendered=True)
    _drop_object_columns("figure")
    _drop_object_columns("attachment")
