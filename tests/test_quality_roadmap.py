"""Quality-roadmap foundation features."""

from pathlib import Path

from sqlmodel import Session, select

from iceberg.models import (
    AuditAction,
    AuditEvent,
    AudienceGroup,
    IntelLevel,
    Notebook,
    ProductFormat,
    RenderedProduct,
    Report,
    TLP,
    User,
)
from iceberg.services import reports as report_service


def _make_report(client, login, *, title="R", body="body", tlp="AMBER"):
    login("ANALYST", email="author@example.com")
    nb = client.post("/api/notebooks", json={"title": "nb"}).json()
    report = client.post(
        "/api/reports",
        json={
            "notebook_id": nb["id"],
            "title": title,
            "body_md": body,
            "tlp": tlp,
        },
    ).json()
    return nb, report


def _publish(client, login, report_id):
    login("ANALYST", email="author@example.com")
    client.post(f"/api/reports/{report_id}/transition", json={"target": "IN_REVIEW"})
    login("REVIEWER", email="rev@example.com")
    client.post(f"/api/reports/{report_id}/transition", json={"target": "APPROVED"})
    resp = client.post(f"/api/reports/{report_id}/transition", json={"target": "PUBLISHED"})
    assert resp.status_code == 200
    return resp.json()


def _tag(client, login, label):
    login("ADMIN", email="admin@example.com")
    resp = client.post("/api/tags", json={"kind": "ACTOR", "label": label})
    assert resp.status_code == 201
    return resp.json()


def test_logout_revokes_cookie_token(client, login):
    login("ANALYST", email="revoked@example.com")
    assert client.get("/api/me").status_code == 200
    assert client.post("/auth/logout").status_code == 200
    assert client.get("/api/me").status_code == 401


def test_hedging_lint_returned_from_preview(client, login):
    login("ANALYST")
    nb, report = _make_report(client, login, body="This could indicate staging.")
    resp = client.post(
        "/api/preview/product",
        json={
            "report_id": report["id"],
            "body_md": "This could indicate staging.",
            "key_judgements": "The actor might return.",
            "key_assumptions": "",
            "intelligence_gaps": "",
        },
    )
    assert resp.status_code == 200
    terms = {w["term"] for w in resp.json()["warnings"]}
    assert {"could", "might"} <= terms
    assert nb["id"]


def test_ai_disabled_is_writer_only_and_audited(client, login, engine):
    _, report = _make_report(client, login)
    login("STAKEHOLDER", email="ro@example.com")
    assert client.post("/api/ai/judgements", json={"report_id": report["id"]}).status_code == 403

    login("ANALYST", email="author@example.com")
    resp = client.post("/api/ai/judgements", json={"report_id": report["id"]})
    assert resp.status_code == 200
    assert resp.json()["available"] is False
    with Session(engine) as session:
        assert session.exec(
            select(AuditEvent).where(AuditEvent.action == AuditAction.AI_ASSIST)
        ).first()


def test_tag_subscription_narrows_dissemination(client, login):
    wanted = _tag(client, login, "Wanted Actor")
    other = _tag(client, login, "Other Actor")
    login("STAKEHOLDER", email="sub@example.com")
    client.patch("/api/me", json={"preferred_intel_level": None, "subscribed_tag_ids": [wanted["id"]]})

    _, miss = _make_report(client, login, title="Miss")
    login("ANALYST", email="author@example.com")
    client.put(f"/api/reports/{miss['id']}/tags", json={"tag_ids": [other["id"]]})
    _publish(client, login, miss["id"])

    _, hit = _make_report(client, login, title="Hit")
    login("ANALYST", email="author@example.com")
    client.put(f"/api/reports/{hit['id']}/tags", json={"tag_ids": [wanted["id"]]})
    _publish(client, login, hit["id"])

    login("STAKEHOLDER", email="sub@example.com")
    titles = [item["report"]["title"] for item in client.get("/api/feed").json()]
    assert titles == ["Hit"]


