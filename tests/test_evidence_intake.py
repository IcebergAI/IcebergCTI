"""Governed evidence intake from adjacent systems (#305).

The contract fixture in ``docs/contracts/evidence-envelope-v1.json`` is the
envelope producers are asked to send, so it is exercised here rather than
described only in prose.
"""

import copy
import json
from pathlib import Path

import pytest
from sqlmodel import Session, select

from iceberg.config import get_settings
from iceberg.models import AuditEvent, EvidenceReference, PublicationSnapshot, Source
from iceberg.services import evidence

FIXTURE = (
    Path(__file__).resolve().parents[1] / "docs" / "contracts" / "evidence-envelope-v1.json"
)


@pytest.fixture
def envelope():
    payload = json.loads(FIXTURE.read_text())
    payload.pop("$comment", None)
    return payload


def _notebook(client, login):
    login("ANALYST", email="author@example.com")
    return client.post("/api/notebooks", json={"title": "Evidence"}).json()


def _post(client, notebook_id, envelope):
    return client.post(f"/api/notebooks/{notebook_id}/evidence", json=envelope)


def _accept(client, notebook_id, reference_id):
    return client.post(
        f"/api/notebooks/{notebook_id}/evidence/{reference_id}/accept"
    )


# --------------------------------------------------------------------------- #
# Contract + intake
# --------------------------------------------------------------------------- #
def test_the_published_contract_fixture_is_accepted(client, login, envelope):
    notebook = _notebook(client, login)

    received = _post(client, notebook["id"], envelope)

    assert received.status_code == 201, received.text
    body = received.json()
    assert body["created"] is True
    reference = body["evidence"]
    assert reference["source_system"] == "iceberg-osint"
    assert reference["external_id"] == "osint-2026-000412"
    assert reference["revision"] == "3"
    assert reference["state"] == "PENDING"
    assert reference["verification"]["digest_declared"] is True


def test_intake_is_writer_only_and_authenticated(client, login, envelope):
    notebook = _notebook(client, login)

    login("STAKEHOLDER", email="sh@example.com")
    assert _post(client, notebook["id"], envelope).status_code == 403


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda e: e.pop("external_id"), "missing required field"),
        (lambda e: e.update(schema_version="iceberg.evidence/9"), "Unsupported envelope schema"),
        (lambda e: e.update(source_system="Not A System!"), "short lowercase identifier"),
        (lambda e: e.update(deep_link="javascript:alert(1)"), "http(s) URL"),
        (lambda e: e.update(content_sha256="not-a-digest"), "hex SHA-256"),
        (lambda e: e.update(tlp="PURPLE"), "'tlp' must be one of"),
        (lambda e: e.update(provenance="a string"), "'provenance' must be a JSON object"),
    ],
)
def test_a_malformed_envelope_is_refused_with_the_reason(
    client, login, envelope, mutate, expected
):
    notebook = _notebook(client, login)
    mutate(envelope)

    refused = _post(client, notebook["id"], envelope)

    assert refused.status_code == 422, refused.text
    assert expected in refused.json()["detail"]


def test_an_oversized_envelope_is_refused(client, login, envelope):
    notebook = _notebook(client, login)
    envelope["provenance"] = {"padding": "x" * (evidence.MAX_ENVELOPE_BYTES + 100)}

    refused = _post(client, notebook["id"], envelope)

    assert refused.status_code == 422
    assert "Send a reference, not the evidence body" in refused.json()["detail"]


def test_an_oversized_body_is_refused_before_it_is_parsed(client, login, envelope):
    """The cap has to bound the bytes that arrived, not the object they parse to.

    JSON allows unlimited whitespace around a small object, so measuring the
    re-serialised envelope bounds what is stored while letting a body orders of
    magnitude larger be read and parsed first.
    """

    notebook = _notebook(client, login)
    padded = json.dumps(envelope) + " " * (evidence.MAX_ENVELOPE_BYTES + 100)

    refused = client.post(
        f"/api/notebooks/{notebook['id']}/evidence",
        content=padded,
        headers={"content-type": "application/json"},
    )

    assert refused.status_code == 422
    assert "request body is" in refused.json()["detail"]
    assert "Send a reference, not the evidence body" in refused.json()["detail"]


