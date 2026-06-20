"""Intelligence-cycle feedback loop (backlog D): stakeholder product feedback /
RFI-satisfaction on disseminated products, the auto-satisfy close, writer/analyst
surfaces, maturity aggregation, role gating, and cascade behaviour."""

from sqlmodel import Session, select

from iceberg.models import ProductFeedback, Report, Requirement
from iceberg.services import feedback as feedback_service


def _make_stakeholder(client, login, email, level="STRATEGIC"):
    login("STAKEHOLDER", email=email)
    if level is not None:
        client.patch("/api/me", json={"preferred_intel_level": level})


def _publish(client, login, *, level="STRATEGIC", requirement_ids=None, title="Brief"):
    """Author + publish a report (optionally linking requirements), returning its id."""
    login("ANALYST", email="author@example.com")
    nb = client.post("/api/notebooks", json={"title": "nb"}).json()
    rid = client.post(
        "/api/reports",
        json={"notebook_id": nb["id"], "title": title, "intel_level": level},
    ).json()["id"]
    if requirement_ids:
        r = client.put(
            f"/api/reports/{rid}/requirements",
            json={"requirement_ids": requirement_ids},
        )
        assert r.status_code == 200, r.text
    client.post(f"/api/reports/{rid}/transition", json={"target": "IN_REVIEW"})
    login("REVIEWER", email="rev@example.com")
    client.post(f"/api/reports/{rid}/transition", json={"target": "APPROVED"})
    pub = client.post(f"/api/reports/{rid}/transition", json={"target": "PUBLISHED"})
    assert pub.status_code == 200, pub.text
    return rid


def _new_requirement(client, login, email, title="Need X"):
    login("STAKEHOLDER", email=email)
    return client.post("/api/requirements", json={"title": title}).json()["id"]


