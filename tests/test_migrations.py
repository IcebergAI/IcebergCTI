"""Alembic migrations: a fresh `upgrade head` builds the full schema + the FTS5
objects, the models don't drift from the baseline migration, and the migration
round-trips (downgrade base -> upgrade head).

Runs against a throwaway temp-file SQLite DB (the rest of the suite uses the fast
in-memory engine + create_all). The fixture clears the settings cache on the way
in and out so the temp URL doesn't leak into other tests.
"""

import pytest
from alembic import command
from sqlalchemy import column, create_engine, String, table, text

from iceberg import db as db_mod
from iceberg.config import get_settings
from iceberg.migrations.versions.a4b5c6d7e8f9_attack_tactics import (
    _technique_predicate,
)


def _objects(url: str, kind: str) -> set[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type = :k"), {"k": kind}
            ).all()
        return {r[0] for r in rows}
    finally:
        engine.dispose()


@pytest.fixture
def migrated_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'iceberg.db'}"
    monkeypatch.setenv("ICEBERG_DATABASE_URL", url)
    get_settings.cache_clear()
    yield url, db_mod.alembic_config()
    get_settings.cache_clear()  # restore the suite's in-memory settings


def test_upgrade_head_builds_schema_and_fts(migrated_db):
    url, cfg = migrated_db
    command.upgrade(cfg, "head")

    tables = _objects(url, "table")
    # Representative model tables across the core + link + analytic models.
    assert {"user", "notebook", "report", "tag", "reportsource", "diamondmodel"} <= tables
    # The FTS5 virtual table + its sync triggers came from the migration's DDL.
    assert "report_fts" in tables
    assert {"report_ai", "report_ad", "report_au"} <= _objects(url, "trigger")


def test_no_model_migration_drift(migrated_db):
    """Guard: if a model changes without a matching migration, `alembic check`
    (autogenerate diff, FTS objects excluded in env.py) raises and fails here."""
    _, cfg = migrated_db
    command.upgrade(cfg, "head")
    command.check(cfg)


def test_downgrade_then_upgrade_roundtrips(migrated_db):
    url, cfg = migrated_db
    command.upgrade(cfg, "head")

    command.downgrade(cfg, "base")
    assert "report" not in _objects(url, "table")
    assert "report_fts" not in _objects(url, "table")

    command.upgrade(cfg, "head")
    assert "report" in _objects(url, "table")
    assert "report_fts" in _objects(url, "table")


def test_attack_tactic_backfill_casts_native_postgres_enum():
    tag = table("tag", column("kind", String()))

    predicate = str(_technique_predicate(tag, "postgresql"))

    assert predicate == "tag.kind = 'TECHNIQUE'::tagkind"
