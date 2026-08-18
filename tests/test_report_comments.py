"""Section-anchored editorial comments and suggested edits (#306)."""

import pytest
from sqlmodel import Session, select

from iceberg.config import get_settings
from iceberg.models import AuditEvent, OutboxJob, Report, ReportComment
from iceberg.services import email as email_service


@pytest.fixture(autouse=True)
def _clear_outbox():
    email_service.OUTBOX.clear()
    yield
    email_service.OUTBOX.clear()


def _report(client, login, *, body="The actor used spearphishing to gain access.", **fields):
    login("ANALYST", email="author@example.com")
    notebook = client.post("/api/notebooks", json={"title": "Review"}).json()
    report = client.post(
        "/api/reports",
        json={"notebook_id": notebook["id"], "title": "Under review", "body_md": body},
    )
    assert report.status_code == 201, report.text
    report = report.json()
    if fields:
        updated = client.patch(
            f"/api/reports/{report['id']}", json={"version": report["version"], **fields}
        )
        assert updated.status_code == 200, updated.text
        report = updated.json()
    return report


def _comment(client, login, report_id, *, as_role="REVIEWER", email="rev@example.com", **body):
    login(as_role, email=email)
    resp = client.post(f"/api/reports/{report_id}/comments", json=body)
    return resp


def _threads(client, login, report_id, *, as_role="REVIEWER", email="rev@example.com"):
    login(as_role, email=email)
    resp = client.get(f"/api/reports/{report_id}/comments")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _approve(client, login, report_id):
    login("ANALYST", email="author@example.com")
    client.post(f"/api/reports/{report_id}/transition", json={"target": "IN_REVIEW"})
    login("REVIEWER", email="rev@example.com")
    assert client.post(
        f"/api/reports/{report_id}/transition", json={"target": "APPROVED"}
    ).status_code == 200


# --------------------------------------------------------------------------- #
# Anchors
# --------------------------------------------------------------------------- #
def test_a_thread_anchors_to_the_revision_it_was_written_against(client, login):
    report = _report(client, login)
    created = _comment(
        client, login, report["id"],
        section="BODY", body="Is spearphishing the right word?", anchor_text="spearphishing",
    )
    assert created.status_code == 201, created.text

    payload = _threads(client, login, report["id"])
    anchor = payload["threads"][0]["anchor"]
    assert anchor["anchor_version"] == report["version"]
    assert anchor["stale"] is False
    assert anchor["anchor_present"] is True
    assert anchor["label"] == "current"


def test_an_edit_marks_the_anchor_stale_but_keeps_a_present_quote_usable(client, login):
    report = _report(client, login)
    _comment(
        client, login, report["id"],
        body="Clarify the access vector.", anchor_text="spearphishing",
    )

    login("ANALYST", email="author@example.com")
    current = client.get(f"/api/reports/{report['id']}").json()["report"]
    assert client.patch(
        f"/api/reports/{report['id']}",
        json={
            "version": current["version"],
            "body_md": "In 2026 the actor used spearphishing to gain access.",
        },
    ).status_code == 200

    anchor = _threads(client, login, report["id"])["threads"][0]["anchor"]
    assert anchor["stale"] is True
    assert anchor["anchor_present"] is True
    assert "still present" in anchor["label"]


def test_a_rewrite_that_removes_the_quote_says_so_plainly(client, login):
    report = _report(client, login)
    _comment(client, login, report["id"], body="Reword.", anchor_text="spearphishing")

    login("ANALYST", email="author@example.com")
    current = client.get(f"/api/reports/{report['id']}").json()["report"]
    client.patch(
        f"/api/reports/{report['id']}",
        json={"version": current["version"], "body_md": "The actor phoned the helpdesk."},
    )

    anchor = _threads(client, login, report["id"])["threads"][0]["anchor"]
    assert anchor["stale"] is True
    assert anchor["anchor_present"] is False
    assert "no longer in this section" in anchor["label"]


def test_a_quote_that_is_not_in_the_section_is_refused(client, login):
    report = _report(client, login)

    refused = _comment(
        client, login, report["id"], body="Comment", anchor_text="never written"
    )

    assert refused.status_code == 422
    assert "not in that section" in refused.json()["detail"]


