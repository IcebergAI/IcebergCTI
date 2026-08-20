"""Permission-safe, observable related-product indexing (#311).

The security property under test is a *side channel* one: a product the reader
cannot access must not change what they see — not the number of results, not the
ranking, not a title or a score, and not the shape of an error.
"""

import pytest
from sqlmodel import Session, select

from iceberg.config import get_settings
from iceberg.models import Report, ReportEmbedding
from iceberg.services import related


@pytest.fixture
def lexical_only(monkeypatch):
    """Run with the vector provider switched off."""

    monkeypatch.setattr(get_settings(), "related_backend", "none")
    yield


def _report(client, login, *, title, body, publish=True, group_ids=None):
    login("ANALYST", email="author@example.com")
    notebook = client.post("/api/notebooks", json={"title": "Related"}).json()
    report = client.post(
        "/api/reports",
        json={"notebook_id": notebook["id"], "title": title, "body_md": body},
    ).json()
    if group_ids:
        login("ADMIN", email="admin@example.com")
        assert client.put(
            f"/api/audience-groups/reports/{report['id']}", json={"group_ids": group_ids}
        ).status_code == 200
    if publish:
        login("ANALYST", email="author@example.com")
        client.post(f"/api/reports/{report['id']}/transition", json={"target": "IN_REVIEW"})
        login("REVIEWER", email="rev@example.com")
        client.post(f"/api/reports/{report['id']}/transition", json={"target": "APPROVED"})
        assert client.post(
            f"/api/reports/{report['id']}/transition", json={"target": "PUBLISHED"}
        ).status_code == 200
    return report


def _group(client, login, name, member_ids):
    login("ADMIN", email="admin@example.com")
    return client.post(
        "/api/audience-groups", json={"name": name, "member_user_ids": member_ids}
    ).json()["id"]


def _stakeholder(client, login, email):
    login("STAKEHOLDER", email=email, name=email.split("@")[0])
    return client.get("/api/me").json()["id"]


def _related(client, login, report_id, *, as_role="ANALYST", email="author@example.com"):
    login(as_role, email=email)
    resp = client.get(f"/api/reports/{report_id}/related")
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# Authorization side channels
# --------------------------------------------------------------------------- #
def test_an_invisible_product_changes_nothing_a_stakeholder_sees(client, login):
    """A near-identical product behind a group the reader is not in must not
    appear, nor displace, nor renumber anything they can see."""

    stakeholder = _stakeholder(client, login, "sh@example.com")
    cohort = _group(client, login, "Cleared", [])
    _group(client, login, "Reader cohort", [stakeholder])

    subject = _report(client, login, title="Subject", body="phishing campaign infrastructure")
    visible = _report(client, login, title="Visible", body="phishing campaign infrastructure")
    baseline = _related(client, login, subject["id"], as_role="STAKEHOLDER", email="sh@example.com")

    _report(
        client, login, title="Restricted", body="phishing campaign infrastructure",
        group_ids=[cohort],
    )
    after = _related(client, login, subject["id"], as_role="STAKEHOLDER", email="sh@example.com")

    assert [item["report"]["id"] for item in baseline["results"]] == [visible["id"]]
    assert baseline["results"] == after["results"]
    titles = {item["report"]["title"] for item in after["results"]}
    assert "Restricted" not in titles


def test_a_reader_removed_from_a_group_loses_access_on_the_next_request(client, login):
    """Permission changes need no reindex — access is evaluated per request."""

    stakeholder = _stakeholder(client, login, "sh@example.com")
    cohort = _group(client, login, "Cleared", [stakeholder])
    subject = _report(client, login, title="Subject", body="ransomware affiliate tooling")
    scoped = _report(
        client, login, title="Scoped", body="ransomware affiliate tooling", group_ids=[cohort]
    )

    before = _related(client, login, subject["id"], as_role="STAKEHOLDER", email="sh@example.com")
    assert [item["report"]["id"] for item in before["results"]] == [scoped["id"]]

    login("ADMIN", email="admin@example.com")
    assert client.put(
        f"/api/audience-groups/{cohort}/members", json={"member_user_ids": []}
    ).status_code == 200

    after = _related(client, login, subject["id"], as_role="STAKEHOLDER", email="sh@example.com")
    assert after["results"] == []