# --------------------------------------------------------------------------- #
# Submission + delivered-product guard
# --------------------------------------------------------------------------- #
def test_stakeholder_submits_feedback_on_delivered_product(client, login):
    _make_stakeholder(client, login, "s@example.com", "STRATEGIC")
    rid = _publish(client, login)

    login("STAKEHOLDER", email="s@example.com")
    resp = client.post(
        f"/api/reports/{rid}/feedback",
        json={"usefulness": "HIGHLY_USEFUL", "comment": "Great."},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["usefulness"] == "HIGHLY_USEFUL"

    # Writer sees it.
    login("ANALYST", email="author@example.com")
    got = client.get(f"/api/reports/{rid}/feedback").json()
    assert len(got) == 1 and got[0]["comment"] == "Great."


def test_feedback_on_undelivered_product_is_forbidden(client, login):
    # Stakeholder prefers OPERATIONAL, report is STRATEGIC → not disseminated.
    _make_stakeholder(client, login, "s@example.com", "OPERATIONAL")
    rid = _publish(client, login, level="STRATEGIC")

    login("STAKEHOLDER", email="s@example.com")
    resp = client.post(
        f"/api/reports/{rid}/feedback", json={"usefulness": "USEFUL"}
    )
    assert resp.status_code == 403


def test_resubmit_upserts(client, login):
    _make_stakeholder(client, login, "s@example.com", "STRATEGIC")
    rid = _publish(client, login)

    login("STAKEHOLDER", email="s@example.com")
    client.post(f"/api/reports/{rid}/feedback", json={"usefulness": "USEFUL"})
    client.post(
        f"/api/reports/{rid}/feedback",
        json={"usefulness": "NOT_USEFUL", "comment": "changed my mind"},
    )

    login("ANALYST", email="author@example.com")
    got = client.get(f"/api/reports/{rid}/feedback").json()
    assert len(got) == 1
    assert got[0]["usefulness"] == "NOT_USEFUL"
    assert got[0]["comment"] == "changed my mind"


# --------------------------------------------------------------------------- #
# Role gating
# --------------------------------------------------------------------------- #
def test_non_stakeholder_cannot_give_feedback(client, login):
    _make_stakeholder(client, login, "s@example.com", "STRATEGIC")
    rid = _publish(client, login)

    login("ANALYST", email="author@example.com")
    resp = client.post(f"/api/reports/{rid}/feedback", json={"usefulness": "USEFUL"})
    assert resp.status_code == 403


def test_writer_feedback_list_is_writer_only(client, login):
    _make_stakeholder(client, login, "s@example.com", "STRATEGIC")
    rid = _publish(client, login)

    login("STAKEHOLDER", email="s@example.com")
    assert client.get(f"/api/reports/{rid}/feedback").status_code == 403


# --------------------------------------------------------------------------- #
# Requirement linkage validation
# --------------------------------------------------------------------------- #
def test_requirement_must_be_owned_and_linked(client, login):
    # A requirement owned by the stakeholder but NOT addressed by the report.
    req_id = _new_requirement(client, login, "s@example.com")
    _make_stakeholder(client, login, "s@example.com", "STRATEGIC")
    rid = _publish(client, login)  # no requirement link

    login("STAKEHOLDER", email="s@example.com")
    resp = client.post(
        f"/api/reports/{rid}/feedback",
        json={"usefulness": "USEFUL", "requirement_id": req_id, "satisfaction": "MET"},
    )
    assert resp.status_code == 400


def test_cannot_claim_another_stakeholders_requirement(client, login):
    other_req = _new_requirement(client, login, "other@example.com")
    _make_stakeholder(client, login, "s@example.com", "STRATEGIC")
    rid = _publish(client, login, requirement_ids=[other_req])

    login("STAKEHOLDER", email="s@example.com")
    resp = client.post(
        f"/api/reports/{rid}/feedback",
        json={"usefulness": "USEFUL", "requirement_id": other_req, "satisfaction": "MET"},
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# Auto-satisfy (closing the loop)
# --------------------------------------------------------------------------- #
def test_met_verdict_auto_satisfies_requirement(client, login):
    req_id = _new_requirement(client, login, "s@example.com")
    _make_stakeholder(client, login, "s@example.com", "STRATEGIC")
    rid = _publish(client, login, requirement_ids=[req_id])

    login("STAKEHOLDER", email="s@example.com")
    resp = client.post(
        f"/api/reports/{rid}/feedback",
        json={"usefulness": "USEFUL", "requirement_id": req_id, "satisfaction": "MET"},
    )
    assert resp.status_code == 201
    assert client.get(f"/api/requirements/{req_id}").json()["requirement"]["status"] == "SATISFIED"


def test_partial_verdict_does_not_satisfy(client, login):
    req_id = _new_requirement(client, login, "s@example.com")
    _make_stakeholder(client, login, "s@example.com", "STRATEGIC")
    rid = _publish(client, login, requirement_ids=[req_id])

    login("STAKEHOLDER", email="s@example.com")
    client.post(
        f"/api/reports/{rid}/feedback",
        json={
            "usefulness": "USEFUL",
            "requirement_id": req_id,
            "satisfaction": "PARTIALLY_MET",
        },
    )
    assert client.get(f"/api/requirements/{req_id}").json()["requirement"]["status"] == "OPEN"


# --------------------------------------------------------------------------- #
# Portal surfaces
# --------------------------------------------------------------------------- #
def test_portal_feedback_flow_and_surfaces(client, login):
    req_id = _new_requirement(client, login, "s@example.com")
    _make_stakeholder(client, login, "s@example.com", "STRATEGIC")
    rid = _publish(client, login, requirement_ids=[req_id], title="Portal product")

    # Stakeholder sees the feedback form on the delivered report.
    login("STAKEHOLDER", email="s@example.com")
    view = client.get(f"/reports/{rid}")
    assert "Your feedback" in view.text

    # Submit via the portal form.
    resp = client.post(
        f"/reports/{rid}/feedback",
        data={
            "usefulness": "HIGHLY_USEFUL",
            "requirement_id": str(req_id),
            "satisfaction": "MET",
            "comment": "Spot on",
        },
    )
    assert resp.status_code in (200, 303)

    # Requirement detail (analyst view) shows the feedback.
    login("ANALYST", email="author@example.com")
    detail = client.get(f"/requirements/{req_id}")
    assert "Stakeholder feedback" in detail.text
    assert "Spot on" in detail.text
    # Auto-satisfied by the MET verdict.
    assert client.get(f"/api/requirements/{req_id}").json()["requirement"]["status"] == "SATISFIED"


def test_writer_report_view_shows_received_feedback(client, login):
    _make_stakeholder(client, login, "s@example.com", "STRATEGIC")
    rid = _publish(client, login)
    login("STAKEHOLDER", email="s@example.com")
    client.post(
        f"/api/reports/{rid}/feedback",
        json={"usefulness": "USEFUL", "comment": "useful note"},
    )

    login("ANALYST", email="author@example.com")
    view = client.get(f"/reports/{rid}")
    assert "Product feedback" in view.text
    assert "useful note" in view.text


# --------------------------------------------------------------------------- #
# Maturity aggregation
# --------------------------------------------------------------------------- #
def test_maturity_feedback_metrics(client, login):
    req_id = _new_requirement(client, login, "s@example.com")
    _make_stakeholder(client, login, "s@example.com", "STRATEGIC")
    rid = _publish(client, login, requirement_ids=[req_id])
    login("STAKEHOLDER", email="s@example.com")
    client.post(
        f"/api/reports/{rid}/feedback",
        json={"usefulness": "HIGHLY_USEFUL", "requirement_id": req_id, "satisfaction": "MET"},
    )

    login("ANALYST", email="author@example.com")
    page = client.get("/maturity")
    assert page.status_code == 200
    assert "Feedback loop" in page.text


def test_feedback_effectiveness_empty_is_safe(engine):
    with Session(engine) as session:
        fb = feedback_service.feedback_effectiveness(session)
    assert fb == {
        "responses": 0,
        "deliveries": 0,
        "response_rate": 0.0,
        "verdicts": 0,
        "satisfaction_rate": 0.0,
        "useful_rate": 0.0,
    }


# --------------------------------------------------------------------------- #
# Cascade behaviour
# --------------------------------------------------------------------------- #
def test_deleting_report_removes_feedback(client, login, engine):
    _make_stakeholder(client, login, "s@example.com", "STRATEGIC")
    rid = _publish(client, login)
    login("STAKEHOLDER", email="s@example.com")
    client.post(f"/api/reports/{rid}/feedback", json={"usefulness": "USEFUL"})

    with Session(engine) as session:
        report = session.get(Report, rid)
        session.delete(report)
        session.commit()
        assert session.exec(select(ProductFeedback)).all() == []


def test_deleting_requirement_nulls_link_keeps_feedback(client, login, engine):
    req_id = _new_requirement(client, login, "s@example.com")
    _make_stakeholder(client, login, "s@example.com", "STRATEGIC")
    rid = _publish(client, login, requirement_ids=[req_id])
    login("STAKEHOLDER", email="s@example.com")
    client.post(
        f"/api/reports/{rid}/feedback",
        json={"usefulness": "USEFUL", "requirement_id": req_id, "satisfaction": "NOT_MET"},
    )

    with Session(engine) as session:
        req = session.get(Requirement, req_id)
        session.delete(req)
        session.commit()
        rows = session.exec(select(ProductFeedback)).all()
        assert len(rows) == 1
        assert rows[0].requirement_id is None