def test_audience_group_scopes_report_search_and_view(client, login):
    login("STAKEHOLDER", email="allowed@example.com")
    allowed_id = client.get("/api/me").json()["id"]
    login("STAKEHOLDER", email="blocked@example.com")
    blocked_id = client.get("/api/me").json()["id"]

    login("ADMIN", email="admin@example.com")
    group = client.post(
        "/api/audience-groups",
        json={"name": "Exec", "member_user_ids": [allowed_id]},
    ).json()

    _, report = _make_report(client, login, title="Compartmented", body="needtoknowterm")
    _publish(client, login, report["id"])
    login("ADMIN", email="admin@example.com")
    client.put(
        f"/api/audience-groups/reports/{report['id']}",
        json={"group_ids": [group["id"]]},
    )

    login("STAKEHOLDER", email="blocked@example.com")
    assert client.get(f"/api/reports/{report['id']}").status_code == 404
    assert client.get("/api/search", params={"q": "needtoknowterm"}).json()["count"] == 0

    login("STAKEHOLDER", email="allowed@example.com")
    assert client.get(f"/api/reports/{report['id']}").status_code == 200
    assert client.get("/api/search", params={"q": "needtoknowterm"}).json()["count"] == 1
    assert blocked_id


def test_audience_group_api_crud_filters_non_stakeholder_members(client, login, engine):
    login("STAKEHOLDER", email="member@example.com")
    stakeholder_id = client.get("/api/me").json()["id"]
    login("ANALYST", email="analyst-member@example.com")
    analyst_id = client.get("/api/me").json()["id"]

    login("ADMIN", email="admin@example.com")
    created = client.post(
        "/api/audience-groups",
        json={
            "name": "Executive group",
            "description": "Initial",
            "member_user_ids": [stakeholder_id, analyst_id],
        },
    )
    assert created.status_code == 201
    group = created.json()
    with Session(engine) as session:
        saved = session.get(AudienceGroup, group["id"])
        assert {member.id for member in saved.members} == {stakeholder_id}

    renamed = client.patch(
        f"/api/audience-groups/{group['id']}",
        json={"name": "Executive stakeholders", "description": "Updated"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["slug"] == "executive-stakeholders"

    members = client.put(
        f"/api/audience-groups/{group['id']}/members",
        json={"member_user_ids": [analyst_id]},
    )
    assert members.status_code == 200
    with Session(engine) as session:
        saved = session.get(AudienceGroup, group["id"])
        assert saved.members == []

    assert client.delete(f"/api/audience-groups/{group['id']}").status_code == 204
    assert client.get("/api/audience-groups").json() == []


def test_stakeholder_report_list_respects_audience_scope(client, login):
    login("STAKEHOLDER", email="visible@example.com")
    allowed_id = client.get("/api/me").json()["id"]
    login("STAKEHOLDER", email="hidden@example.com")
    blocked_id = client.get("/api/me").json()["id"]
    assert blocked_id

    login("ADMIN", email="admin@example.com")
    group = client.post(
        "/api/audience-groups",
        json={"name": "Need to know", "member_user_ids": [allowed_id]},
    ).json()
    _, report = _make_report(client, login, title="Scoped list product")
    _publish(client, login, report["id"])
    login("ADMIN", email="admin@example.com")
    client.put(
        f"/api/audience-groups/reports/{report['id']}",
        json={"group_ids": [group["id"]]},
    )

    login("STAKEHOLDER", email="hidden@example.com")
    body = client.get("/reports").text
    assert "Scoped list product" not in body

    login("STAKEHOLDER", email="visible@example.com")
    body = client.get("/reports").text
    assert "Scoped list product" in body


def test_portal_preferences_update_tag_subscriptions(client, login, engine):
    tag = _tag(client, login, "Portal Preference Actor")
    login("STAKEHOLDER", email="prefs@example.com")
    page = client.get("/preferences")
    assert page.status_code == 200
    assert "Portal Preference Actor" in page.text

    resp = client.post(
        "/preferences",
        data={"preferred_intel_level": "", "subscribed_tag_ids": str(tag["id"])},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with Session(engine) as session:
        user = session.exec(select(User).where(User.email == "prefs@example.com")).one()
        assert [t.label for t in user.tag_subscriptions] == ["Portal Preference Actor"]


def test_admin_audience_portal_and_report_editor_scope(client, login, engine):
    login("STAKEHOLDER", email="portal-member@example.com")
    stakeholder_id = client.get("/api/me").json()["id"]
    login("ADMIN", email="admin@example.com")
    resp = client.post(
        "/admin/audience",
        data={
            "name": "Portal audience",
            "description": "Created from portal",
            "member_user_ids": str(stakeholder_id),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with Session(engine) as session:
        group = session.exec(select(AudienceGroup).where(AudienceGroup.name == "Portal audience")).one()

    _, report = _make_report(client, login, title="Portal scoped")
    login("ADMIN", email="admin@example.com")
    editor = client.get(f"/reports/{report['id']}/edit")
    assert editor.status_code == 200
    assert "Need-to-know audience" in editor.text
    scoped = client.post(
        f"/reports/{report['id']}/audience",
        data={"group_ids": str(group.id)},
        follow_redirects=False,
    )
    assert scoped.status_code == 303
    with Session(engine) as session:
        saved = session.get(Report, report["id"])
        assert [g.name for g in saved.audience_groups] == ["Portal audience"]


def test_stix_export_for_tagged_published_report(client, login):
    tag = _tag(client, login, "APT Export")
    _, report = _make_report(client, login)
    login("ANALYST", email="author@example.com")
    client.put(f"/api/reports/{report['id']}/tags", json={"tag_ids": [tag["id"]]})
    _publish(client, login, report["id"])

    resp = client.get(f"/api/reports/{report['id']}/stix")
    assert resp.status_code == 200
    bundle = resp.json()
    assert bundle["type"] == "bundle"
    assert any(obj["type"] == "report" for obj in bundle["objects"])
    assert any(obj["type"] == "threat-actor" for obj in bundle["objects"])


def test_report_view_shows_stix_and_related_reports(client, login, engine):
    _, first = _make_report(client, login, title="Credential theft", body="Shared intrusion text")
    _publish(client, login, first["id"])
    _, second = _make_report(client, login, title="Related credential theft", body="Shared intrusion text")
    _publish(client, login, second["id"])

    login("ANALYST", email="author@example.com")
    resp = client.get(f"/reports/{first['id']}")
    assert resp.status_code == 200
    assert "STIX 2.1 bundle" in resp.text
    assert "Related reports" in resp.text
    assert "Related credential theft" in resp.text
    assert engine


def test_source_content_unblocks_heuristic_credibility(client, login):
    login("ANALYST")
    nb = client.post("/api/notebooks", json={"title": "nb"}).json()
    resp = client.post(
        f"/api/notebooks/{nb['id']}/sources",
        json={
            "title": "CISA alert",
            "reference": "https://cisa.gov/news",
            "content_md": "Confirmed exploitation has been observed.",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["content_md"]
    assert resp.json()["credibility"] != "6"


def test_render_retention_prunes_old_rows_and_files(engine, tmp_path, monkeypatch):
    with Session(engine) as session:
        user = User(email="a@example.com", display_name="A")
        session.add(user)
        session.commit()
        session.refresh(user)
        notebook = Notebook(title="nb", owner_id=user.id)
        session.add(notebook)
        session.commit()
        session.refresh(notebook)
        report = Report(
            notebook_id=notebook.id,
            title="R",
            author_id=user.id,
            intel_level=IntelLevel.OPERATIONAL,
            tlp=TLP.AMBER,
        )
        session.add(report)
        session.commit()
        session.refresh(report)
        for i in range(5):
            p = tmp_path / f"r-{i}.pdf"
            p.write_bytes(b"%PDF")
            session.add(
                RenderedProduct(
                    report_id=report.id,
                    format=ProductFormat.FULL,
                    pdf_path=str(p),
                )
            )
        session.commit()
        settings = report_service.get_settings()
        monkeypatch.setattr(settings, "render_retention_keep", 2)
        monkeypatch.setattr(settings, "render_retention_days", 90)
        pruned = report_service.prune_rendered_products(
            session, report_id=report.id, fmt=ProductFormat.FULL
        )
        remaining = session.exec(select(RenderedProduct)).all()
    assert pruned == 3
    assert len(remaining) == 2
    assert len(list(Path(tmp_path).glob("*.pdf"))) == 2
