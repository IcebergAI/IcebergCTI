"""Milestone 2: stakeholder requirements, analyst tasking, and traceability."""


def _create_req(client, title="Visibility into APT29 TTPs", priority="HIGH"):
    resp = client.post(
        "/api/requirements",
        json={"title": title, "priority": priority, "intel_level": "STRATEGIC"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# API: roles & ownership
# --------------------------------------------------------------------------- #
def test_stakeholder_creates_and_lists_own(client, login):
    login("STAKEHOLDER", email="stake@example.com")
    req = _create_req(client)
    listing = client.get("/api/requirements").json()
    assert [r["id"] for r in listing] == [req["id"]]


def test_analyst_cannot_create_requirement(client, login):
    login("ANALYST")
    resp = client.post("/api/requirements", json={"title": "x"})
    assert resp.status_code == 403


def test_analyst_sees_full_backlog(client, login):
    login("STAKEHOLDER", email="s1@example.com")
    _create_req(client, title="A")
    login("STAKEHOLDER", email="s2@example.com")
    _create_req(client, title="B")

    login("ANALYST")
    titles = {r["title"] for r in client.get("/api/requirements").json()}
    assert {"A", "B"} <= titles


def test_stakeholder_isolation(client, login):
    login("STAKEHOLDER", email="owner@example.com")
    mine = _create_req(client, title="Mine")
    login("STAKEHOLDER", email="other@example.com")
    # The other stakeholder sees only their own (empty) list...
    assert client.get("/api/requirements").json() == []
    # ...and cannot view someone else's requirement.
    assert client.get(f"/api/requirements/{mine['id']}").status_code == 403


def test_status_change_is_analyst_only(client, login):
    login("STAKEHOLDER", email="stake@example.com")
    req = _create_req(client)

    # Stakeholder cannot triage.
    assert (
        client.post(
            f"/api/requirements/{req['id']}/status", json={"status": "SATISFIED"}
        ).status_code
        == 403
    )

    login("ANALYST")
    resp = client.post(
        f"/api/requirements/{req['id']}/status", json={"status": "IN_PROGRESS"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "IN_PROGRESS"


def test_owner_can_edit_and_delete(client, login):
    login("STAKEHOLDER", email="stake@example.com")
    req = _create_req(client)
    upd = client.patch(
        f"/api/requirements/{req['id']}", json={"priority": "CRITICAL"}
    )
    assert upd.status_code == 200 and upd.json()["priority"] == "CRITICAL"
    assert client.delete(f"/api/requirements/{req['id']}").status_code == 204


# --------------------------------------------------------------------------- #
# API: traceability (report -> requirement)
# --------------------------------------------------------------------------- #
def test_report_links_to_requirement(client, login):
    login("STAKEHOLDER", email="stake@example.com")
    req = _create_req(client)

    login("ANALYST", email="author@example.com")
    nb = client.post("/api/notebooks", json={"title": "nb"}).json()
    report = client.post(
        "/api/reports", json={"notebook_id": nb["id"], "title": "Brief"}
    ).json()

    link = client.put(
        f"/api/reports/{report['id']}/requirements",
        json={"requirement_ids": [req["id"]]},
    )
    assert link.status_code == 200
    assert len(link.json()["requirements"]) == 1

    # Traceability is visible from the requirement side.
    detail = client.get(f"/api/requirements/{req['id']}").json()
    assert [r["id"] for r in detail["reports"]] == [report["id"]]


def test_notebook_links_to_requirement(client, login):
    login("STAKEHOLDER", email="stake@example.com")
    req = _create_req(client)
    login("ANALYST")
    nb = client.post("/api/notebooks", json={"title": "nb"}).json()
    link = client.put(
        f"/api/notebooks/{nb['id']}/requirements",
        json={"requirement_ids": [req["id"]]},
    )
    assert link.status_code == 200
    assert len(link.json()["requirements"]) == 1


# --------------------------------------------------------------------------- #
# Portal
# --------------------------------------------------------------------------- #
def test_stakeholder_portal_intake(client, login):
    login("STAKEHOLDER", email="stake@example.com")
    resp = client.get("/requirements")
    assert resp.status_code == 200
    assert "My requirements" in resp.text

    resp = client.post(
        "/requirements",
        data={"title": "Need supply-chain coverage", "priority": "HIGH"},
    )
    assert resp.status_code == 200
    assert "Need supply-chain coverage" in resp.text  # landed on detail page


def test_analyst_tasking_board_and_status(client, login):
    login("STAKEHOLDER", email="stake@example.com")
    rid = _create_req(client, title="Board item")["id"]

    login("ANALYST")
    board = client.get("/requirements")
    assert board.status_code == 200
    assert "tasking board" in board.text and "Board item" in board.text

    # Move it across the board.
    resp = client.post(f"/requirements/{rid}/status", data={"status": "IN_PROGRESS"})
    assert resp.status_code == 200
    assert "IN PROGRESS" in resp.text


def test_report_editor_links_requirements(client, login):
    login("STAKEHOLDER", email="stake@example.com")
    rid = _create_req(client)["id"]

    login("ANALYST", email="author@example.com")
    nb = client.post("/api/notebooks", json={"title": "nb"}).json()
    report = client.post(
        "/api/reports", json={"notebook_id": nb["id"], "title": "R"}
    ).json()

    edit = client.get(f"/reports/{report['id']}/edit")
    assert edit.status_code == 200
    assert "Requirements satisfied" in edit.text

    resp = client.post(
        f"/reports/{report['id']}/requirements", data={"requirement_ids": [rid]}
    )
    assert resp.status_code == 200