def test_a_body_that_is_not_json_is_refused_with_the_same_reason(client, login):
    notebook = _notebook(client, login)

    refused = client.post(
        f"/api/notebooks/{notebook['id']}/evidence",
        content="not json at all",
        headers={"content-type": "application/json"},
    )

    assert refused.status_code == 422
    assert "must be a JSON object" in refused.json()["detail"]


# --------------------------------------------------------------------------- #
# Identity: idempotent, replay-safe, revision-aware
# --------------------------------------------------------------------------- #
def test_reposting_the_same_revision_is_idempotent(client, login, envelope):
    notebook = _notebook(client, login)
    first = _post(client, notebook["id"], envelope).json()

    replay = _post(client, notebook["id"], envelope)

    assert replay.status_code == 200
    assert replay.json()["created"] is False
    assert replay.json()["evidence"]["id"] == first["evidence"]["id"]


def test_a_replay_does_not_undo_a_decision_already_made(client, login, envelope):
    notebook = _notebook(client, login)
    reference = _post(client, notebook["id"], envelope).json()["evidence"]
    _accept(client, notebook["id"], reference["id"])

    replay = _post(client, notebook["id"], envelope)

    assert replay.json()["evidence"]["state"] == "ACCEPTED"


def test_the_same_identity_with_different_content_is_a_conflict(client, login, envelope):
    notebook = _notebook(client, login)
    _post(client, notebook["id"], envelope)
    tampered = copy.deepcopy(envelope)
    tampered["summary"] = "Something else entirely"

    refused = _post(client, notebook["id"], tampered)

    assert refused.status_code == 409
    assert "publish a new revision" in refused.json()["detail"]


def test_a_new_revision_supersedes_its_predecessor(client, login, envelope):
    notebook = _notebook(client, login)
    first = _post(client, notebook["id"], envelope).json()["evidence"]
    newer = copy.deepcopy(envelope)
    newer["revision"] = "4"
    newer["summary"] = "Updated after the seller edited the advert."

    second = _post(client, notebook["id"], newer)

    assert second.status_code == 201
    assert second.json()["superseded"] == [first["id"]]
    listing = {item["id"]: item for item in client.get(f"/api/notebooks/{notebook['id']}/evidence").json()}
    assert listing[first["id"]]["state"] == "SUPERSEDED"
    assert listing[second.json()["evidence"]["id"]]["state"] == "PENDING"


def test_a_superseded_item_cannot_be_accepted(client, login, envelope):
    notebook = _notebook(client, login)
    first = _post(client, notebook["id"], envelope).json()["evidence"]
    newer = copy.deepcopy(envelope)
    newer["revision"] = "4"
    _post(client, notebook["id"], newer)

    refused = _accept(client, notebook["id"], first["id"])

    assert refused.status_code == 409
    assert "newer revision" in refused.json()["detail"]


# --------------------------------------------------------------------------- #
# Markings: preserved or strengthened, never weakened
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("declared", "expected"),
    [("CLEAR", "AMBER"), ("GREEN", "AMBER"), ("AMBER", "AMBER"), ("RED", "RED")],
)
def test_a_producer_cannot_weaken_the_marking(client, login, envelope, declared, expected):
    notebook = _notebook(client, login)
    envelope["tlp"] = declared

    reference = _post(client, notebook["id"], envelope).json()["evidence"]

    assert reference["tlp"] == expected


def test_the_accepted_source_inherits_the_strengthened_marking(client, login, envelope, engine):
    notebook = _notebook(client, login)
    envelope["tlp"] = "CLEAR"
    reference = _post(client, notebook["id"], envelope).json()["evidence"]

    accepted = _accept(client, notebook["id"], reference["id"]).json()

    with Session(engine) as session:
        source = session.get(Source, accepted["source_id"])
        assert str(source.tlp) == "AMBER"


def test_the_floor_marking_is_configurable_upwards(client, login, envelope, monkeypatch):
    monkeypatch.setattr(get_settings(), "evidence_min_tlp", "RED")
    notebook = _notebook(client, login)
    envelope["tlp"] = "GREEN"

    reference = _post(client, notebook["id"], envelope).json()["evidence"]

    assert reference["tlp"] == "RED"
    assert reference["verification"]["marking_strengthened"] is True


