"""API tests: auth gating, notebook/source/note/report CRUD, citations, preview."""


def _make_notebook(client, title="APT29 tracking", topic="Cozy Bear"):
    resp = client.post("/api/notebooks", json={"title": title, "topic": topic})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_unauthenticated_api_returns_401(client):
    assert client.get("/api/notebooks").status_code == 401


def test_dev_login_then_list(client, login):
    login("ANALYST")
    resp = client.get("/api/notebooks")
    assert resp.status_code == 200
    assert resp.json() == []


def test_stakeholder_cannot_write(client, login):
    login("STAKEHOLDER")
    resp = client.post("/api/notebooks", json={"title": "x"})
    assert resp.status_code == 403


def test_notebook_sources_notes_flow(client, login):
    login("ANALYST")
    nb = _make_notebook(client)

    src = client.post(
        f"/api/notebooks/{nb['id']}/sources",
        json={"title": "Vendor blog", "reference": "https://ex.com", "summary": "s"},
    )
    assert src.status_code == 201

    note = client.post(
        f"/api/notebooks/{nb['id']}/notes", json={"body_md": "working note"}
    )
    assert note.status_code == 201

    detail = client.get(f"/api/notebooks/{nb['id']}").json()
    assert len(detail["sources"]) == 1
    assert len(detail["notes"]) == 1
    assert detail["notebook"]["title"] == "APT29 tracking"


def test_report_create_update_and_citations(client, login):
    login("ANALYST")
    nb = _make_notebook(client)
    src = client.post(
        f"/api/notebooks/{nb['id']}/sources", json={"title": "Src1"}
    ).json()

    report = client.post(
        "/api/reports",
        json={
            "notebook_id": nb["id"],
            "title": "Quarterly threat brief",
            "intel_level": "STRATEGIC",
            "tlp": "AMBER",
        },
    )
    assert report.status_code == 201, report.text
    rid = report.json()["id"]

    upd = client.patch(f"/api/reports/{rid}", json={"body_md": "# Heading\n\ntext"})
    assert upd.status_code == 200
    assert upd.json()["body_md"].startswith("# Heading")

    cite = client.put(
        f"/api/reports/{rid}/citations", json={"source_ids": [src["id"]]}
    )
    assert cite.status_code == 200
    assert len(cite.json()["cited_sources"]) == 1


def test_citation_rejects_foreign_source(client, login):
    """A source from another notebook must not become a citation."""
    login("ANALYST")
    nb_a = _make_notebook(client, title="A")
    nb_b = _make_notebook(client, title="B")
    foreign = client.post(
        f"/api/notebooks/{nb_b['id']}/sources", json={"title": "Foreign"}
    ).json()
    report = client.post(
        "/api/reports", json={"notebook_id": nb_a["id"], "title": "R"}
    ).json()

    cite = client.put(
        f"/api/reports/{report['id']}/citations",
        json={"source_ids": [foreign["id"]]},
    )
    assert cite.status_code == 200
    assert cite.json()["cited_sources"] == []


def test_preview_sanitizes_html(client, login):
    login("ANALYST")
    resp = client.post(
        "/api/preview",
        json={"markdown": "# Title\n\n<script>alert(1)</script>\n\n**bold**"},
    )
    assert resp.status_code == 200
    html = resp.json()["html"]
    assert "<h1" in html
    assert "<strong>bold</strong>" in html
    assert "<script" not in html