def test_a_draft_product_is_never_related(client, login):
    subject = _report(client, login, title="Subject", body="credential harvesting kit")
    _report(client, login, title="Draft", body="credential harvesting kit", publish=False)

    assert _related(client, login, subject["id"])["results"] == []


# --------------------------------------------------------------------------- #
# Index provenance and staleness
# --------------------------------------------------------------------------- #
def _entry(engine, report_id):
    with Session(engine) as session:
        return session.get(ReportEmbedding, report_id)


def test_an_index_entry_records_what_produced_it(client, login, engine):
    report = _report(client, login, title="Provenance", body="beaconing over dns")

    entry = _entry(engine, report["id"])
    assert entry is not None
    assert (entry.backend, entry.model, entry.model_version) == ("local", "hash-v1", "1")
    assert entry.dimensions == 32
    assert entry.source_version > 0
    assert len(entry.content_sha256) == 64
    assert entry.updated_at is not None


def test_an_edited_product_reads_as_stale_until_reindexed(client, login, engine):
    report = _report(client, login, title="Edited", body="original body")

    with Session(engine) as session:
        row = session.get(Report, report["id"])
        row.body_md = "an entirely different body about supply chain compromise"
        session.add(row)
        session.commit()
        session.refresh(row)
        provider = related.get_provider()
        assert related.is_stale(session.get(ReportEmbedding, report["id"]), row, provider)
        assert related.index_health(session)["stale"] == 1

        result = related.reindex(session)
        assert result["indexed"] == 1 and result["pending"] == 0
        session.refresh(row)
        assert not related.is_stale(session.get(ReportEmbedding, report["id"]), row, provider)


def test_a_model_version_change_invalidates_every_entry(client, login, engine, monkeypatch):
    _report(client, login, title="First", body="one")
    _report(client, login, title="Second", body="two")

    monkeypatch.setattr(related.LocalHashProvider, "version", "2")
    with Session(engine) as session:
        assert related.index_health(session)["stale"] == 2
        assert related.reindex(session)["indexed"] == 2
        assert related.index_health(session)["stale"] == 0
        entries = session.exec(select(ReportEmbedding)).all()
        assert {entry.model_version for entry in entries} == {"2"}


def test_a_stale_entry_is_not_served(client, login, engine):
    """Ranking by a superseded revision would rank a product by text it no
    longer contains, so a stale entry is skipped rather than trusted."""

    subject = _report(client, login, title="Subject", body="botnet takedown coordination")
    other = _report(client, login, title="Other", body="botnet takedown coordination")
    assert [item["report"]["id"] for item in _related(client, login, subject["id"])["results"]] == [
        other["id"]
    ]

    with Session(engine) as session:
        row = session.get(Report, other["id"])
        row.body_md = "unrelated commentary"
        session.add(row)
        session.commit()

    assert _related(client, login, subject["id"])["results"] == []


# --------------------------------------------------------------------------- #
# Reindex lifecycle
# --------------------------------------------------------------------------- #
def test_reindexing_is_bounded_resumable_and_idempotent(client, login, engine, monkeypatch):
    for index in range(3):
        _report(client, login, title=f"Product {index}", body=f"body {index}")
    monkeypatch.setattr(related.LocalHashProvider, "version", "next")

    with Session(engine) as session:
        first = related.reindex(session, batch=2)
        assert (first["indexed"], first["pending"]) == (2, 1)
        second = related.reindex(session, batch=2)
        assert (second["indexed"], second["pending"]) == (1, 0)
        # A repeat pass has nothing left to do.
        third = related.reindex(session, batch=2)
        assert (third["indexed"], third["pending"], third["up_to_date"]) == (0, 0, 3)