# --------------------------------------------------------------------------- #
# Analyst decision
# --------------------------------------------------------------------------- #
def test_nothing_becomes_collection_material_until_it_is_accepted(client, login, envelope, engine):
    notebook = _notebook(client, login)
    _post(client, notebook["id"], envelope)

    with Session(engine) as session:
        assert session.exec(select(Source)).all() == []


def test_accepting_creates_a_source_that_keeps_the_way_back(client, login, envelope, engine):
    notebook = _notebook(client, login)
    reference = _post(client, notebook["id"], envelope).json()["evidence"]

    accepted = _accept(client, notebook["id"], reference["id"])

    assert accepted.status_code == 200, accepted.text
    with Session(engine) as session:
        source = session.get(Source, accepted.json()["source_id"])
        assert source.title == envelope["title"]
        assert source.reference == envelope["deep_link"]
        assert envelope["external_id"] in source.content_md
        assert envelope["content_sha256"] in source.content_md
        assert "collection_method" in source.content_md


def test_rejecting_leaves_no_source_behind(client, login, envelope, engine):
    notebook = _notebook(client, login)
    reference = _post(client, notebook["id"], envelope).json()["evidence"]

    rejected = client.post(
        f"/api/notebooks/{notebook['id']}/evidence/{reference['id']}/reject"
    )

    assert rejected.json()["state"] == "REJECTED"
    with Session(engine) as session:
        assert session.exec(select(Source)).all() == []


# --------------------------------------------------------------------------- #
# Revocation and publication
# --------------------------------------------------------------------------- #
def _publish_citing(client, login, notebook_id, source_id):
    login("ANALYST", email="author@example.com")
    report = client.post(
        "/api/reports", json={"notebook_id": notebook_id, "title": "Evidence-backed"}
    ).json()
    assert client.put(
        f"/api/reports/{report['id']}/citations", json={"source_ids": [source_id]}
    ).status_code == 200
    client.post(f"/api/reports/{report['id']}/transition", json={"target": "IN_REVIEW"})
    login("REVIEWER", email="rev@example.com")
    client.post(f"/api/reports/{report['id']}/transition", json={"target": "APPROVED"})
    assert client.post(
        f"/api/reports/{report['id']}/transition", json={"target": "PUBLISHED"}
    ).status_code == 200
    return report


def test_a_published_snapshot_keeps_the_evidence_manifest(client, login, envelope, engine):
    notebook = _notebook(client, login)
    reference = _post(client, notebook["id"], envelope).json()["evidence"]
    accepted = _accept(client, notebook["id"], reference["id"]).json()
    report = _publish_citing(client, login, notebook["id"], accepted["source_id"])

    with Session(engine) as session:
        snapshot = session.exec(
            select(PublicationSnapshot).where(PublicationSnapshot.report_id == report["id"])
        ).one()
        manifest = snapshot.payload["evidence"]

    assert len(manifest) == 1
    assert manifest[0]["source_system"] == "iceberg-osint"
    assert manifest[0]["revision"] == "3"
    assert manifest[0]["content_sha256"] == envelope["content_sha256"]
    assert manifest[0]["tlp"] == "AMBER"


def test_revocation_signals_without_erasing_the_citation(client, login, envelope, engine):
    notebook = _notebook(client, login)
    reference = _post(client, notebook["id"], envelope).json()["evidence"]
    accepted = _accept(client, notebook["id"], reference["id"]).json()
    report = _publish_citing(client, login, notebook["id"], accepted["source_id"])

    login("ANALYST", email="author@example.com")
    revoked = client.post(
        f"/api/notebooks/{notebook['id']}/evidence/{reference['id']}/revoke",
        json={"reason": "Seller advert removed"},
    )

    assert revoked.status_code == 200
    assert revoked.json()["state"] == "REVOKED"
    with Session(engine) as session:
        # The source and its citation survive; the snapshot is untouched.
        assert session.get(Source, accepted["source_id"]) is not None
        snapshot = session.exec(
            select(PublicationSnapshot).where(PublicationSnapshot.report_id == report["id"])
        ).one()
        assert snapshot.payload["evidence"][0]["state"] == "ACCEPTED"

    page = client.get(f"/reports/{report['id']}")
    assert "Evidence withdrawn at source" in page.text
    assert "Seller advert removed" in page.text


