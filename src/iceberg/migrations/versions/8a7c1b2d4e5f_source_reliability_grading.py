"""source reliability grading

Revision ID: 8a7c1b2d4e5f
Revises: 11f71f3875b9
Create Date: 2026-06-13 16:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = "8a7c1b2d4e5f"
down_revision: Union[str, Sequence[str], None] = "11f71f3875b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Postgres needs the ENUM types created before they're referenced in ALTER TABLE
# ADD COLUMN (unlike CREATE TABLE, add_column doesn't auto-create them). On SQLite
# enums are VARCHAR, so .create()/.drop() are no-ops. Kept identical to the
# add_column definitions below.
_ENUMS = (
    sa.Enum("A", "B", "C", "D", "E", "F", name="sourcereliability"),
    sa.Enum(
        "CONFIRMED",
        "PROBABLY_TRUE",
        "POSSIBLY_TRUE",
        "DOUBTFULLY_TRUE",
        "IMPROBABLE",
        "CANNOT_BE_JUDGED",
        name="sourcecredibility",
    ),
    sa.Enum("UNGRADED", "AUTO", "MANUAL", name="sourcegradingorigin"),
)


def _enum_type(bind, enum):
    if bind.dialect.name == "postgresql":
        return postgresql.ENUM(*enum.enums, name=enum.name, create_type=False)
    return sa.Enum(*enum.enums, name=enum.name)


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    for enum in _ENUMS:
        enum.create(bind, checkfirst=True)
    with op.batch_alter_table("source", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "reliability",
                _enum_type(bind, _ENUMS[0]),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "credibility",
                _enum_type(bind, _ENUMS[1]),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "grading_origin",
                _enum_type(bind, _ENUMS[2]),
                server_default="UNGRADED",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column("grading_engine", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("grading_rationale", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("grading_error", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default="")
        )
        batch_op.add_column(sa.Column("graded_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("source", schema=None) as batch_op:
        batch_op.drop_column("graded_at")
        batch_op.drop_column("grading_error")
        batch_op.drop_column("grading_rationale")
        batch_op.drop_column("grading_engine")
        batch_op.drop_column("grading_origin")
        batch_op.drop_column("credibility")
        batch_op.drop_column("reliability")
    bind = op.get_bind()
    for enum in _ENUMS:
        enum.drop(bind, checkfirst=True)
