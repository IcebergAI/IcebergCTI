"""drop entity relationships

Removes the EntityRelationship knowledge-graph table: corporate CTI teams manage
entity relationships in a dedicated TIP, so the in-app graph was retired.

Revision ID: a1f0c2d3e4b5
Revises: dfb25674e675
Create Date: 2026-06-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a1f0c2d3e4b5'
down_revision: Union[str, Sequence[str], None] = 'dfb25674e675'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_RELATION_VALUES = ('USES', 'ATTRIBUTED_TO', 'VARIANT_OF', 'TARGETS', 'RELATED_TO')
_RELATION_TYPE = sa.Enum(*_RELATION_VALUES, name='relationtype')


def _relation_type(bind):
    if bind.dialect.name == 'postgresql':
        return postgresql.ENUM(*_RELATION_VALUES, name='relationtype', create_type=False)
    return sa.Enum(*_RELATION_VALUES, name='relationtype')


def upgrade() -> None:
    """Drop the entityrelationship table and its indexes."""
    with op.batch_alter_table('entityrelationship', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_entityrelationship_target_tag_id'))
        batch_op.drop_index(batch_op.f('ix_entityrelationship_source_tag_id'))

    op.drop_table('entityrelationship')
    # This migration permanently removes the table, so it is also the final
    # owner of its native PostgreSQL ENUM.
    _RELATION_TYPE.drop(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    """Recreate the entityrelationship table (data is not restored)."""
    bind = op.get_bind()
    _RELATION_TYPE.create(bind, checkfirst=True)
    op.create_table('entityrelationship',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('source_tag_id', sa.Integer(), nullable=False),
    sa.Column('target_tag_id', sa.Integer(), nullable=False),
    sa.Column('relation_type', _relation_type(bind), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['source_tag_id'], ['tag.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['target_tag_id'], ['tag.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('source_tag_id', 'target_tag_id', 'relation_type', name='uq_entity_relationship')
    )
    with op.batch_alter_table('entityrelationship', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_entityrelationship_source_tag_id'), ['source_tag_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_entityrelationship_target_tag_id'), ['target_tag_id'], unique=False)
