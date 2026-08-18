"""Guided demo: real lifecycle, strict isolation, and synthetic-data guarantees."""

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import BackgroundTasks
from sqlmodel import Session, select
from starlette.requests import Request

from iceberg import demo_content
from iceberg.config import get_settings
from iceberg.models import (
    Attachment,
    DemoWorkspace,
    DisseminationEvent,
    Figure,
    Notebook,
    OutboxJob,
    ProductFormat,
    ProductUsefulness,
    PublicationSnapshot,
    Report,
    RenderedProduct,
    ReportStatus,
    Requirement,
    RequirementStatus,
    RfiSatisfaction,
    Role,
    Source,
    User,
)
from iceberg.services import (
    demo,
    feedback,
    lifecycle,
    maturity,
    publication,
    storage,
    storage_deletions,
)


def _user(session: Session, email: str, role: Role) -> User:
    row = User(email=email, display_name=email.split("@")[0], role=role)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/demo-test",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )


def _joined_workspace(session: Session):
    analyst = _user(session, "analyst@demo.example", Role.ANALYST)
    stakeholder = _user(session, "stakeholder@demo.example", Role.STAKEHOLDER)
    started = demo.start(session, analyst)
    workspace = demo.join(
        session,
        public_id=started.workspace.public_id,
        join_code=started.join_code,
        stakeholder=stakeholder,
    )
    return workspace, analyst, stakeholder


def test_start_join_and_web_discovery_are_role_safe(client, login, engine):
    login("ANALYST", "demo-owner@example.com")
    start = client.post("/api/demo-workspaces")
    assert start.status_code == 200
    payload = start.json()
    assert payload["join_code"]
    assert payload["progress"]["joined"] is False
    assert payload["join_code"] not in client.get("/demo").text

    login("STAKEHOLDER", "demo-stakeholder@example.com")
    joined = client.post(
        "/api/demo-workspaces/join",
        json={"public_id": payload["public_id"], "join_code": payload["join_code"]},
    )
    assert joined.status_code == 200
    joined_payload = joined.json()
    assert set(joined_payload["progress"]) == {
        "joined",
        "requirement_started",
        "submitted",
        "approved",
        "published",
        "delivered",
        "feedback",
        "complete",
    }
    serialized = joined.text
    for private_field in (
        "body_md",
        "key_judgements",
        "key_assumptions",
        "intelligence_gaps",
        "owner_id",
    ):
        assert private_field not in serialized
    with Session(engine) as session:
        workspace = session.exec(
            select(DemoWorkspace).where(DemoWorkspace.public_id == payload["public_id"])
        ).one()
        state = demo.progress(session, workspace)
        requirement_id = state["requirement"].id
        notebook_id = state["notebook"].id
    page = client.get("/demo")
    assert page.status_code == 200
    assert "DEMO · SYNTHETIC" in page.text
    assert "Nothing here is live intelligence" in page.text
    assert "/help?role=STAKEHOLDER#requirements" in page.text
    assert "/help?role=STAKEHOLDER#dissemination" in page.text
    assert "Open feed" not in page.text
    assert client.delete(f"/api/requirements/{requirement_id}").status_code == 409

    login("ANALYST", "demo-owner@example.com")
    assert client.delete(f"/api/notebooks/{notebook_id}").status_code == 409
    extra_report = client.post(
        "/api/reports",
        json={"notebook_id": notebook_id, "title": "Escaped report"},
    )
    assert extra_report.status_code == 409

    login("STAKEHOLDER", "demo-stakeholder@example.com")
    reused = client.post(
        "/api/demo-workspaces/join",
        json={"public_id": payload["public_id"], "join_code": payload["join_code"]},
    )
    assert reused.status_code in {404, 409}


