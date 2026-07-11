"""Regressions for the general-grivas identity and visibility reports (#198-201)."""

import json

import pytest
from sqlmodel import Session, select

from iceberg.auth import routes as auth_routes
from iceberg.config import get_settings
from iceberg.models import Attachment, AuditEvent, Role, User
from iceberg.services.users import OIDCIdentityCollisionError, upsert_user


def _publish(client, login, report_id: int) -> None:
    login("ANALYST", email="author@example.com")
    submitted = client.post(
        f"/api/reports/{report_id}/transition", json={"target": "IN_REVIEW"}
    )
    assert submitted.status_code == 200, submitted.text
    login("REVIEWER", email="reviewer@example.com")
    approved = client.post(
        f"/api/reports/{report_id}/transition", json={"target": "APPROVED"}
    )
    assert approved.status_code == 200, approved.text
    published = client.post(
        f"/api/reports/{report_id}/transition", json={"target": "PUBLISHED"}
    )
    assert published.status_code == 200, published.text


def _report(client, login, *, title: str, body: str = "") -> tuple[dict, dict]:
    login("ANALYST", email="author@example.com")
    notebook = client.post("/api/notebooks", json={"title": f"{title} notebook"}).json()
    report = client.post(
        "/api/reports",
        json={"notebook_id": notebook["id"], "title": title, "body_md": body},
    ).json()
    return notebook, report


def test_oidc_identity_uses_issuer_subject_and_never_email_fallback(engine):
    with Session(engine) as session:
        first = upsert_user(
            session,
            issuer="https://issuer-a.example.test",
            sub="subject-a",
            email="shared@example.test",
            display_name="Original",
            role=Role.ANALYST,
            department="Original department",
        )
        original_id = first.id
        original_token_version = first.token_version

        with pytest.raises(OIDCIdentityCollisionError):
            upsert_user(
                session,
                issuer="https://issuer-a.example.test",
                sub="subject-b",
                email="shared@example.test",
                display_name="Attacker",
                role=Role.ADMIN,
                department="Mutated",
            )

        session.refresh(first)
        assert first.id == original_id
        assert first.issuer == "https://issuer-a.example.test"
        assert first.sub == "subject-a"
        assert first.role == Role.ANALYST
        assert first.department == "Original department"
        assert first.token_version == original_token_version

        # Same immutable identity can make an unclaimed email change.
        moved = upsert_user(
            session,
            issuer="https://issuer-a.example.test",
            sub="subject-a",
            email="moved@example.test",
            display_name="Original renamed",
            role=Role.REVIEWER,
        )
        assert moved.id == original_id and moved.email == "moved@example.test"

        # A subject is only unique inside its issuer, not globally.
        other_issuer = upsert_user(
            session,
            issuer="https://issuer-b.example.test",
            sub="subject-a",
            email="other@example.test",
            display_name="Other issuer",
            role=Role.STAKEHOLDER,
        )
        assert other_issuer.id != original_id


def test_oidc_callback_rejects_email_collision_without_minting_a_token(
    client, engine, monkeypatch
):
    with Session(engine) as session:
        original = upsert_user(
            session,
            issuer="https://issuer.example.test",
            sub="subject-a",
            email="shared@example.test",
            display_name="Original",
            role=Role.ANALYST,
            department="Original department",
        )
        original_id = original.id

    class FakeEntra:
        async def authorize_access_token(self, _request):
            return {
                "userinfo": {
                    "iss": "https://issuer.example.test",
                    "sub": "subject-b",
                    "email": "shared@example.test",
                    "name": "Collision principal",
                    "roles": ["ADMIN"],
                    "department": "Mutated department",
                }
            }

    class FakeOAuth:
        entra = FakeEntra()

    monkeypatch.setattr(get_settings(), "oidc_enabled", True)
    monkeypatch.setattr(auth_routes, "_get_oauth", lambda: FakeOAuth())
    response = client.get("/auth/callback", follow_redirects=False)
    assert response.status_code == 401
    assert "set-cookie" not in response.headers

    with Session(engine) as session:
        saved = session.get(User, original_id)
        assert saved is not None
        assert saved.sub == "subject-a"
        assert saved.role == Role.ANALYST
        assert saved.department == "Original department"
        assert saved.token_version == 0
        assert session.exec(select(User)).all() == [saved]


