"""PostgreSQL datastore smoke tests (CI postgres-smoke job).

Skipped unless ``ICEBERG_DATABASE_URL`` points at Postgres — then the conftest
engine fixture builds a real Postgres engine and migrates it to head (running the
dialect-guarded ``postgres_fts`` migration), so these exercise the Postgres path:
the generated ``search_vector`` FTS, faceting, alias resolution and the
stakeholder access-control filter.

Deliberately backend-agnostic on *semantics*: only whole-word matches and
result-set membership are asserted, not SQLite-FTS5 prefix matching or a specific
rank order (Postgres ``websearch_to_tsquery`` + ``ts_rank`` differ by design —
the richer SQLite-specific relevance assertions live in test_search.py)."""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest
from alembic import command
from sqlalchemy import event, text
from sqlmodel import Session, select

from iceberg import db
from iceberg.models import AuditSettings, MISPSettings, ProxySettings, WebhookSettings
from iceberg.services import (
    audit_settings,
    misp_settings,
    proxy_settings,
    webhook_settings,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("ICEBERG_DATABASE_URL", "").startswith("postgresql"),
    reason="Postgres-only smoke (set ICEBERG_DATABASE_URL=postgresql+psycopg://…)",
)


def _migrate(engine, direction: str, revision: str) -> None:
    cfg = db.alembic_config()
    with engine.connect() as conn:
        cfg.attributes["connection"] = conn
        getattr(command, direction)(cfg, revision)


def test_pg_enum_migration_downgrade_and_full_roundtrip(engine):
    """Native types have one owner and leave no orphan at Alembic ``base``."""
    # The fixture is at head. This is the reported failure path: a1f0's
    # downgrade must recreate its retired relation type/table without a duplicate
    # object error.
    _migrate(engine, "downgrade", "dfb25674e675")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT to_regclass('entityrelationship')")).scalar()

    _migrate(engine, "upgrade", "head")
    _migrate(engine, "downgrade", "base")
    with engine.connect() as conn:
        enum_names = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT t.typname FROM pg_type AS t "
                    "JOIN pg_namespace AS n ON n.oid = t.typnamespace "
                    "WHERE n.nspname = current_schema() AND t.typtype = 'e'"
                )
            )
        }
    assert enum_names == set()

    _migrate(engine, "upgrade", "head")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT to_regclass('report')")).scalar()


@pytest.mark.parametrize(
    ("model", "reader"),
    [
        (AuditSettings, audit_settings.get),
        (ProxySettings, proxy_settings.get),
        (MISPSettings, misp_settings.get),
        (WebhookSettings, webhook_settings.get),
    ],
)
def test_pg_concurrent_first_setting_reads_are_conflict_safe(engine, model, reader):
    """PostgreSQL's conflicting first INSERT resolves without breaking sessions."""
    with engine.begin() as conn:
        conn.execute(model.__table__.delete())

    barrier = Barrier(2)
    lock = Lock()
    pauses = 0

    @event.listens_for(engine, "before_cursor_execute")
    def synchronise_initial_reads(
        _conn, _cursor, statement, _parameters, _context, _executemany
    ):
        nonlocal pauses
        if not statement.lstrip().lower().startswith("select"):
            return
        if model.__tablename__ not in statement.lower():
            return
        with lock:
            should_pause = pauses < 2
            if should_pause:
                pauses += 1
        if should_pause:
            barrier.wait(timeout=10)

    def read_once() -> tuple[int | None, int]:
        with Session(engine) as session:
            row = reader(session)
            return row.id, len(session.exec(select(model)).all())

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(read_once) for _ in range(2)]
            results = [future.result(timeout=15) for future in futures]
        assert pauses == 2
        assert results == [(1, 1), (1, 1)]
    finally:
        event.remove(engine, "before_cursor_execute", synchronise_initial_reads)