def test_a_revoked_item_cannot_be_rejected_back_into_play(client, login, envelope):
    """Revocation is the producer's statement, not an analyst decision.

    Rejecting a revoked item would move it to REJECTED, which accept() allows —
    laundering withdrawn evidence into collection material and silencing the
    report's withdrawal notice, which matches on REVOKED.
    """

    notebook = _notebook(client, login)
    reference = _post(client, notebook["id"], envelope).json()["evidence"]
    _accept(client, notebook["id"], reference["id"])
    client.post(
        f"/api/notebooks/{notebook['id']}/evidence/{reference['id']}/revoke",
        json={"reason": "withdrawn"},
    )

    refused = client.post(
        f"/api/notebooks/{notebook['id']}/evidence/{reference['id']}/reject"
    )

    assert refused.status_code == 409
    assert "revocation stands" in refused.json()["detail"]
    # ... and the state is untouched, so acceptance is still refused and the
    # withdrawal notice still fires.
    still = _accept(client, notebook["id"], reference["id"])
    assert still.status_code == 409
    assert "revoked by its producing system" in still.json()["detail"]


def test_a_superseded_item_cannot_be_rejected_either(client, login, envelope):
    notebook = _notebook(client, login)
    first = _post(client, notebook["id"], envelope).json()["evidence"]
    newer = {**envelope, "revision": str(int(envelope["revision"]) + 1)}
    _post(client, notebook["id"], newer)

    refused = client.post(
        f"/api/notebooks/{notebook['id']}/evidence/{first['id']}/reject"
    )

    assert refused.status_code == 409
    assert "supersedes it" in refused.json()["detail"]


def test_a_revoked_item_cannot_be_accepted_afterwards(client, login, envelope):
    notebook = _notebook(client, login)
    reference = _post(client, notebook["id"], envelope).json()["evidence"]
    client.post(
        f"/api/notebooks/{notebook['id']}/evidence/{reference['id']}/revoke",
        json={"reason": "withdrawn"},
    )

    refused = _accept(client, notebook["id"], reference["id"])

    assert refused.status_code == 409
    assert "revoked by its producing system" in refused.json()["detail"]


# --------------------------------------------------------------------------- #
# Audit + portal
# --------------------------------------------------------------------------- #
def test_intake_and_decisions_are_audited(client, login, envelope, engine):
    notebook = _notebook(client, login)
    reference = _post(client, notebook["id"], envelope).json()["evidence"]
    _accept(client, notebook["id"], reference["id"])

    with Session(engine) as session:
        actions = {
            event.action
            for event in session.exec(
                select(AuditEvent).where(AuditEvent.resource_type == "notebook")
            ).all()
        }
    assert {"EVIDENCE_RECEIVED", "EVIDENCE_ACCEPTED"} <= actions


def test_the_notebook_page_shows_and_decides_evidence(client, login, envelope, engine):
    notebook = _notebook(client, login)
    reference = _post(client, notebook["id"], envelope).json()["evidence"]

    page = client.get(f"/notebooks/{notebook['id']}")
    assert "External evidence" in page.text
    assert envelope["title"] in page.text
    assert "iceberg-osint" in page.text

    accepted = client.post(
        f"/notebooks/{notebook['id']}/evidence/{reference['id']}/accept",
        follow_redirects=False,
    )
    assert accepted.status_code == 303
    with Session(engine) as session:
        row = session.get(EvidenceReference, reference["id"])
        assert str(row.state) == "ACCEPTED"
        assert row.source_id is not None


def test_the_notebook_page_surfaces_a_refusal(client, login, envelope):
    notebook = _notebook(client, login)
    reference = _post(client, notebook["id"], envelope).json()["evidence"]
    client.post(
        f"/api/notebooks/{notebook['id']}/evidence/{reference['id']}/revoke",
        json={"reason": "withdrawn"},
    )

    refused = client.post(
        f"/notebooks/{notebook['id']}/evidence/{reference['id']}/accept",
        follow_redirects=False,
    )

    assert refused.status_code == 303
    assert "evidence_error" in refused.headers["location"]