def test_admin_must_explicitly_link_a_subjectless_legacy_account(client, login, engine):
    with Session(engine) as session:
        legacy = User(
            email="legacy@example.test",
            display_name="Legacy account",
            role=Role.STAKEHOLDER,
        )
        session.add(legacy)
        session.commit()
        session.refresh(legacy)
        legacy_id = legacy.id

    login("ADMIN", email="admin@example.test")
    linked = client.post(
        f"/api/admin/users/{legacy_id}/oidc-identity",
        json={"issuer": "https://issuer.example.test", "subject": "legacy-subject"},
    )
    assert linked.status_code == 200, linked.text
    assert linked.json() == {"id": legacy_id}

    with Session(engine) as session:
        saved = session.get(User, legacy_id)
        assert saved.issuer == "https://issuer.example.test"
        assert saved.sub == "legacy-subject"
        assert saved.email == "legacy@example.test"
        assert saved.role == Role.STAKEHOLDER


def test_stakeholder_report_detail_never_serializes_collection_records(client, login, engine):
    notebook, report = _report(
        client,
        login,
        title="Finished product",
        body="Finished reporting only.",
    )
    login("ANALYST", email="author@example.com")
    source = client.post(
        f"/api/notebooks/{notebook['id']}/sources",
        json={"title": "Public citation", "reference": "https://example.test/source"},
    ).json()
    with Session(engine) as session:
        from iceberg.models import Source

        source_row = session.get(Source, source["id"])
        source_row.content_md = "SYNTHETIC-COLLECTION-SECRET"
        source_row.ai_provenance = {"secret": "SYNTHETIC-AI-PROVENANCE"}
        attachment = Attachment(
            notebook_id=notebook["id"],
            title="Supporting evidence",
            original_filename="evidence.pdf",
            stored_filename="SYNTHETIC-STORAGE-KEY.pdf",
            content_type="application/pdf",
            file_size=42,
            summary="SYNTHETIC-ATTACHMENT-SUMMARY",
        )
        session.add(attachment)
        session.commit()
        session.refresh(attachment)
        attachment_id = attachment.id

    assert client.put(
        f"/api/reports/{report['id']}/citations", json={"source_ids": [source["id"]]}
    ).status_code == 200
    assert client.put(
        f"/api/reports/{report['id']}/attachments",
        json={"attachment_ids": [attachment_id]},
    ).status_code == 200
    _publish(client, login, report["id"])

    login("STAKEHOLDER", email="stakeholder@example.test")
    detail = client.get(f"/api/reports/{report['id']}")
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    rendered = json.dumps(payload)
    for forbidden in (
        "SYNTHETIC-COLLECTION-SECRET",
        "SYNTHETIC-AI-PROVENANCE",
        "SYNTHETIC-STORAGE-KEY",
        "SYNTHETIC-ATTACHMENT-SUMMARY",
        "content_md",
        "notebook_id",
        "ai_provenance",
        "grading_engine",
        "stored_filename",
    ):
        assert forbidden not in rendered
    assert payload["cited_sources"][0]["title"] == "Public citation"
    assert payload["cited_sources"][0]["reference"] == "https://example.test/source"
    assert set(payload["cited_sources"][0]) == {
        "title",
        "reference",
        "reliability",
        "credibility",
    }
    assert payload["cited_attachments"][0]["original_filename"] == "evidence.pdf"


