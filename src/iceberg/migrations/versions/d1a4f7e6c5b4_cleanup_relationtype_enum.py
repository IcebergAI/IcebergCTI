"""Clean up the orphaned legacy entity-relationship ENUM.

Revision ID: d1a4f7e6c5b4
Revises: d1e2f3a4b5c6
Create Date: 2026-07-11 00:00:00.000000

``a1f0c2d3e4b5`` removed ``entityrelationship`` before its PostgreSQL native
``relationtype`` enum was explicitly owned. Existing databases have therefore
already run that historical upgrade and retain an orphan type; repairing the
historical scripts alone cannot revisit them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "d1a4f7e6c5b4"
down_revision: str | Sequence[str] | None = "d1e2f3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RELATION_TYPE = sa.Enum(
    "USES",
    "ATTRIBUTED_TO",
    "VARIANT_OF",
    "TARGETS",
    "RELATED_TO",
    name="relationtype",
)


def upgrade() -> None:
    """Remove the type left behind by the retired entityrelationship table."""
    _RELATION_TYPE.drop(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    """No type is needed at the current historical revision.

    If a later downgrade crosses ``a1f0c2d3e4b5``, that migration recreates the
    type immediately before recreating its table.
    """
