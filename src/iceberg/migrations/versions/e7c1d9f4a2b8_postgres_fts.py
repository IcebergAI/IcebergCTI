"""postgres full-text search (tsvector generated column + GIN index)

Revision ID: e7c1d9f4a2b8
Revises: c8f0a1b2d3e4
Create Date: 2026-06-22

PostgreSQL has no SQLite FTS5 equivalent, so on Postgres the report search index
is a DB-maintained generated ``tsvector`` column over the same text columns the
SQLite ``report_fts`` virtual table mirrors (title + body + the ICD 203 judgement
scaffolding), with a GIN index. Like the baseline migration's FTS block, the
whole thing is **dialect-guarded**: on SQLite (the dev/test default) and any
other backend it is a no-op — search there stays the FTS5 path created by the
baseline. The generated column is always current (no rebuild step needed).

See ``services/search.py`` (``search_reports`` branches on the dialect) and
CLAUDE.md *Database & migrations*.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "e7c1d9f4a2b8"
down_revision = "c8f0a1b2d3e4"
branch_labels = None
depends_on = None

# Mirrors services/search._FTS_COLS — keep in sync if the indexed text columns
# change (the SQLite report_fts table indexes the same set).
_TSVECTOR_EXPR = (
    "to_tsvector('english', "
    "coalesce(title, '') || ' ' || "
    "coalesce(body_md, '') || ' ' || "
    "coalesce(key_judgements, '') || ' ' || "
    "coalesce(key_assumptions, '') || ' ' || "
    "coalesce(intelligence_gaps, ''))"
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return  # SQLite (and others) keep the FTS5 path from the baseline migration.
    op.execute(
        "ALTER TABLE report ADD COLUMN search_vector tsvector "
        f"GENERATED ALWAYS AS ({_TSVECTOR_EXPR}) STORED"
    )
    op.execute(
        "CREATE INDEX ix_report_search_vector ON report USING GIN (search_vector)"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS ix_report_search_vector")
    op.execute("ALTER TABLE report DROP COLUMN IF EXISTS search_vector")