def _report(client, login, title, body, author="author@example.com"):
    login("ANALYST", email=author)
    nb = client.post("/api/notebooks", json={"title": "nb"}).json()
    return client.post(
        "/api/reports",
        json={"notebook_id": nb["id"], "title": title, "body_md": body},
    ).json()["id"]


def _publish(client, login, rid):
    login("ANALYST", email="author@example.com")
    client.post(f"/api/reports/{rid}/transition", json={"target": "IN_REVIEW"})
    login("REVIEWER", email="rev@example.com")
    client.post(f"/api/reports/{rid}/transition", json={"target": "APPROVED"})
    client.post(f"/api/reports/{rid}/transition", json={"target": "PUBLISHED"})


def _titles(resp):
    return {r["report"]["title"] for r in resp.json()["results"]}


def test_pg_fts_matches_title_and_body(client, login):
    """The generated tsvector column indexes title + body (whole-word match)."""
    _report(client, login, "Spearphishing wave", "Targeting the energy sector.")
    login("ANALYST", email="author@example.com")
    assert client.get("/api/search", params={"q": "spearphishing"}).json()["count"] == 1
    assert client.get("/api/search", params={"q": "energy"}).json()["count"] == 1
    assert client.get("/api/search", params={"q": "nonexistentterm"}).json()["count"] == 0


def test_pg_fts_indexes_judgement_scaffolding(client, login):
    """The tsvector spans the ICD 203 scaffolding and stays current on update
    (the column is GENERATED ALWAYS, so no trigger/reindex needed)."""
    rid = _report(client, login, "Intrusion set update", "Generic narrative body.")
    login("ANALYST", email="author@example.com")
    client.patch(
        f"/api/reports/{rid}",
        json={"key_judgements": "We assess uniquejudgementterm with high confidence."},
    )
    assert client.get("/api/search", params={"q": "uniquejudgementterm"}).json()["count"] == 1
    client.patch(f"/api/reports/{rid}", json={"key_judgements": ""})
    assert client.get("/api/search", params={"q": "uniquejudgementterm"}).json()["count"] == 0


def test_pg_facet_by_intel_level(client, login):
    login("ANALYST", email="author@example.com")
    nb = client.post("/api/notebooks", json={"title": "nb"}).json()
    client.post("/api/reports", json={"notebook_id": nb["id"], "title": "Strat", "intel_level": "STRATEGIC"})
    client.post("/api/reports", json={"notebook_id": nb["id"], "title": "Op", "intel_level": "OPERATIONAL"})
    assert _titles(client.get("/api/search", params={"intel_level": "STRATEGIC"})) == {"Strat"}


def test_pg_alias_query_finds_canonical_entity(client, login):
    # A unique label/alias that isn't in the seeded starter taxonomy — on Postgres
    # the test DB is the (seeded) shared engine, so reusing a starter actor (APT28
    # / "Fancy Bear") would collide on the unique slug.
    login("ADMIN", email="admin@example.com")
    tag = client.post(
        "/api/tags",
        json={"kind": "ACTOR", "label": "SmokeTestActor", "aliases": ["Smoke Phantom"]},
    ).json()
    rid = _report(client, login, "Intrusion set update", "Generic narrative body.")
    login("ANALYST", email="author@example.com")
    client.put(f"/api/reports/{rid}/tags", json={"tag_ids": [tag["id"]]})
    # Body never names the alias; it resolves via the tag (alias-aware search).
    assert _titles(client.get("/api/search", params={"q": "Smoke Phantom"})) == {
        "Intrusion set update"
    }


def test_pg_stakeholder_search_excludes_unpublished(client, login):
    _report(client, login, "Phantom draft", "phantommenace secret material")
    pub = _report(client, login, "Phantom published", "phantommenace public brief")
    _publish(client, login, pub)
    login("STAKEHOLDER", email="nosy@example.com")
    assert _titles(client.get("/api/search", params={"q": "phantommenace"})) == {
        "Phantom published"
    }
    login("ANALYST", email="author@example.com")
    assert client.get("/api/search", params={"q": "phantommenace"}).json()["count"] == 2
