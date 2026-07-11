"""add durable external-work outbox jobs

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-07-11 00:00:00.000000

Publication delivery records stay synchronous in ``disseminationevent``.  This
table is only for retryable operations that leave Iceberg's process: email,
publication webhooks and inbound RSS polling.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "b5c6d7e8f9a0"
down_revision: str | Sequence[str] | None = "a4b5c6d7e8f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ENUMS = (
    sa.Enum(
        "DISSEMINATION_EMAIL",
        "DISSEMINATION_WEBHOOK",
        "RSS_POLL",
        name="jobkind",
    ),
    sa.Enum("PENDING", "RUNNING", "SUCCEEDED", "FAILED", name="jobstatus"),
)


def _enum_type(bind, enum):
    if bind.dialect.name == "postgresql":
        return postgresql.ENUM(*enum.enums, name=enum.name, create_type=False)
    return sa.Enum(*enum.enums, name=enum.name)


def upgrade() -> None:
    bind = op.get_bind()
    for enum in _ENUMS:
        enum.create(bind, checkfirst=True)

    op.create_table(
        "outboxjob",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", _enum_type(bind, _ENUMS[0]), nullable=False),
        sa.Column("status", _enum_type(bind, _ENUMS[1]), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("idempotency_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("leased_at", sa.DateTime(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("lease_token", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("leased_by", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("last_error", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_outboxjob_idempotency_key"),
    )
    with op.batch_alter_table("outboxjob", schema=None) as batch:
        batch.create_index(batch.f("ix_outboxjob_kind"), ["kind"], unique=False)
        batch.create_index(batch.f("ix_outboxjob_status"), ["status"], unique=False)
        batch.create_index(
            batch.f("ix_outboxjob_idempotency_key"),
            ["idempotency_key"],
            unique=False,
        )
        batch.create_index(
            batch.f("ix_outboxjob_available_at"), ["available_at"], unique=False
        )
        batch.create_index(
            batch.f("ix_outboxjob_lease_expires_at"),
            ["lease_expires_at"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("outboxjob", schema=None) as batch:
        batch.drop_index(batch.f("ix_outboxjob_lease_expires_at"))
        batch.drop_index(batch.f("ix_outboxjob_available_at"))
        batch.drop_index(batch.f("ix_outboxjob_idempotency_key"))
        batch.drop_index(batch.f("ix_outboxjob_status"))
        batch.drop_index(batch.f("ix_outboxjob_kind"))
    op.drop_table("outboxjob")
    bind = op.get_bind()
    for enum in reversed(_ENUMS):
        enum.drop(bind, checkfirst=True)