def test_demo_completes_real_publication_and_feedback_without_external_jobs(engine):
    with Session(engine) as session:
        workspace, analyst, stakeholder = _joined_workspace(session)
        reviewer = _user(session, "reviewer@demo.example", Role.REVIEWER)
        report = session.exec(
            select(Report).where(Report.demo_workspace_id == workspace.id)
        ).one()
        requirement = session.exec(
            select(Requirement).where(Requirement.demo_workspace_id == workspace.id)
        ).one()

        lifecycle.transition(session, report, ReportStatus.IN_REVIEW, actor=analyst)
        lifecycle.transition(session, report, ReportStatus.APPROVED, actor=reviewer)
        publication.publish(
            session,
            report,
            actor=reviewer,
            request=_request(),
            background_tasks=BackgroundTasks(),
        )

        snapshot = session.exec(
            select(PublicationSnapshot).where(
                PublicationSnapshot.report_id == report.id
            )
        ).one()
        assert snapshot.snapshot_hash == report.publication_snapshot_hash
        assert session.exec(
            select(DisseminationEvent).where(
                DisseminationEvent.report_id == report.id,
                DisseminationEvent.stakeholder_id == stakeholder.id,
            )
        ).one()
        assert session.exec(select(OutboxJob)).all() == []

        feedback.submit_feedback(
            session,
            report=report,
            stakeholder=stakeholder,
            usefulness=ProductUsefulness.HIGHLY_USEFUL,
            requirement_id=requirement.id,
            satisfaction=RfiSatisfaction.MET,
            comment="Synthetic scenario answered the fictional decision.",
        )
        session.refresh(requirement)
        assert requirement.status == RequirementStatus.SATISFIED
        assert demo.progress(session, workspace)["complete"] is True
        metrics = maturity.program_maturity(session)
        assert metrics["production"]["total"] == 0
        assert metrics["requirements"]["total"] == 0
        assert metrics["dissemination"]["events_total"] == 0

        source = session.exec(
            select(Source).where(Source.notebook_id == report.notebook_id)
        ).one()
        frozen = snapshot.payload.copy()
        source.content_md = "Later synthetic collection change"
        session.add(source)
        session.commit()
        session.refresh(snapshot)
        assert snapshot.payload == frozen


def test_reset_replaces_only_demo_generation_and_rejects_stale_reset(engine):
    with Session(engine) as session:
        workspace, analyst, _stakeholder = _joined_workspace(session)
        unrelated = Notebook(title="User work", topic="Keep me", owner_id=analyst.id)
        session.add(unrelated)
        session.commit()
        session.refresh(unrelated)
        old = demo.progress(session, workspace)
        old_report = old["report"]
        old_report.title = "Modified disposable title"
        session.add(old_report)
        session.commit()

        demo.reset(
            session,
            workspace=workspace,
            actor=analyst,
            expected_generation=1,
        )
        state = demo.progress(session, workspace)
        assert state["report"].title == demo_content.REPORT_TITLE
        assert session.get(Notebook, unrelated.id).topic == "Keep me"
        assert (
            len(
                session.exec(
                    select(Report).where(Report.demo_workspace_id == workspace.id)
                ).all()
            )
            == 1
        )
        try:
            demo.reset(
                session,
                workspace=workspace,
                actor=analyst,
                expected_generation=1,
            )
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 409
        else:  # pragma: no cover - makes the concurrency contract explicit
            raise AssertionError("stale reset unexpectedly succeeded")


