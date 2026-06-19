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


# --------------------------------------------------------------------------- #
# FR #42: PIR / GIR / RFI kinds + PIR coverage
# --------------------------------------------------------------------------- #
def _mk(client, title, *, kind="RFI", priority="MEDIUM", **extra):
    body = {"title": title, "kind": kind, "priority": priority,
            "intel_level": "STRATEGIC", **extra}
    resp = client.post("/api/requirements", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_kind_defaults_to_rfi(client, login):
    login("STAKEHOLDER", email="stake@example.com")
    req = _create_req(client)  # no kind supplied
    assert req["kind"] == "RFI"


def test_pir_create_round_trips_fields(client, login):
    login("STAKEHOLDER", email="stake@example.com")
    req = _mk(client, "Board decision support", kind="PIR",
              decision_context="Informs the Q3 board risk decision",
              review_by="2026-07-01")
    assert req["kind"] == "PIR"
    assert req["decision_context"] == "Informs the Q3 board risk decision"
    assert req["review_by"] == "2026-07-01"  # ISO date round-trip


def test_non_pir_blanks_pir_only_fields(client, login):
    login("STAKEHOLDER", email="stake@example.com")
    # An RFI must never carry the PIR-only time-bound fields.
    req = _mk(client, "Ad-hoc question", kind="RFI",
              decision_context="should be dropped", review_by="2026-07-01")
    assert req["decision_context"] == ""
    assert req["review_by"] is None


def test_patch_to_non_pir_clears_pir_fields(client, login):
    login("STAKEHOLDER", email="stake@example.com")
    req = _mk(client, "PIR then demoted", kind="PIR",
              decision_context="ctx", review_by="2026-07-01")
    upd = client.patch(f"/api/requirements/{req['id']}", json={"kind": "RFI"}).json()
    assert upd["kind"] == "RFI"
    assert upd["decision_context"] == "" and upd["review_by"] is None


def test_board_orders_pir_floor_without_burying_critical(client, login):
    """A PIR is floored to HIGH (leads medium/low work), but a CRITICAL RFI of
    any kind still tops the column — the urgency-vs-kind resolution (FR #42)."""
    login("STAKEHOLDER", email="stake@example.com")
    _mk(client, "Zulu critical RFI", kind="RFI", priority="CRITICAL")
    _mk(client, "Alpha low PIR", kind="PIR", priority="LOW")
    _mk(client, "Mike medium GIR", kind="GIR", priority="MEDIUM")

    login("ANALYST")
    # Slice to the board (the coverage panel precedes it and also names PIRs).
    board = client.get("/requirements").text.split('class="board"', 1)[1]
    i_crit = board.index("Zulu critical RFI")
    i_pir = board.index("Alpha low PIR")
    i_gir = board.index("Mike medium GIR")
    assert i_crit < i_pir < i_gir


def test_pir_coverage_panel_lists_gaps_and_excludes_covered(client, login):
    login("STAKEHOLDER", email="stake@example.com")
    uncovered = _mk(client, "Uncovered PIR", kind="PIR")
    covered = _mk(client, "Covered PIR", kind="PIR")
    done = _mk(client, "Done PIR", kind="PIR")

    login("ANALYST")
    # Link a notebook to the covered PIR; mark the done PIR SATISFIED.
    nb = client.post("/api/notebooks", json={"title": "nb"}).json()
    client.put(f"/api/notebooks/{nb['id']}/requirements",
               json={"requirement_ids": [covered["id"]]})
    client.post(f"/api/requirements/{done['id']}/status",
                json={"status": "SATISFIED"})

    panel = client.get("/requirements").text.split('class="board"', 1)[0]
    assert "Uncovered PIR" in panel       # a real collection gap
    assert "Covered PIR" not in panel     # has a linked notebook
    assert "Done PIR" not in panel        # SATISFIED → excluded


def test_pir_overdue_flagged_but_does_not_reorder_board(client, login):
    login("STAKEHOLDER", email="stake@example.com")
    # Same priority + kind; only review_by differs. Created normal-first.
    _mk(client, "Normal PIR", kind="PIR", priority="LOW")
    _mk(client, "Overdue PIR", kind="PIR", priority="LOW", review_by="2020-01-01")

    login("ANALYST")
    html = client.get("/requirements").text
    panel = html.split('class="board"', 1)[0]
    board = html.split('class="board"', 1)[1]
    # Overdue appears in the coverage panel...
    assert "Overdue PIR" in panel
    # ...but the board still orders by created_at (no overdue boost).
    assert board.index("Normal PIR") < board.index("Overdue PIR")


def test_portal_pir_empty_review_by_coerces_to_none(client, login):
    login("STAKEHOLDER", email="stake@example.com")
    resp = client.post("/requirements", data={
        "title": "PIR no date", "kind": "PIR",
        "decision_context": "ctx", "review_by": "", "priority": "HIGH",
    })
    assert resp.status_code == 200  # empty date must not 422
    req = client.get("/api/requirements").json()[0]
    assert req["kind"] == "PIR" and req["review_by"] is None


def test_portal_pir_intake_shows_kind_selector(client, login):
    login("STAKEHOLDER", email="stake@example.com")
    page = client.get("/requirements").text
    assert 'name="kind"' in page and "Requirement kind" in page
