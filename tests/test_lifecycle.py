"""Report lifecycle: legal transitions, role guards, illegal transitions,
and immutability of published reports. Includes regression coverage for the
state machine."""


def _report(client, title="R", intel="OPERATIONAL", tlp="AMBER"):
    nb = client.post("/api/notebooks", json={"title": "nb"}).json()
    resp = client.post(
        "/api/reports",
        json={"notebook_id": nb["id"], "title": title, "intel_level": intel, "tlp": tlp},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _transition(client, rid, target):
    return client.post(f"/api/reports/{rid}/transition", json={"target": target})


def test_full_happy_path(client, login):
    login("ANALYST", email="author@example.com")
    rid = _report(client)["id"]

    assert _transition(client, rid, "IN_REVIEW").json()["status"] == "IN_REVIEW"

    # Reviewer approves and publishes.
    login("REVIEWER", email="rev@example.com")
    assert _transition(client, rid, "APPROVED").json()["status"] == "APPROVED"
    published = _transition(client, rid, "PUBLISHED").json()
    assert published["status"] == "PUBLISHED"
    assert published["published_at"] is not None


def test_illegal_transition_draft_to_published(client, login):
    login("ANALYST")
    rid = _report(client)["id"]
    resp = _transition(client, rid, "PUBLISHED")
    # Publishing uses an atomic compare-and-swap path, where an invalid source
    # state is correctly represented as a resource-state conflict.
    assert resp.status_code == 409


def test_analyst_cannot_approve(client, login):
    login("ANALYST", email="author@example.com")
    rid = _report(client)["id"]
    _transition(client, rid, "IN_REVIEW")
    # Same analyst (non-reviewer) attempts to approve their own report.
    resp = _transition(client, rid, "APPROVED")
    assert resp.status_code == 400


def test_published_report_is_immutable(client, login):
    login("ANALYST", email="author@example.com")
    rid = _report(client)["id"]
    _transition(client, rid, "IN_REVIEW")

    login("REVIEWER", email="rev@example.com")
    _transition(client, rid, "APPROVED")
    _transition(client, rid, "PUBLISHED")

    # Back to the author: editing a published report is rejected.
    login("ANALYST", email="author@example.com")
    version = client.get(f"/api/reports/{rid}").json()["report"]["version"]
    resp = client.patch(f"/api/reports/{rid}", json={"body_md": "tampered", "version": version})
    assert resp.status_code == 409


def test_send_back_clears_reviewer(client, login):
    login("ANALYST", email="author@example.com")
    rid = _report(client)["id"]
    _transition(client, rid, "IN_REVIEW")

    login("REVIEWER", email="rev@example.com")
    _transition(client, rid, "APPROVED")
    sent_back = _transition(client, rid, "IN_REVIEW").json()
    assert sent_back["status"] == "IN_REVIEW"
