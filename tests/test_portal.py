"""Portal (server-rendered) tests. These drive the Jinja2 templates end-to-end
so template/macro errors surface, and verify the full authoring flow through
the HTML routes."""


def _first_notebook_id(client) -> int:
    return client.get("/api/notebooks").json()[0]["id"]


def _first_report_id(client) -> int:
    return client.get("/api/reports").json()[0]["id"]


def test_login_page_renders(client):
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert "Sign in" in resp.text


def test_dashboard_renders(client, login):
    login("ANALYST")
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Notebooks" in resp.text


def test_full_authoring_flow_through_portal(client, login):
    login("ANALYST", email="author@example.com")

    # Create a notebook via the portal form.
    resp = client.post("/notebooks", data={"title": "Ransomware ops", "topic": "LockBit"})
    assert resp.status_code == 200
    assert "Ransomware ops" in resp.text
    nb_id = _first_notebook_id(client)

    # Add a source.
    resp = client.post(
        f"/notebooks/{nb_id}/sources",
        data={"title": "Leak site", "reference": "http://x.onion", "summary": "obs"},
    )
    assert resp.status_code == 200
    assert "Leak site" in resp.text

    # Create a report -> lands on the editor.
    resp = client.post(
        f"/notebooks/{nb_id}/reports",
        data={"title": "LockBit update", "intel_level": "TACTICAL", "tlp": "AMBER"},
    )
    assert resp.status_code == 200
    assert "Finished preview" in resp.text
    rid = _first_report_id(client)

    # Save body content.
    resp = client.post(
        f"/reports/{rid}",
        data={
            "title": "LockBit update",
            "body_md": "# Summary\n\nNew affiliate activity.",
            "intel_level": "TACTICAL",
            "tlp": "AMBER",
        },
    )
    assert resp.status_code == 200

    # Cite the source.
    src_id = client.get(f"/api/notebooks/{nb_id}").json()["sources"][0]["id"]
    resp = client.post(f"/reports/{rid}/citations", data={"source_ids": [src_id]})
    assert resp.status_code == 200

    # Submit for review.
    resp = client.post(f"/reports/{rid}/transition", data={"target": "IN_REVIEW"})
    assert resp.status_code == 200

    # Reviewer approves and publishes.
    login("REVIEWER", email="rev@example.com")
    assert client.post(f"/reports/{rid}/transition", data={"target": "APPROVED"}).status_code == 200
    assert client.post(f"/reports/{rid}/transition", data={"target": "PUBLISHED"}).status_code == 200

    # Public report page renders the markdown body and cited source.
    resp = client.get(f"/reports/{rid}")
    assert resp.status_code == 200
    assert "<h1" in resp.text and "Summary" in resp.text
    assert "Leak site" in resp.text

    # Reports list renders.
    assert client.get("/reports").status_code == 200


def test_stakeholder_portal_is_read_only(client, login):
    login("STAKEHOLDER")
    # Stakeholder cannot create a notebook through the portal.
    resp = client.post("/notebooks", data={"title": "nope"})
    assert resp.status_code == 403