def test_an_entry_for_an_unpublished_product_is_pruned(client, login, engine):
    report = _report(client, login, title="Withdrawn", body="body")

    with Session(engine) as session:
        row = session.get(Report, report["id"])
        row.status = "APPROVED"
        session.add(row)
        session.commit()

        assert related.index_health(session)["orphans"] == 1
        assert related.reindex(session)["removed"] == 1
        assert session.get(ReportEmbedding, report["id"]) is None


def test_deleting_a_product_removes_its_entry(client, login, engine):
    report = _report(client, login, title="Deleted", body="body")

    with Session(engine) as session:
        session.delete(session.get(Report, report["id"]))
        session.commit()
        assert session.get(ReportEmbedding, report["id"]) is None


# --------------------------------------------------------------------------- #
# Lexical fallback
# --------------------------------------------------------------------------- #
def test_related_products_still_work_with_the_provider_disabled(client, login, lexical_only):
    subject = _report(
        client, login, title="Subject", body="spearphishing lure targeting logistics operators"
    )
    match = _report(
        client, login, title="Match", body="spearphishing lure targeting logistics operators"
    )
    _report(client, login, title="Unrelated", body="quarterly budget planning notes")

    payload = _related(client, login, subject["id"])

    assert payload["retrieval"] == "lexical"
    assert [item["report"]["id"] for item in payload["results"]] == [match["id"]]
    assert payload["results"][0]["method"] == "lexical"


def test_the_disabled_provider_indexes_nothing(client, login, engine, lexical_only):
    _report(client, login, title="Unindexed", body="body")

    with Session(engine) as session:
        assert session.exec(select(ReportEmbedding)).all() == []
        health = related.index_health(session)
        assert health["enabled"] is False
        assert health["retrieval"] == "lexical"
        assert health["pending"] == 0


def test_the_lexical_fallback_keeps_the_same_access_rules(client, login, lexical_only):
    stakeholder = _stakeholder(client, login, "sh@example.com")
    cohort = _group(client, login, "Cleared", [])
    _group(client, login, "Reader cohort", [stakeholder])
    subject = _report(client, login, title="Subject", body="wiper malware deployment tradecraft")
    _report(
        client, login, title="Restricted", body="wiper malware deployment tradecraft",
        group_ids=[cohort],
    )

    payload = _related(client, login, subject["id"], as_role="STAKEHOLDER", email="sh@example.com")
    assert payload["results"] == []


# --------------------------------------------------------------------------- #
# Relevance + operator health
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("backend", ["local", "none"])
def test_golden_relevance_ranks_the_substantively_related_product_first(
    client, login, monkeypatch, backend
):
    monkeypatch.setattr(get_settings(), "related_backend", backend)
    subject = _report(
        client,
        login,
        title="Ransomware affiliate targeting hospitals",
        body="The affiliate deploys ransomware against hospital networks after "
        "buying access from an initial access broker.",
    )
    expected = _report(
        client,
        login,
        title="Initial access brokers selling hospital network access",
        body="Brokers advertise hospital network access to ransomware affiliates.",
    )
    _report(
        client,
        login,
        title="Quarterly cloud spend review",
        body="Finance summarises cloud spend and licence renewals for the quarter.",
    )

    results = _related(client, login, subject["id"])["results"]
    assert results, backend
    assert results[0]["report"]["id"] == expected["id"], backend


def test_index_health_is_admin_only_and_describes_the_index(client, login):
    _report(client, login, title="Indexed", body="body")

    login("ADMIN", email="admin@example.com")
    health = client.get("/api/reports/index-health/related")
    assert health.status_code == 200, health.text
    payload = health.json()
    assert payload["backend"] == "local"
    assert payload["published"] == payload["indexed"] == 1
    assert payload["pending"] == 0
    assert payload["retrieval"] == "vector"

    login("ANALYST", email="author@example.com")
    assert client.get("/api/reports/index-health/related").status_code == 403
