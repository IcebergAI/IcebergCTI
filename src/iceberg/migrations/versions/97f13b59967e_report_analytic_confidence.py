"""report analytic_confidence

Revision ID: 97f13b59967e
Revises: f6b4f974b72e
Create Date: 2026-06-15 20:26:07.780376

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '97f13b59967e'
down_revision: Union[str, Sequence[str], None] = 'f6b4f974b72e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Postgres needs the ENUM type created before the ALTER TABLE ADD COLUMN
# references it; on SQLite (VARCHAR enums) .create()/.drop() are no-ops.
_CONFIDENCE = sa.Enum('LOW', 'MODERATE', 'HIGH', name='analyticconfidence')


def upgrade() -> None:
    """Upgrade schema."""
    _CONFIDENCE.create(op.get_bind(), checkfirst=True)
    with op.batch_alter_table('report', schema=None) as batch_op:
        batch_op.add_column(sa.Column('analytic_confidence', sa.Enum('LOW', 'MODERATE', 'HIGH', name='analyticconfidence'), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('report', schema=None) as batch_op:
        batch_op.drop_column('analytic_confidence')
    _CONFIDENCE.drop(op.get_bind(), checkfirst=True)
