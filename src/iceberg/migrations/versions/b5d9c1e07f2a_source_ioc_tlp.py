"""tlp marking on source and ioc

Revision ID: b5d9c1e07f2a
Revises: a1b2c3d4e5f6
Create Date: 2026-06-27 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "b5d9c1e07f2a"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TLP_VALUES = ("RED", "AMBER_STRICT", "AMBER", "GREEN", "CLEAR")


def _tlp_type(bind):
    """The existing ``tlp`` enum (created by ``report.tlp`` in the initial
    schema). On Postgres reference it with ``create_type=False`` so ADD COLUMN
    doesn't try to recreate the type; on SQLite it's a VARCHAR + CHECK, built
    fresh in batch mode."""
    if bind.dialect.name == "postgresql":
        return postgresql.ENUM(*_TLP_VALUES, name="tlp", create_type=False)
    return sa.Enum(*_TLP_VALUES, name="tlp")


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    for table in ("source", "ioc"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "tlp",
                    _tlp_type(bind),
                    nullable=False,
                    server_default="AMBER",
                )
            )


def downgrade() -> None:
    """Downgrade schema."""
    for table in ("ioc", "source"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_column("tlp")