def test_reset_unlinks_only_demo_owned_files(engine, tmp_path, monkeypatch):
    """Reset tombstones only demo-owned objects; the storage worker then removes
    exactly those bytes and leaves an unrelated user-owned object intact."""
    settings = get_settings()
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "attachments_dir", str(tmp_path / "attachments"))
    monkeypatch.setattr(settings, "figures_dir", str(tmp_path / "figures"))
    monkeypatch.setattr(settings, "render_output_dir", str(tmp_path / "renders"))
    store = storage.get_store("local")

    def _store(kind: str, name: str, payload: bytes) -> tuple[str, str, int]:
        source = tmp_path / name
        source.write_bytes(payload)
        digest = storage.file_sha256(source)
        key = storage.new_key(Path(name).suffix, sha256=digest)
        store.put_file(kind, key, source, sha256=digest, content_type="application/octet-stream")
        return key, digest, len(payload)

    with Session(engine) as session:
        workspace, analyst, _stakeholder = _joined_workspace(session)
        state = demo.progress(session, workspace)
        attachment_key, attachment_digest, attachment_size = _store(
            "attachment", "attachment.bin", b"synthetic attachment"
        )
        figure_key, figure_digest, figure_size = _store(
            "figure", "figure.png", b"synthetic figure"
        )
        render_key, render_digest, render_size = _store(
            "render", "report.pdf", b"synthetic render"
        )
        # An unrelated user-owned attachment in the very same backend + prefix.
        control_key, control_digest, control_size = _store(
            "attachment", "user-owned.bin", b"user owned bytes"
        )
        control_notebook = Notebook(title="User work", topic="Keep", owner_id=analyst.id)
        session.add(control_notebook)
        session.commit()
        session.refresh(control_notebook)
        session.add(
            Attachment(
                notebook_id=control_notebook.id,
                original_filename="user-owned.bin",
                stored_filename=control_key,
                storage_backend="local",
                storage_key=control_key,
                content_sha256=control_digest,
                content_type="application/octet-stream",
                file_size=control_size,
            )
        )
        session.add(
            Attachment(
                notebook_id=state["notebook"].id,
                original_filename="attachment.bin",
                stored_filename=attachment_key,
                storage_backend="local",
                storage_key=attachment_key,
                content_sha256=attachment_digest,
                content_type="application/octet-stream",
                file_size=attachment_size,
            )
        )
        session.add(
            Figure(
                notebook_id=state["notebook"].id,
                original_filename="figure.png",
                stored_filename=figure_key,
                storage_backend="local",
                storage_key=figure_key,
                content_sha256=figure_digest,
                content_type="image/png",
                file_size=figure_size,
            )
        )
        session.add(
            RenderedProduct(
                report_id=state["report"].id,
                format=ProductFormat.FULL,
                pdf_path=str(storage.LocalObjectStore().path("render", render_key)),
                storage_backend="local",
                storage_key=render_key,
                content_sha256=render_digest,
                file_size=render_size,
            )
        )
        session.commit()

        demo.reset(
            session,
            workspace=workspace,
            actor=analyst,
            expected_generation=1,
        )

    # The demo tombstones skip the backup grace window, so one bounded worker
    # pass reclaims them immediately.
    storage_deletions.process_due_deletions(bind=engine)

    for kind, key in (
        ("attachment", attachment_key),
        ("figure", figure_key),
        ("render", render_key),
    ):
        with pytest.raises(storage.ObjectMissing):
            store.head(kind, key)
    assert store.head("attachment", control_key).sha256 == control_digest


def test_fixture_is_synthetic_and_contains_no_indicators(engine):
    with Session(engine) as session:
        workspace, _analyst, _stakeholder = _joined_workspace(session)
        state = demo.progress(session, workspace)
        source = session.exec(
            select(Source).where(Source.notebook_id == state["notebook"].id)
        ).one()
        corpus = " ".join(
            [
                source.title,
                source.reference,
                source.summary,
                source.content_md,
                state["requirement"].title,
                state["report"].title,
            ]
        ).lower()
        assert source.reference.startswith("urn:iceberg:demo:")
        assert "http://" not in corpus and "https://" not in corpus
        assert "synthetic" in corpus
        assert demo_content.SCENARIO_VERSION == workspace.scenario_version


def test_demo_workspace_code_is_never_stored_in_plaintext(engine):
    with Session(engine) as session:
        analyst = _user(session, "owner@demo.example", Role.ANALYST)
        started = demo.start(session, analyst)
        stored = session.get(DemoWorkspace, started.workspace.id)
        assert stored.join_code_digest != started.join_code
        assert len(stored.join_code_digest) == 64


def test_expired_join_code_fails_without_revealing_workspace(engine):
    with Session(engine) as session:
        analyst = _user(session, "expired-owner@demo.example", Role.ANALYST)
        stakeholder = _user(
            session, "expired-stakeholder@demo.example", Role.STAKEHOLDER
        )
        started = demo.start(session, analyst)
        started.workspace.join_expires_at = started.workspace.created_at - timedelta(
            seconds=1
        )
        session.add(started.workspace)
        session.commit()
        try:
            demo.join(
                session,
                public_id=started.workspace.public_id,
                join_code=started.join_code,
                stakeholder=stakeholder,
            )
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 404
        else:  # pragma: no cover
            raise AssertionError("expired join code unexpectedly succeeded")
