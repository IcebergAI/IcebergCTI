"""ai settings table

Revision ID: b1c2d3e4f5a6
Revises: a6b7c8d9e0f1
Create Date: 2026-07-21 12:00:00.000000

Admin-editable AI provider configuration (single row, id=1), seeded from the
``ICEBERG_AI_*`` env on first read. Additive — no change to existing rows. See
services/ai_settings.py + models.AISettings.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # SQLModel renders columns as sqlmodel.sql.sqltypes.AutoString


# revision identifiers, used by Alembic.
revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, Sequence[str], None] = 'a6b7c8d9e0f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'aisettings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('backend', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('base_url', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('model', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('aws_region', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('timeout', sa.Float(), nullable=False),
        sa.Column('max_tlp', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('embeddings_enabled', sa.Boolean(), nullable=False),
        sa.Column('embedding_model', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('aisettings')
