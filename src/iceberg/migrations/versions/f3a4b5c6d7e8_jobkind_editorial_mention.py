"""add EDITORIAL_MENTION to the outbox job-kind enum

Revision ID: f3a4b5c6d7e8
Revises: f2a3b4c5d6e7
Create Date: 2026-08-20 00:45:00.000000

``JobKind.EDITORIAL_MENTION`` arrived with the editorial-comment threads, but
the job-kind enum is a **native type** on PostgreSQL and still held only the
three kinds the outbox shipped with.  SQLite stores the column as a plain
VARCHAR with no constraint, so the whole suite passes while the first mention
enqueued against PostgreSQL — the required datastore for every deployment —
fails on an invalid enum value.

``ALTER TYPE … ADD VALUE`` cannot be used in the same transaction that adds it,
so it runs in an autocommit block.  ``IF NOT EXISTS`` keeps a re-run harmless.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "f3a4b5c6d7e8"
down_revision: str | Sequence[str] | None = "f2a3b4c5d6e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # Every other dialect stores the column as text; nothing to widen.
        return
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE jobkind ADD VALUE IF NOT EXISTS 'EDITORIAL_MENTION'")


def downgrade() -> None:
    """Deliberately empty.

    PostgreSQL cannot drop a value from an enum, and rebuilding the type would
    have to rewrite every row that already uses it. Leaving an unused value in
    place costs nothing; the rollback boundary is documented in RELEASING.md.
    """
