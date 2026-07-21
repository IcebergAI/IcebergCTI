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


def test_notebooks_index_renders_for_writer(client, login):
    login("ANALYST")
    client.post("/notebooks", data={"title": "Volt Typhoon", "topic": "CNI"})
    resp = client.get("/notebooks")
    assert resp.status_code == 200
    assert "Volt Typhoon" in resp.text


def test_notebooks_index_forbidden_for_stakeholder(client, login):
    # Notebooks are writer-only collection material; stakeholders are excluded.
    login("STAKEHOLDER")
    resp = client.get("/notebooks")
    assert resp.status_code == 403


def test_entities_index_renders(client, login):
    login("ANALYST")
    resp = client.get("/tags")
    assert resp.status_code == 200
    # Named-threat kinds head the browse index.
    assert "Threat entities" in resp.text


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
            "version": "1",
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


def test_portal_can_edit_source(client, login):
    login("ANALYST")
    nb = client.post("/api/notebooks", json={"title": "Editable source"}).json()
    src = client.post(
        f"/api/notebooks/{nb['id']}/sources", json={"title": "Original source"}
    ).json()

    resp = client.post(
        f"/notebooks/{nb['id']}/sources/{src['id']}",
        data={
            "title": "Updated source",
            "reference": "https://example.test/updated",
            "summary": "",
            "reliability": "C",
            "credibility": "3",
            "grading_rationale": "Reviewed during source edit.",
        },
    )

    assert resp.status_code == 200
    assert "Source updated." in resp.text
    assert "Updated source" in resp.text
    assert "https://example.test/updated" in resp.text
    assert "C3" in resp.text
    assert "Reviewed during source edit." in resp.text
    assert "Original source" not in resp.text


def test_portal_source_edit_preserves_auto_grade(client, login):
    login("ANALYST")
    nb = client.post("/api/notebooks", json={"title": "Auto source edit"}).json()

    client.post(
        f"/api/notebooks/{nb['id']}/sources",
        json={
            "title": "CISA source",
            "reference": "https://www.cisa.gov/news-events/cybersecurity-advisories/test",
        },
    )
    # Grading is inline and offline; read the graded source back so we resubmit
    # its real (auto) grade unchanged when editing the title.
    src = client.get(f"/api/notebooks/{nb['id']}").json()["sources"][0]
    assert src["grading_origin"] == "AUTO"

    resp = client.post(
        f"/notebooks/{nb['id']}/sources/{src['id']}",
        data={
            "title": "CISA source renamed",
            "reference": src["reference"],
            "summary": "",
            "reliability": src["reliability"],
            "credibility": src["credibility"],
            "grading_rationale": src["grading_rationale"],
        },
    )

    assert resp.status_code == 200
    saved = client.get(f"/api/notebooks/{nb['id']}").json()["sources"][0]
    assert saved["title"] == "CISA source renamed"
    assert saved["grading_origin"] == "AUTO"
    assert saved["grading_engine"] == "heuristic:v1"


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
            "version": "1",
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


def test_report_analytic_confidence_via_portal(client, login):
    """The editor select persists analytic confidence; an empty value coerces to
    None ("not stated"); the report view shows the chip only when set."""
    login("ANALYST", email="author@example.com")
    nb = client.post("/api/notebooks", json={"title": "Confidence nb"}).json()
    rid = client.post(
        "/api/reports", json={"notebook_id": nb["id"], "title": "Assessment"}
    ).json()["id"]

    # Setting a value persists it and renders the masthead chip.
    saved = client.post(
        f"/reports/{rid}",
        data={"version": "1", "title": "Assessment", "analytic_confidence": "HIGH"},
    )
    assert saved.status_code == 200, saved.text
    assert client.get(f"/api/reports/{rid}").json()["report"][
        "analytic_confidence"
    ] == "HIGH"
    view = client.get(f"/reports/{rid}")
    assert "High confidence" in view.text
    # The probability yardstick reference panel + deep-link are in the editor.
    edit = client.get(f"/reports/{rid}/edit")
    assert "Probability yardstick" in edit.text
    assert "Roughly even chance" in edit.text
    assert "/help#estimative-language" in edit.text

    # The "— Not stated —" option posts "", which clears the field.
    client.post(
        f"/reports/{rid}",
        data={"version": "2", "title": "Assessment", "analytic_confidence": ""},
    )
    assert client.get(f"/api/reports/{rid}").json()["report"][
        "analytic_confidence"
    ] is None
    assert 'class="tag conf' not in client.get(f"/reports/{rid}").text


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


