"""isolated guided demo workspaces

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-08-12 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: str | Sequence[str] | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SQLITE_FTS_TRIGGERS = (
    """CREATE TRIGGER IF NOT EXISTS report_ai AFTER INSERT ON report BEGIN
        INSERT INTO report_fts(rowid, title, body_md, key_judgements, key_assumptions, intelligence_gaps)
        VALUES (new.id, new.title, new.body_md, new.key_judgements, new.key_assumptions, new.intelligence_gaps);
    END""",
    """CREATE TRIGGER IF NOT EXISTS report_ad AFTER DELETE ON report BEGIN
        INSERT INTO report_fts(report_fts, rowid, title, body_md, key_judgements, key_assumptions, intelligence_gaps)
        VALUES ('delete', old.id, old.title, old.body_md, old.key_judgements, old.key_assumptions, old.intelligence_gaps);
    END""",
    """CREATE TRIGGER IF NOT EXISTS report_au AFTER UPDATE ON report BEGIN
        INSERT INTO report_fts(report_fts, rowid, title, body_md, key_judgements, key_assumptions, intelligence_gaps)
        VALUES ('delete', old.id, old.title, old.body_md, old.key_judgements, old.key_assumptions, old.intelligence_gaps);
        INSERT INTO report_fts(rowid, title, body_md, key_judgements, key_assumptions, intelligence_gaps)
        VALUES (new.id, new.title, new.body_md, new.key_judgements, new.key_assumptions, new.intelligence_gaps);
    END""",
)


def _restore_sqlite_fts_triggers() -> None:
    if op.get_bind().dialect.name == "sqlite":
        for statement in _SQLITE_FTS_TRIGGERS:
            op.execute(statement)


def upgrade() -> None:
    op.create_table(
        "demoworkspace",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("stakeholder_id", sa.Integer(), nullable=True),
        sa.Column(
            "join_code_digest", sqlmodel.sql.sqltypes.AutoString(), nullable=False
        ),
        sa.Column("join_expires_at", sa.DateTime(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("scenario_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["stakeholder_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", name="uq_demo_workspace_owner"),
        sa.UniqueConstraint("public_id", name="uq_demo_workspace_public_id"),
    )
    op.create_index("ix_demoworkspace_owner_id", "demoworkspace", ["owner_id"])
    op.create_index("ix_demoworkspace_public_id", "demoworkspace", ["public_id"])
    op.create_index(
        "ix_demoworkspace_stakeholder_id", "demoworkspace", ["stakeholder_id"]
    )

    for table in ("audiencegroup", "notebook", "report", "requirement"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(
                sa.Column("demo_workspace_id", sa.Integer(), nullable=True)
            )
            batch.create_foreign_key(
                f"fk_{table}_demo_workspace_id",
                "demoworkspace",
                ["demo_workspace_id"],
                ["id"],
            )
            batch.create_index(
                f"ix_{table}_demo_workspace_id", ["demo_workspace_id"], unique=True
            )
    # SQLite batch mode recreates tables and therefore drops their triggers.
    _restore_sqlite_fts_triggers()


def downgrade() -> None:
    for table in ("requirement", "report", "notebook", "audiencegroup"):
        with op.batch_alter_table(table) as batch:
            batch.drop_index(f"ix_{table}_demo_workspace_id")
            batch.drop_constraint(f"fk_{table}_demo_workspace_id", type_="foreignkey")
            batch.drop_column("demo_workspace_id")
    _restore_sqlite_fts_triggers()
    op.drop_index("ix_demoworkspace_stakeholder_id", table_name="demoworkspace")
    op.drop_index("ix_demoworkspace_public_id", table_name="demoworkspace")
    op.drop_index("ix_demoworkspace_owner_id", table_name="demoworkspace")
    op.drop_table("demoworkspace")
