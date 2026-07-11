"""make ATT&CK tactic metadata first class

Revision ID: a4b5c6d7e8f9
Revises: f3e4d5c6b7a8
Create Date: 2026-07-11 00:00:00.000000

Existing technique tags historically stored one tactic in ``description``.
Retain that human text but seed the new structured list whenever it matches a
known Enterprise ATT&CK tactic.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op


revision: str = "a4b5c6d7e8f9"
down_revision: str | Sequence[str] | None = "f3e4d5c6b7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TACTICS = {
    "reconnaissance": "Reconnaissance",
    "resource development": "Resource Development",
    "initial access": "Initial Access",
    "execution": "Execution",
    "persistence": "Persistence",
    "privilege escalation": "Privilege Escalation",
    "defense evasion": "Defense Evasion",
    "credential access": "Credential Access",
    "discovery": "Discovery",
    "lateral movement": "Lateral Movement",
    "collection": "Collection",
    "command and control": "Command and Control",
    "exfiltration": "Exfiltration",
    "impact": "Impact",
}


def _technique_predicate(
    tag: sa.TableClause, dialect_name: str
) -> sa.ColumnElement[bool]:
    if dialect_name == "postgresql":
        # ``tag.kind`` is the native ``tagkind`` enum. An ordinary string bind
        # is rendered as VARCHAR by psycopg and PostgreSQL has no enum=varchar
        # operator, so make the controlled enum literal's type explicit.
        return sa.text("tag.kind = 'TECHNIQUE'::tagkind")
    return tag.c.kind == "TECHNIQUE"


def upgrade() -> None:
    with op.batch_alter_table("tag") as batch:
        batch.add_column(
            sa.Column("attack_tactics", sa.JSON(), nullable=False, server_default="[]")
        )

    # Offline SQL generation has no rows to inspect.  Online upgrades preserve
    # the established starter-taxonomy convention without guessing at arbitrary
    # human descriptions.
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    tag = sa.table(
        "tag",
        sa.column("id", sa.Integer()),
        sa.column("kind", sa.String()),
        sa.column("description", sa.String()),
        sa.column("attack_tactics", sa.JSON()),
    )
    rows = bind.execute(
        sa.select(tag.c.id, tag.c.description).where(
            _technique_predicate(tag, bind.dialect.name)
        )
    ).mappings()
    for row in rows:
        tactic = _TACTICS.get((row["description"] or "").strip().lower())
        if tactic:
            bind.execute(
                tag.update()
                .where(tag.c.id == row["id"])
                .values(attack_tactics=[tactic])
            )


def downgrade() -> None:
    with op.batch_alter_table("tag") as batch:
        batch.drop_column("attack_tactics")
