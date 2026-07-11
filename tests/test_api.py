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


def test_update_source(client, login):
    login("ANALYST")
    nb = _make_notebook(client)
    src = client.post(
        f"/api/notebooks/{nb['id']}/sources", json={"title": "Original source"}
    ).json()

    resp = client.patch(
        f"/api/notebooks/{nb['id']}/sources/{src['id']}",
        json={
            "version": 1,
            "title": "Updated source",
            "reference": "https://example.test/updated",
            "summary": "kept for older records",
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "Updated source"
    assert body["reference"] == "https://example.test/updated"
    assert body["summary"] == "kept for older records"


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

    upd = client.patch(f"/api/reports/{rid}", json={"body_md": "# Heading\n\ntext", "version": 1})
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


def test_stakeholder_cannot_read_unpublished_report(client, login):
    """Regression: the report list hid drafts from stakeholders, but the detail
    view (and products/downloads) leaked them by direct ID — a read-only
    stakeholder could read any unpublished report, including TLP:RED."""
    login("ANALYST", email="author@example.com")
    nb = _make_notebook(client)
    rid = client.post(
        "/api/reports",
        json={"notebook_id": nb["id"], "title": "Secret draft", "tlp": "RED"},
    ).json()["id"]

    login("STAKEHOLDER", email="nosy@example.com")
    assert client.get(f"/api/reports/{rid}").status_code == 404
    assert client.get(f"/api/reports/{rid}/products").status_code == 404
    # An analyst (writer) can still read it.
    login("ANALYST", email="author@example.com")
    assert client.get(f"/api/reports/{rid}").status_code == 200


def test_stakeholder_cannot_read_notebooks(client, login):
    """Regression (S1): raw notebook material (sources/notes/attachments) is
    writer-only — a read-only stakeholder must not list or open notebooks,
    including unpublished collection work."""
    login("ANALYST", email="author@example.com")
    nb = _make_notebook(client)
    client.post(
        f"/api/notebooks/{nb['id']}/sources", json={"title": "secret source"}
    )

    login("STAKEHOLDER", email="nosy@example.com")
    assert client.get("/api/notebooks").status_code == 403
    assert client.get(f"/api/notebooks/{nb['id']}").status_code == 403

    # A writer still has full access.
    login("ANALYST", email="author@example.com")
    assert client.get(f"/api/notebooks/{nb['id']}").status_code == 200


def test_published_report_citations_are_immutable(client, login):
    """Regression: citations bypassed the published-immutability guard, so a
    published product could still be re-cited after the fact."""
    login("ANALYST", email="author@example.com")
    nb = _make_notebook(client)
    src = client.post(
        f"/api/notebooks/{nb['id']}/sources", json={"title": "S"}
    ).json()
    rid = client.post(
        "/api/reports",
        json={"notebook_id": nb["id"], "title": "R", "tlp": "GREEN"},
    ).json()["id"]
    client.post(f"/api/reports/{rid}/transition", json={"target": "IN_REVIEW"})

    login("REVIEWER", email="rev@example.com")
    client.post(f"/api/reports/{rid}/transition", json={"target": "APPROVED"})
    client.post(f"/api/reports/{rid}/transition", json={"target": "PUBLISHED"})

    login("ANALYST", email="author@example.com")
    resp = client.put(
        f"/api/reports/{rid}/citations", json={"source_ids": [src["id"]]}
    )
    assert resp.status_code == 409


def test_report_judgement_scaffolding(client, login):
    """ICD 203 scaffolding: Key Judgements / Key Assumptions / Intelligence Gaps
    are first-class editable fields, and immutable once published (they route
    through ensure_editable, like the body)."""
    login("ANALYST", email="author@example.com")
    nb = _make_notebook(client)
    rid = client.post(
        "/api/reports",
        json={"notebook_id": nb["id"], "title": "R", "tlp": "GREEN"},
    ).json()["id"]

    upd = client.patch(
        f"/api/reports/{rid}",
        json={
            "version": 1,
            "key_judgements": "- We assess **with high confidence**…",
            "key_assumptions": "Logs are authentic.",
            "intelligence_gaps": "Attribution unconfirmed.",
        },
    )
    assert upd.status_code == 200, upd.text
    body = upd.json()
    assert body["key_judgements"].startswith("- We assess")
    assert body["key_assumptions"] == "Logs are authentic."
    assert body["intelligence_gaps"] == "Attribution unconfirmed."

    # Publish, then confirm the scaffolding is locked like the body.
    client.post(f"/api/reports/{rid}/transition", json={"target": "IN_REVIEW"})
    login("REVIEWER", email="rev@example.com")
    client.post(f"/api/reports/{rid}/transition", json={"target": "APPROVED"})
    client.post(f"/api/reports/{rid}/transition", json={"target": "PUBLISHED"})

    login("ANALYST", email="author@example.com")
    version = client.get(f"/api/reports/{rid}").json()["report"]["version"]
    locked = client.patch(
        f"/api/reports/{rid}", json={"key_judgements": "tampered", "version": version}
    )
    assert locked.status_code == 409


def test_report_analytic_confidence(client, login):
    """ICD 203 analytic confidence: an optional LOW/MODERATE/HIGH marking that
    round-trips, can be cleared (set to null), and is locked once published."""
    login("ANALYST", email="author@example.com")
    nb = _make_notebook(client)
    rid = client.post(
        "/api/reports",
        json={"notebook_id": nb["id"], "title": "R", "tlp": "GREEN"},
    ).json()["id"]

    # Defaults to "not stated".
    assert client.get(f"/api/reports/{rid}").json()["report"][
        "analytic_confidence"
    ] is None

    set_high = client.patch(
        f"/api/reports/{rid}", json={"analytic_confidence": "HIGH", "version": 1}
    )
    assert set_high.status_code == 200, set_high.text
    assert set_high.json()["analytic_confidence"] == "HIGH"

    # Explicit null clears it back to "not stated".
    cleared = client.patch(
        f"/api/reports/{rid}", json={"analytic_confidence": None, "version": 2}
    )
    assert cleared.status_code == 200
    assert cleared.json()["analytic_confidence"] is None

    client.patch(f"/api/reports/{rid}", json={"analytic_confidence": "MODERATE", "version": 3})

    # Publish, then confirm the marking is locked like the rest of the product.
    client.post(f"/api/reports/{rid}/transition", json={"target": "IN_REVIEW"})
    login("REVIEWER", email="rev@example.com")
    client.post(f"/api/reports/{rid}/transition", json={"target": "APPROVED"})
    client.post(f"/api/reports/{rid}/transition", json={"target": "PUBLISHED"})

    login("ANALYST", email="author@example.com")
    version = client.get(f"/api/reports/{rid}").json()["report"]["version"]
    locked = client.patch(
        f"/api/reports/{rid}", json={"analytic_confidence": "LOW", "version": version}
    )
    assert locked.status_code == 409


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


def test_preview_product_assembles_and_sanitizes(client, login):
    """The editor's live preview assembles the whole product (Key Judgements +
    body + Assumptions + Gaps) and sanitizes every fragment."""
    login("ANALYST", email="author@example.com")
    nb = _make_notebook(client)
    rid = client.post(
        "/api/reports", json={"notebook_id": nb["id"], "title": "R"}
    ).json()["id"]

    resp = client.post(
        "/api/preview/product",
        json={
            "report_id": rid,
            "body_md": "# Body\n\nNarrative uniquebodyphrase.",
            "key_judgements": "We **assess** uniquekjphrase.",
            "key_assumptions": "An assumption.",
            "intelligence_gaps": "<script>alert(1)</script> a gap.",
        },
    )
    assert resp.status_code == 200
    html = resp.json()["html"]
    assert "Key judgements" in html and "uniquekjphrase" in html
    assert "uniquebodyphrase" in html
    assert "Key assumptions" in html
    assert "Intelligence gaps" in html
    assert "<script" not in html  # every fragment is nh3-sanitized

    # Empty scaffolding fields collapse — only the body renders.
    bare = client.post(
        "/api/preview/product",
        json={"report_id": rid, "body_md": "Just a body."},
    ).json()["html"]
    assert "Key judgements" not in bare
    assert "Just a body." in bare