def test_valid_token_for_deleted_user_is_anonymous(client, login, engine):
    """A still-valid session token whose user row no longer exists resolves to
    anonymous (not a 500), so a deleted account can't keep browsing."""
    from sqlmodel import Session, select

    from iceberg.models import User

    email = login("ANALYST")
    assert client.get("/notebooks", follow_redirects=False).status_code == 200
    with Session(engine) as session:
        user = session.exec(select(User).where(User.email == email)).one()
        session.delete(user)
        session.commit()
    # Token is still cryptographically valid, but the subject is gone → 401
    # (anonymous), not a 500. A browser request (Accept: text/html) is then
    # redirected to the login page by the auth handler.
    assert client.get("/notebooks", follow_redirects=False).status_code == 401
    resp = client.get(
        "/notebooks",
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_editor_markings_live_in_the_header(client, login):
    """TLP / intel level / confidence are edited from the always-visible header
    chips, and only there — a second copy in the dock footer would be a second
    source of truth for the same three form fields."""
    login("ANALYST", email="marks@example.com")
    nb = client.post("/api/notebooks", json={"title": "Markings nb"}).json()
    rid = client.post(
        "/api/reports", json={"notebook_id": nb["id"], "title": "Marked product"}
    ).json()["id"]

    page = client.get(f"/reports/{rid}/edit").text
    head = page.split('class="editor-head-actions"', 1)[0]
    for field in ("intel_level", "tlp", "analytic_confidence"):
        assert page.count(f'name="{field}"') == 1
        assert f'name="{field}"' in head
    # Each chip's <select> still posts with the product form, so autosave and a
    # plain submit both carry the markings.
    assert head.count('class="marking-chip-select"') == 3
    assert head.count('form="reportform"') == 4  # title input + three markings


def test_editor_markings_are_read_only_for_a_non_author(client, login):
    """A reviewer opening someone else's product sees the markings as static
    chips — visible, but with no editable control smuggled into the page."""
    login("ANALYST", email="author2@example.com")
    nb = client.post("/api/notebooks", json={"title": "Read-only nb"}).json()
    rid = client.post(
        "/api/reports", json={"notebook_id": nb["id"], "title": "Someone else's"}
    ).json()["id"]

    login("REVIEWER", email="reviewer2@example.com")
    page = client.get(f"/reports/{rid}/edit").text
    assert "marking-chip" in page
    assert "marking-chip-select" not in page
    assert 'name="tlp"' not in page


def test_notebook_phases_keep_every_section_reachable(client, login):
    """The nine sections are re-cut into four phases, not removed: each section
    id still renders, and the <noscript> rule cancels x-cloak so a browser
    without Alpine still sees all of them."""
    login("ANALYST", email="phases@example.com")
    nb = client.post("/api/notebooks", json={"title": "Phased nb"}).json()

    page = client.get(f"/notebooks/{nb['id']}").text
    for section in (
        "sources",
        "notes",
        "attachments",
        "figures",
        "indicators",
        "diamonds",
        "ach",
        "products",
        "requirements",
    ):
        assert f'id="{section}"' in page
        assert f'href="#{section}"' in page  # its phase tab
    for phase in ("collect", "analyze", "produce", "trace"):
        assert f"phase === '{phase}'" in page
    # Collect is the server-rendered default: it alone is not cloaked.
    assert "x-show=\"phase === 'collect'\">" in page
    assert "[x-cloak] { display: revert; }" in page


def test_analyst_rail_follows_the_intelligence_cycle(client, login):
    login("ANALYST", email="cycle@example.com")
    page = client.get("/").text
    rail = page.split('class="rail-nav"', 1)[1].split("</nav>", 1)[0]
    labels = ["Workspace", "Collect", "Produce", "Discover"]
    positions = [rail.index(f">{label}</div>") for label in labels]
    assert positions == sorted(positions), "rail groups are out of cycle order"
    # Tasking is collection, not administration.
    collect = rail.split(">Collect</div>", 1)[1].split(">Produce</div>", 1)[0]
    assert "/requirements" in collect and "/notebooks" in collect


def test_admin_rail_collapses_the_config_consoles_behind_the_hub(client, login):
    """Eleven admin links become four; every collapsed console is still one ⌘K
    keystroke away, so nothing became unreachable."""
    login("ADMIN", email="railadmin@example.com")
    page = client.get("/").text
    rail = page.split('class="rail-nav"', 1)[1].split("</nav>", 1)[0]
    admin = rail.split(">Administration</div>", 1)[1]
    assert admin.count("<a href=") == 4
    for collapsed in ("/admin/ai", "/admin/misp", "/admin/proxy", "/admin/webhook",
                      "/admin/oidc", "/admin/config", "/admin/feeds"):
        assert f'href="{collapsed}"' not in admin
        assert f"'href': '{collapsed}'" in page or f'"href": "{collapsed}"' in page


def test_every_command_palette_destination_resolves(client, login):
    """The palette is the safety net for the collapsed rail — a dead entry in it
    would strand a whole console."""
    import json
    import re

    for role in ("ANALYST", "ADMIN", "STAKEHOLDER"):
        login(role, email=f"palette-{role.lower()}@example.com")
        page = client.get("/").text
        raw = re.search(r"x-data='appShell\((\[.*?\])\)'", page, re.S).group(1)
        items = json.loads(raw.replace("&#34;", '"').replace("&amp;", "&"))
        assert items, f"{role} has an empty palette"
        for item in items:
            resp = client.get(item["href"], follow_redirects=False)
            assert resp.status_code in (200, 303), f"{role} → {item['href']}"


def test_editor_has_one_save_model_and_a_publish_tab(client, login):
    """Autosave is the only save path (plus a <noscript> fallback), and the
    lifecycle + render + transition all live under Publish rather than being
    spread over a subhead and a separate tab."""
    login("ANALYST", email="publish@example.com")
    nb = client.post("/api/notebooks", json={"title": "Publish nb"}).json()
    rid = client.post(
        "/api/reports", json={"notebook_id": nb["id"], "title": "Publishable"}
    ).json()["id"]

    page = client.get(f"/reports/{rid}/edit").text
    assert "Save draft" not in page
    assert "All changes saved" in page
    assert "<noscript><button form=\"reportform\"" in page
    assert 'class="editor-subhead"' not in page

    # Four verb-labelled tabs (+ Assist, which is conditional on AI being on).
    tabs = page.split('class="dock-tabs"', 1)[1].split("</div>", 1)[0]
    assert tabs.count("data-editor-tab") in (4, 5)
    for label in (">Cite<", ">Classify<", ">Link<", ">Publish<"):
        assert label in tabs
    # Publish owns the lifecycle stepper, the renders, and the transition.
    publish = page.split('id="editor-panel-publish"', 1)[1]
    assert 'class="flow"' in publish
    assert "Rendered products" in publish
    assert "Submit for review" in page.split('class="dock-foot"', 1)[1]
