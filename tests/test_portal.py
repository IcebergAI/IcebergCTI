"""Portal (server-rendered) tests. These drive the Jinja2 templates end-to-end
so template/macro errors surface, and verify the full authoring flow through
the HTML routes."""

from sqlmodel import Session

from iceberg.models import ProductFormat, RenderedProduct


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


def test_report_citation_update_returns_to_citation_section(client, login):
    login("ANALYST")
    nb = client.post("/api/notebooks", json={"title": "Source trail"}).json()
    src = client.post(
        f"/api/notebooks/{nb['id']}/sources",
        json={"title": "Primary source", "reference": "https://example.test"},
    ).json()
    report = client.post(
        "/api/reports",
        json={
            "notebook_id": nb["id"],
            "title": "Source-backed report",
            "intel_level": "TACTICAL",
            "tlp": "AMBER",
        },
    ).json()

    resp = client.post(
        f"/reports/{report['id']}/citations",
        data={"source_ids": [src["id"]]},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"].endswith(
        f"/reports/{report['id']}/edit?updated=citations#citations"
    )

    saved = client.get(f"/reports/{report['id']}/edit?updated=citations")
    assert saved.status_code == 200
    assert "Update citations" not in saved.text
    assert "Citations updated." not in saved.text


def test_report_citation_autosave_returns_no_content(client, login):
    login("ANALYST")
    nb = client.post("/api/notebooks", json={"title": "Autosave trail"}).json()
    src = client.post(
        f"/api/notebooks/{nb['id']}/sources",
        json={"title": "Background source", "reference": "https://example.test"},
    ).json()
    report = client.post(
        "/api/reports",
        json={
            "notebook_id": nb["id"],
            "title": "Autosaved report",
            "intel_level": "TACTICAL",
            "tlp": "AMBER",
        },
    ).json()

    resp = client.post(
        f"/reports/{report['id']}/citations",
        data={"source_ids": [src["id"]]},
        headers={"X-Requested-With": "fetch"},
    )

    assert resp.status_code == 204
    detail = client.get(f"/api/reports/{report['id']}").json()
    assert detail["cited_sources"][0]["id"] == src["id"]


def test_report_judgement_scaffolding_persists_and_renders(client, login):
    login("ANALYST", email="author@example.com")
    nb = client.post("/api/notebooks", json={"title": "Scaffolding nb"}).json()
    rid = client.post(
        "/api/reports", json={"notebook_id": nb["id"], "title": "Assessment"}
    ).json()["id"]

    # Editor form posts the body alongside the three scaffolding fields.
    saved = client.post(
        f"/reports/{rid}",
        data={
            "title": "Assessment",
            "body_md": "Narrative body.",
            "key_judgements": "We assess the intrusion is ongoing.",
            "key_assumptions": "Telemetry is complete.",
            "intelligence_gaps": "Initial access vector unknown.",
        },
    )
    assert saved.status_code == 200, saved.text

    detail = client.get(f"/api/reports/{rid}").json()["report"]
    assert detail["key_judgements"] == "We assess the intrusion is ongoing."
    assert detail["intelligence_gaps"] == "Initial access vector unknown."

    # Report view renders the three sections.
    view = client.get(f"/reports/{rid}")
    assert view.status_code == 200
    assert "Key judgements" in view.text
    assert "We assess the intrusion is ongoing." in view.text
    assert "Key assumptions" in view.text
    assert "Intelligence gaps" in view.text

    # The editor seeds its preview with the same assembled product, so the
    # read-only / live-preview pane shows the scaffolding, not just the body.
    edit = client.get(f"/reports/{rid}/edit")
    assert edit.status_code == 200
    assert "Key judgements" in edit.text
    assert "Intelligence gaps" in edit.text


def test_rendered_product_can_be_deleted_from_portal(
    client, login, engine, tmp_path
):
    login("ANALYST", email="author@example.com")
    nb = client.post("/api/notebooks", json={"title": "Rendered trail"}).json()
    report = client.post(
        "/api/reports",
        json={
            "notebook_id": nb["id"],
            "title": "Rendered report",
            "intel_level": "TACTICAL",
            "tlp": "AMBER",
        },
    ).json()
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-1.7\n%%EOF")

    with Session(engine) as session:
        product = RenderedProduct(
            report_id=report["id"],
            format=ProductFormat.FULL,
            pdf_path=str(pdf),
        )
        session.add(product)
        session.commit()
        session.refresh(product)
        product_id = product.id

    edit = client.get(f"/reports/{report['id']}/edit")
    assert edit.status_code == 200
    assert f"/reports/{report['id']}/products/{product_id}/delete" in edit.text
    assert "Delete rendered product" in edit.text

    resp = client.post(
        f"/reports/{report['id']}/products/{product_id}/delete",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].endswith(
        f"/reports/{report['id']}/edit#rendered-products"
    )
    saved = client.get(resp.headers["location"])
    assert "PDF product rendered." not in saved.text
    assert not pdf.exists()
    assert client.get(f"/api/reports/{report['id']}/products").json() == []


def test_stakeholder_portal_is_read_only(client, login):
    login("STAKEHOLDER")
    # Stakeholder cannot create a notebook through the portal.
    resp = client.post("/notebooks", data={"title": "nope"})
    assert resp.status_code == 403


def test_stakeholder_cannot_browse_notebooks_in_portal(client, login):
    """Regression (S1): the portal must not expose notebook collection material
    to read-only stakeholders — not the detail page, and not via the dashboard
    (which previously listed every notebook and the latest reports incl. drafts)."""
    login("ANALYST", email="author@example.com")
    nb = client.post("/api/notebooks", json={"title": "Covert tracking"}).json()
    client.post(
        "/api/reports", json={"notebook_id": nb["id"], "title": "Draft secret"}
    )

    login("STAKEHOLDER", email="nosy@example.com")
    assert client.get(f"/notebooks/{nb['id']}").status_code == 403
    dash = client.get("/")
    assert dash.status_code == 200
    assert "Covert tracking" not in dash.text  # notebook list not leaked
    assert "Draft secret" not in dash.text  # unpublished report not leaked


def test_csrf_blocks_cross_origin_cookie_post(client, login):
    """S2: a cookie-authenticated state-changing request from a foreign origin is
    blocked; same-origin requests and Bearer API clients are allowed."""
    login("ANALYST")

    # Cross-origin POST carrying the session cookie -> blocked.
    blocked = client.post(
        "/api/notebooks",
        json={"title": "evil"},
        headers={"origin": "http://evil.example"},
    )
    assert blocked.status_code == 403

    # Same-origin (the fixture's default Origin) -> allowed.
    assert client.post("/api/notebooks", json={"title": "fine"}).status_code == 201

    # A Bearer API client is not browser-CSRF-prone, so origin is not enforced.
    token = client.cookies["iceberg_session"]
    via_token = client.post(
        "/api/notebooks",
        json={"title": "via token"},
        headers={"origin": "http://evil.example", "authorization": f"Bearer {token}"},
    )
    assert via_token.status_code == 201


def test_logout_requires_post(client, login):
    """S2: logout is POST-only (no GET side effect) and clears the session."""
    login("ANALYST")
    assert client.get("/auth/logout").status_code == 405
    assert client.post("/auth/logout").status_code == 200  # follows redirect to login