# --------------------------------------------------------------------------- #
# Suggested edits
# --------------------------------------------------------------------------- #
def test_accepting_a_suggestion_rewrites_exactly_the_quoted_passage(client, login):
    report = _report(client, login)
    thread = _comment(
        client, login, report["id"],
        body="Use the ICD 203 term.", anchor_text="spearphishing", suggestion="spear-phishing",
    ).json()

    login("ANALYST", email="author@example.com")
    accepted = client.post(
        f"/api/reports/{report['id']}/comments/{thread['id']}/accept",
        json={"version": report["version"]},
    )

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "ACCEPTED"
    login("ANALYST", email="author@example.com")
    current = client.get(f"/api/reports/{report['id']}").json()["report"]
    assert current["body_md"] == "The actor used spear-phishing to gain access."
    assert accepted.json()["applied_version"] == current["version"]


def test_a_suggestion_is_refused_against_a_stale_report_version(client, login):
    report = _report(client, login)
    thread = _comment(
        client, login, report["id"],
        body="Reword.", anchor_text="spearphishing", suggestion="spear-phishing",
    ).json()

    login("ANALYST", email="author@example.com")
    current = client.get(f"/api/reports/{report['id']}").json()["report"]
    client.patch(
        f"/api/reports/{report['id']}",
        json={"version": current["version"], "title": "Retitled under review"},
    )

    login("ANALYST", email="author@example.com")
    refused = client.post(
        f"/api/reports/{report['id']}/comments/{thread['id']}/accept",
        json={"version": report["version"]},
    )

    assert refused.status_code == 409
    assert "changed while this suggestion was open" in refused.json()["detail"]


def test_a_suggestion_is_refused_once_its_passage_has_gone(client, login):
    report = _report(client, login)
    thread = _comment(
        client, login, report["id"],
        body="Reword.", anchor_text="spearphishing", suggestion="spear-phishing",
    ).json()

    login("ANALYST", email="author@example.com")
    current = client.get(f"/api/reports/{report['id']}").json()["report"]
    client.patch(
        f"/api/reports/{report['id']}",
        json={"version": current["version"], "body_md": "The actor phoned the helpdesk."},
    )
    latest = client.get(f"/api/reports/{report['id']}").json()["report"]

    login("ANALYST", email="author@example.com")
    refused = client.post(
        f"/api/reports/{report['id']}/comments/{thread['id']}/accept",
        json={"version": latest["version"]},
    )

    assert refused.status_code == 409
    assert "no longer uniquely present" in refused.json()["detail"]


def test_an_ambiguous_quote_is_refused_when_it_carries_a_suggestion(client, login):
    report = _report(client, login, body="alpha and alpha again")

    refused = _comment(
        client, login, report["id"],
        body="Reword.", anchor_text="alpha", suggestion="beta",
    )

    assert refused.status_code == 422
    assert "more than once" in refused.json()["detail"]


def test_a_section_without_editable_text_cannot_carry_a_suggestion(client, login):
    report = _report(client, login)

    refused = _comment(
        client, login, report["id"],
        section="SOURCES", body="Cite the vendor report.", anchor_text="x", suggestion="y",
    )

    assert refused.status_code == 422
    assert "no editable text" in refused.json()["detail"]


def test_an_accepted_thread_cannot_be_reopened(client, login):
    report = _report(client, login)
    thread = _comment(
        client, login, report["id"],
        body="Reword.", anchor_text="spearphishing", suggestion="spear-phishing",
    ).json()
    login("ANALYST", email="author@example.com")
    client.post(
        f"/api/reports/{report['id']}/comments/{thread['id']}/accept",
        json={"version": report["version"]},
    )

    refused = client.post(f"/api/reports/{report['id']}/comments/{thread['id']}/reopen")

    assert refused.status_code == 409
    assert "cannot be reopened" in refused.json()["detail"]


