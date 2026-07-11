"""Durable outbox/worker behavior for notification and RSS work (#177)."""

from sqlmodel import Session, select

from iceberg.models import (
    JobKind,
    JobStatus,
    Notebook,
    OutboxJob,
    Report,
    ReportStatus,
    Role,
    User,
    utcnow,
)
from iceberg.services import dissemination, jobs


def _published_report(session: Session) -> tuple[Report, User]:
    author = User(email="author@example.test", display_name="Author", role=Role.ANALYST)
    stakeholder = User(
        email="stakeholder@example.test",
        display_name="Stakeholder",
        role=Role.STAKEHOLDER,
    )
    session.add(author)
    session.add(stakeholder)
    session.commit()
    notebook = Notebook(title="Outbox", owner_id=author.id)
    session.add(notebook)
    session.commit()
    report = Report(
        notebook_id=notebook.id,
        author_id=author.id,
        title="Finished product",
        status=ReportStatus.PUBLISHED,
        published_at=utcnow(),
    )
    session.add(report)
    session.commit()
    session.refresh(report)
    session.refresh(stakeholder)
    return report, stakeholder


def test_email_job_is_durable_idempotent_and_processed(engine, monkeypatch):
    sent: list[tuple] = []
    monkeypatch.setattr(
        dissemination,
        "deliver_email_notification",
        lambda *args: sent.append(args),
    )
    with Session(engine) as session:
        report, stakeholder = _published_report(session)
        first = jobs.enqueue(
            session,
            kind=JobKind.DISSEMINATION_EMAIL,
            payload={"report_id": report.id, "stakeholder_id": stakeholder.id},
            idempotency_key=f"publication:{report.id}:email:{stakeholder.id}",
        )
        duplicate = jobs.enqueue(
            session,
            kind=JobKind.DISSEMINATION_EMAIL,
            payload={"report_id": report.id, "stakeholder_id": stakeholder.id},
            idempotency_key=f"publication:{report.id}:email:{stakeholder.id}",
        )
        assert duplicate.id == first.id
        session.commit()
        job_id = first.id
        report_id = report.id

    result = jobs.process_due_jobs(bind=engine, worker_id="test-worker")
    assert (result.processed, result.succeeded, result.retried, result.failed) == (1, 1, 0, 0)
    assert sent == [
        ("stakeholder@example.test", "Stakeholder", "Finished product", report_id)
    ]
    with Session(engine) as session:
        saved = session.get(OutboxJob, job_id)
        assert saved.status == JobStatus.SUCCEEDED
        assert saved.attempt_count == 1 and saved.last_error == ""


def test_failed_job_retries_then_remains_inspectable(engine, monkeypatch):
    monkeypatch.setattr(
        dissemination,
        "deliver_email_notification",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("SMTP down")),
    )
    with Session(engine) as session:
        report, stakeholder = _published_report(session)
        job = jobs.enqueue(
            session,
            kind=JobKind.DISSEMINATION_EMAIL,
            payload={"report_id": report.id, "stakeholder_id": stakeholder.id},
            idempotency_key="retry-email",
            max_attempts=2,
        )
        session.commit()
        job_id = job.id

    first = jobs.process_due_jobs(bind=engine, worker_id="retry-one")
    assert (first.retried, first.failed) == (1, 0)
    with Session(engine) as session:
        saved = session.get(OutboxJob, job_id)
        assert saved.status == JobStatus.PENDING
        assert saved.retry_count == 1 and "SMTP down" in saved.last_error
        saved.available_at = utcnow()
        session.add(saved)
        session.commit()

    second = jobs.process_due_jobs(bind=engine, worker_id="retry-two")
    assert (second.retried, second.failed) == (0, 1)
    with Session(engine) as session:
        saved = session.get(OutboxJob, job_id)
        assert saved.status == JobStatus.FAILED
        assert saved.attempt_count == 2 and saved.retry_count == 2


def test_expired_lease_can_be_recovered_by_another_worker(engine):
    with Session(engine) as session:
        job = jobs.enqueue(
            session,
            kind=JobKind.RSS_POLL,
            payload={"scheduled": False},
            idempotency_key="lease-recovery",
        )
        session.commit()
        job_id = job.id

    first = jobs._claim_next(engine, "first-worker")
    assert first is not None
    assert jobs._claim_next(engine, "second-worker") is None
    with Session(engine) as session:
        saved = session.get(OutboxJob, job_id)
        saved.lease_expires_at = utcnow().replace(year=2000)
        session.add(saved)
        session.commit()

    replacement = jobs._claim_next(engine, "second-worker")
    assert replacement is not None
    assert replacement.id == job_id and replacement.lease_token != first.lease_token


def test_rss_job_executes_through_worker(engine, monkeypatch):
    calls: list[bool] = []
    from iceberg.services import feeds

    monkeypatch.setattr(
        feeds,
        "fetch_all_enabled_for_job",
        lambda _session: calls.append(True) or 0,
    )
    with Session(engine) as session:
        job = jobs.enqueue_rss_poll(session, scheduled=False)
        session.commit()
        job_id = job.id

    result = jobs.process_due_jobs(bind=engine, worker_id="rss-worker")
    assert result.succeeded == 1 and calls == [True]
    with Session(engine) as session:
        assert session.get(OutboxJob, job_id).status == JobStatus.SUCCEEDED


def test_publish_keeps_feed_delivery_synchronous_and_queues_email(client, login, engine, monkeypatch):
    monkeypatch.setattr(jobs, "schedule_worker", lambda *_args, **_kwargs: None)
    login("STAKEHOLDER", email="recipient@example.test")
    recipient_id = client.get("/api/me").json()["id"]
    login("ANALYST", email="author@example.test")
    notebook = client.post("/api/notebooks", json={"title": "Publication"}).json()
    report = client.post(
        "/api/reports", json={"notebook_id": notebook["id"], "title": "Queued"}
    ).json()
    assert client.post(
        f"/api/reports/{report['id']}/transition", json={"target": "IN_REVIEW"}
    ).status_code == 200
    login("REVIEWER", email="reviewer@example.test")
    assert client.post(
        f"/api/reports/{report['id']}/transition", json={"target": "APPROVED"}
    ).status_code == 200
    assert client.post(
        f"/api/reports/{report['id']}/transition", json={"target": "PUBLISHED"}
    ).status_code == 200

    with Session(engine) as session:
        job = session.exec(
            select(OutboxJob).where(OutboxJob.kind == JobKind.DISSEMINATION_EMAIL)
        ).one()
        assert job.status == JobStatus.PENDING
        assert job.payload == {"report_id": report["id"], "stakeholder_id": recipient_id}
