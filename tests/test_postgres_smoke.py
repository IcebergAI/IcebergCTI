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

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("ICEBERG_DATABASE_URL", "").startswith("postgresql"),
    reason="Postgres-only smoke (set ICEBERG_DATABASE_URL=postgresql+psycopg://…)",
)


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
    login("ADMIN", email="admin@example.com")
    tag = client.post(
        "/api/tags",
        json={"kind": "ACTOR", "label": "APT28", "aliases": ["Fancy Bear"]},
    ).json()
    rid = _report(client, login, "Intrusion set update", "Generic narrative body.")
    login("ANALYST", email="author@example.com")
    client.put(f"/api/reports/{rid}/tags", json={"tag_ids": [tag["id"]]})
    # Body never names the alias; it resolves via the tag (alias-aware search).
    assert _titles(client.get("/api/search", params={"q": "Fancy Bear"})) == {
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