def test_stakeholder_dashboard_and_traceability_apply_visibility_boundaries(
    client, login
):
    login("STAKEHOLDER", email="allowed@example.test")
    allowed_id = client.get("/api/me").json()["id"]
    login("STAKEHOLDER", email="excluded@example.test")
    excluded_id = client.get("/api/me").json()["id"]
    excluded_requirement = client.post(
        "/api/requirements", json={"title": "Excluded stakeholder requirement"}
    ).json()
    login("ADMIN", email="admin@example.test")
    group = client.post(
        "/api/audience-groups",
        json={"name": "Allowed only", "member_user_ids": [allowed_id]},
    ).json()

    hidden_notebook, hidden_report = _report(
        client,
        login,
        title="SYNTHETIC-RESTRICTED-TITLE",
        body="SYNTHETIC-DRAFT-BODY",
    )
    login("ANALYST", email="author@example.com")
    assert client.patch(
        f"/api/notebooks/{hidden_notebook['id']}",
        json={"title": "SYNTHETIC-COVERT-NOTEBOOK"},
    ).status_code == 200
    assert client.put(
        f"/api/notebooks/{hidden_notebook['id']}/requirements",
        json={"requirement_ids": [excluded_requirement["id"]]},
    ).status_code == 200
    assert client.put(
        f"/api/reports/{hidden_report['id']}/requirements",
        json={"requirement_ids": [excluded_requirement["id"]]},
    ).status_code == 200
    _publish(client, login, hidden_report["id"])
    login("ADMIN", email="admin@example.test")
    assert client.put(
        f"/api/audience-groups/reports/{hidden_report['id']}",
        json={"group_ids": [group["id"]]},
    ).status_code == 200

    # A separate visible product proves the stakeholder still receives a safe
    # summary for reports they are actually allowed to read.
    visible_notebook, visible_report = _report(
        client,
        login,
        title="Visible linked report",
        body="SYNTHETIC-VISIBLE-BODY",
    )
    assert hidden_notebook and visible_notebook
    login("ANALYST", email="author@example.com")
    assert client.put(
        f"/api/reports/{visible_report['id']}/requirements",
        json={"requirement_ids": [excluded_requirement["id"]]},
    ).status_code == 200
    _publish(client, login, visible_report["id"])

    # Link a second stakeholder's requirement to the visible report.  Its title
    # must not surface when the excluded stakeholder opens that product.
    login("STAKEHOLDER", email="allowed@example.test")
    other_requirement = client.post(
        "/api/requirements", json={"title": "SYNTHETIC-OTHER-OWNER-REQUIREMENT"}
    ).json()
    login("ANALYST", email="author@example.com")
    assert client.put(
        f"/api/reports/{visible_report['id']}/requirements",
        json={
            "requirement_ids": [
                excluded_requirement["id"],
                other_requirement["id"],
            ]
        },
    ).status_code == 200

    login("STAKEHOLDER", email="excluded@example.test")
    assert client.get(f"/api/reports/{hidden_report['id']}").status_code == 404
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "SYNTHETIC-RESTRICTED-TITLE" not in dashboard.text

    detail = client.get(f"/api/requirements/{excluded_requirement['id']}")
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert "notebooks" not in payload
    assert [report["title"] for report in payload["reports"]] == ["Visible linked report"]
    assert "body_md" not in payload["reports"][0]
    assert "notebook_id" not in payload["reports"][0]
    assert "SYNTHETIC-DRAFT-BODY" not in json.dumps(payload)

    requirement_page = client.get(f"/requirements/{excluded_requirement['id']}")
    assert "SYNTHETIC-RESTRICTED-TITLE" not in requirement_page.text
    assert "SYNTHETIC-COVERT-NOTEBOOK" not in requirement_page.text
    assert "Notebooks addressing this" not in requirement_page.text

    report_page = client.get(f"/reports/{visible_report['id']}")
    assert report_page.status_code == 200
    assert "SYNTHETIC-OTHER-OWNER-REQUIREMENT" not in report_page.text
    assert excluded_id


def test_audience_mutations_are_atomic_and_group_deletion_fails_closed(
    client, login, engine
):
    login("STAKEHOLDER", email="allowed@example.test")
    allowed_id = client.get("/api/me").json()["id"]
    login("STAKEHOLDER", email="excluded@example.test")
    excluded_id = client.get("/api/me").json()["id"]
    login("ADMIN", email="admin@example.test")
    group = client.post(
        "/api/audience-groups",
        json={"name": "Restricted", "member_user_ids": [allowed_id]},
    ).json()
    _, report = _report(client, login, title="Restricted report")
    _publish(client, login, report["id"])

    login("ADMIN", email="admin@example.test")
    scoped = client.put(
        f"/api/audience-groups/reports/{report['id']}",
        json={"group_ids": [group["id"]]},
    )
    assert scoped.status_code == 200, scoped.text
    login("STAKEHOLDER", email="excluded@example.test")
    assert client.get(f"/api/reports/{report['id']}").status_code == 404

    login("ADMIN", email="admin@example.test")
    invalid_api = client.put(
        f"/api/audience-groups/reports/{report['id']}",
        json={"group_ids": [group["id"], 999999]},
    )
    assert invalid_api.status_code == 422
    invalid_portal = client.post(
        f"/reports/{report['id']}/audience",
        data={"group_ids": [str(group["id"]), "999999"]},
        follow_redirects=False,
    )
    assert invalid_portal.status_code == 422
    assert client.delete(f"/api/audience-groups/{group['id']}").status_code == 409
    assert (
        client.post(
            f"/admin/audience/{group['id']}/delete", follow_redirects=False
        ).status_code
        == 409
    )

    login("STAKEHOLDER", email="excluded@example.test")
    assert client.get(f"/api/reports/{report['id']}").status_code == 404

    login("ADMIN", email="admin@example.test")
    unscoped = client.put(
        f"/api/audience-groups/reports/{report['id']}", json={"group_ids": []}
    )
    assert unscoped.status_code == 200 and unscoped.json()["groups"] == []
    login("STAKEHOLDER", email="excluded@example.test")
    assert client.get(f"/api/reports/{report['id']}").status_code == 200

    with Session(engine) as session:
        scope_events = list(
            session.exec(
                select(AuditEvent).where(AuditEvent.action == "AUDIENCE_SCOPE_UPDATED")
            ).all()
        )
    assert len(scope_events) >= 2
    assert any(event.detail["unscoped"] for event in scope_events)
    assert excluded_id
