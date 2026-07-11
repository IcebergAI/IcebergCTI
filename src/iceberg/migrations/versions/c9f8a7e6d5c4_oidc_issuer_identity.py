"""bind OIDC users by issuer and subject

Revision ID: c9f8a7e6d5c4
Revises: b5d9c1e07f2a
Create Date: 2026-07-11 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op


revision: str = "c9f8a7e6d5c4"
down_revision: str | Sequence[str] | None = "b5d9c1e07f2a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Replace the globally-unique subject with an issuer/subject identity."""
    with op.batch_alter_table("user") as batch:
        batch.add_column(sa.Column("issuer", sa.String(), nullable=True))
        # The old unique index incorrectly treats a subject as globally unique.
        batch.drop_index("ix_user_sub")
        batch.create_index("ix_user_sub", ["sub"], unique=False)
        batch.create_index("ix_user_issuer", ["issuer"], unique=False)
        batch.create_unique_constraint("uq_user_issuer_sub", ["issuer", "sub"])


def downgrade() -> None:
    """Restore the historical globally-unique subject index when possible."""
    # Offline mode has no database to inspect.  Emit the schema operations so
    # release tooling can generate SQL; an online downgrade retains the data
    # safety check before restoring the historical globally-unique index.
    if not context.is_offline_mode():
        duplicate = op.get_bind().execute(
            sa.text(
                'SELECT sub FROM "user" WHERE sub IS NOT NULL '
                "GROUP BY sub HAVING COUNT(*) > 1 LIMIT 1"
            )
        ).first()
        if duplicate is not None:
            raise RuntimeError(
                "Cannot downgrade OIDC issuer identity: a subject is used by multiple issuers"
            )

    with op.batch_alter_table("user") as batch:
        batch.drop_constraint("uq_user_issuer_sub", type_="unique")
        batch.drop_index("ix_user_issuer")
        batch.drop_index("ix_user_sub")
        batch.create_index("ix_user_sub", ["sub"], unique=True)
        batch.drop_column("issuer")