def test_a_published_product_refuses_a_suggested_edit(client, login):
    report = _report(client, login)
    thread = _comment(
        client, login, report["id"],
        body="Reword.", anchor_text="spearphishing", suggestion="spear-phishing",
    ).json()
    login("REVIEWER", email="rev@example.com")
    client.post(f"/api/reports/{report['id']}/comments/{thread['id']}/resolve")
    _approve(client, login, report["id"])
    assert client.post(
        f"/api/reports/{report['id']}/transition", json={"target": "PUBLISHED"}
    ).status_code == 200

    second = _comment(
        client, login, report["id"],
        body="Late change.", anchor_text="spearphishing", suggestion="spear-phishing",
    ).json()
    login("ANALYST", email="author@example.com")
    current = client.get(f"/api/reports/{report['id']}").json()["report"]
    refused = client.post(
        f"/api/reports/{report['id']}/comments/{second['id']}/accept",
        json={"version": current["version"]},
    )

    assert refused.status_code == 409
    assert "immutable" in refused.json()["detail"]


# --------------------------------------------------------------------------- #
# Threads, replies, mentions
# --------------------------------------------------------------------------- #
def test_a_reply_joins_the_thread_rather_than_starting_one(client, login):
    report = _report(client, login)
    thread = _comment(client, login, report["id"], body="Please clarify.").json()

    login("ANALYST", email="author@example.com")
    reply = client.post(
        f"/api/reports/{report['id']}/comments/{thread['id']}/replies",
        json={"body": "Clarified in the next revision."},
    )

    assert reply.status_code == 201, reply.text
    payload = _threads(client, login, report["id"])
    assert len(payload["threads"]) == 1
    assert [r["body"] for r in payload["threads"][0]["replies"]] == [
        "Clarified in the next revision."
    ]


def test_a_mention_notifies_the_named_writer_through_the_outbox(client, login, engine):
    login("ANALYST", email="author@example.com")
    report = _report(client, login)
    created = _comment(client, login, report["id"], body="@author please take a look").json()

    assert created["mentions"], "the named writer should be resolved"
    with Session(engine) as session:
        jobs = session.exec(
            select(OutboxJob).where(OutboxJob.kind == "EDITORIAL_MENTION")
        ).all()
        assert len(jobs) == 1
        assert jobs[0].payload["comment_id"] == created["id"]


def test_a_stakeholder_is_never_mentioned(client, login, engine):
    login("STAKEHOLDER", email="reader@example.com")
    report = _report(client, login)

    created = _comment(client, login, report["id"], body="@reader thoughts?").json()

    assert created["mentions"] == []
    with Session(engine) as session:
        assert session.exec(
            select(OutboxJob).where(OutboxJob.kind == "EDITORIAL_MENTION")
        ).all() == []


def test_mentioning_yourself_queues_nothing(client, login, engine):
    report = _report(client, login)
    _comment(client, login, report["id"], body="@rev noting this myself")

    with Session(engine) as session:
        assert session.exec(
            select(OutboxJob).where(OutboxJob.kind == "EDITORIAL_MENTION")
        ).all() == []


# --------------------------------------------------------------------------- #
# Permissions
# --------------------------------------------------------------------------- #
def test_threads_are_writer_only(client, login):
    report = _report(client, login)
    _comment(client, login, report["id"], body="Internal note")
    _approve(client, login, report["id"])
    login("REVIEWER", email="rev@example.com")
    client.post(f"/api/reports/{report['id']}/transition", json={"target": "PUBLISHED"})

    login("STAKEHOLDER", email="reader@example.com")
    assert client.get(f"/api/reports/{report['id']}/comments").status_code == 403
    assert client.post(
        f"/api/reports/{report['id']}/comments", json={"body": "hello"}
    ).status_code == 403
    page = client.get(f"/reports/{report['id']}")
    assert page.status_code == 200
    assert "Editorial review" not in page.text
    assert "Internal note" not in page.text


def test_threads_on_an_invisible_product_are_not_reachable(client, login):
    report = _report(client, login)

    login("STAKEHOLDER", email="reader@example.com")
    # Unpublished: ensure_visible hides its existence before the role check.
    assert client.get(f"/api/reports/{report['id']}/comments").status_code == 404


# --------------------------------------------------------------------------- #
# Publication gate
# --------------------------------------------------------------------------- #
def test_an_open_blocking_thread_stops_publication(client, login):
    report = _report(client, login)
    thread = _comment(
        client, login, report["id"], body="Judgement needs a confidence marking.", blocking=True
    ).json()
    _approve(client, login, report["id"])

    refused = client.post(
        f"/api/reports/{report['id']}/transition", json={"target": "PUBLISHED"}
    )
    assert refused.status_code == 409
    assert "blocking review thread" in refused.json()["detail"]

    client.post(f"/api/reports/{report['id']}/comments/{thread['id']}/resolve")
    assert client.post(
        f"/api/reports/{report['id']}/transition", json={"target": "PUBLISHED"}
    ).status_code == 200


def test_a_non_blocking_thread_does_not_stop_publication(client, login):
    report = _report(client, login)
    _comment(client, login, report["id"], body="Nice work.")
    _approve(client, login, report["id"])

    assert client.post(
        f"/api/reports/{report['id']}/transition", json={"target": "PUBLISHED"}
    ).status_code == 200


def test_the_publication_gate_can_be_switched_off(client, login, monkeypatch):
    monkeypatch.setattr(get_settings(), "publish_requires_resolved_threads", False)
    report = _report(client, login)
    _comment(client, login, report["id"], body="Advisory only.", blocking=True)
    _approve(client, login, report["id"])

    assert client.post(
        f"/api/reports/{report['id']}/transition", json={"target": "PUBLISHED"}
    ).status_code == 200


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #
def test_decisions_are_audited_without_the_prose(client, login, engine):
    report = _report(client, login)
    thread = _comment(
        client, login, report["id"],
        body="Sensitive editorial wording", anchor_text="spearphishing",
        suggestion="spear-phishing",
    ).json()
    login("ANALYST", email="author@example.com")
    client.post(
        f"/api/reports/{report['id']}/comments/{thread['id']}/accept",
        json={"version": report["version"]},
    )

    with Session(engine) as session:
        events = session.exec(
            select(AuditEvent).where(AuditEvent.resource_type == "report")
        ).all()
        actions = {event.action for event in events}
        assert "REPORT_COMMENT_CREATED" in actions
        assert "REPORT_SUGGESTION_ACCEPTED" in actions
        for event in events:
            assert "Sensitive editorial wording" not in str(event.detail)
            assert "spear-phishing" not in str(event.detail)
        accepted = next(e for e in events if e.action == "REPORT_SUGGESTION_ACCEPTED")
        assert accepted.detail["applied_version"]


# --------------------------------------------------------------------------- #
# Portal
# --------------------------------------------------------------------------- #
def test_the_portal_thread_flow_works_end_to_end(client, login, engine):
    report = _report(client, login)

    login("REVIEWER", email="rev@example.com")
    created = client.post(
        f"/reports/{report['id']}/comments",
        data={
            "section": "BODY",
            "body": "Use the house term.",
            "anchor_text": "spearphishing",
            "suggestion": "spear-phishing",
            "blocking": "true",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303

    page = client.get(f"/reports/{report['id']}")
    assert "Editorial review" in page.text
    assert "Use the house term." in page.text
    assert "blocking thread" in page.text

    with Session(engine) as session:
        thread = session.exec(select(ReportComment)).one()
        current = session.get(Report, report["id"])
        version = current.version
    login("ANALYST", email="author@example.com")
    accepted = client.post(
        f"/reports/{report['id']}/comments/{thread.id}/accept",
        data={"version": str(version)},
        follow_redirects=False,
    )
    assert accepted.status_code == 303

    with Session(engine) as session:
        assert "spear-phishing" in session.get(Report, report["id"]).body_md


def test_the_portal_surfaces_a_refusal_instead_of_failing_silently(client, login):
    report = _report(client, login)
    login("REVIEWER", email="rev@example.com")

    refused = client.post(
        f"/reports/{report['id']}/comments",
        data={"section": "BODY", "body": "x", "anchor_text": "not present"},
        follow_redirects=False,
    )

    assert refused.status_code == 303
    assert "review_error" in refused.headers["location"]
