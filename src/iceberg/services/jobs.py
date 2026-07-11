"""Small, durable database outbox for external Iceberg work.

The web application writes a job in the same transaction as the state that
caused it.  A worker claims jobs with an expiring lease, executes the external
operation outside that transaction, and records success or an inspectable,
backed-off failure.  This keeps publication feed records synchronous while
email, webhooks and RSS retrieval survive process restarts.
"""

from __future__ import annotations

import logging
import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, or_, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from ..config import get_settings
from ..models import (
    JobKind,
    JobStatus,
    OutboxJob,
    ProxyMode,
    ProxySettings,
    Report,
    ReportStatus,
    User,
    WebhookSettings,
    utcnow,
)

logger = logging.getLogger("iceberg.jobs")

_MAX_ERROR_LENGTH = 1000
_MAX_RETRY_DELAY_SECONDS = 60 * 60


@dataclass(frozen=True)
class ClaimedJob:
    """A detached job claim safe to carry across the external call."""

    id: int
    kind: JobKind
    payload: dict
    lease_token: str


@dataclass
class WorkerResult:
    """Inspectable summary returned by one bounded worker pass."""

    processed: int = 0
    succeeded: int = 0
    retried: int = 0
    failed: int = 0


def _max_attempts() -> int:
    return max(1, int(get_settings().jobs_max_attempts))


def _lease_duration() -> timedelta:
    return timedelta(seconds=max(1, int(get_settings().jobs_lease_seconds)))


def _retry_delay(attempt_count: int) -> timedelta:
    """Bounded exponential retry delay, starting at the configured base."""

    base = max(1, int(get_settings().jobs_retry_base_seconds))
    exponent = max(0, min(attempt_count - 1, 16))
    seconds = min(base * (2**exponent), _MAX_RETRY_DELAY_SECONDS)
    return timedelta(seconds=seconds)


def enqueue(
    session: Session,
    *,
    kind: JobKind,
    payload: Mapping[str, object],
    idempotency_key: str,
    max_attempts: int | None = None,
    available_at: datetime | None = None,
) -> OutboxJob:
    """Add one job to the caller's transaction, without committing it.

    The idempotency key makes publication and multi-replica RSS scheduling safe
    to repeat.  A savepoint contains the narrow unique-key race without
    committing or rolling back the caller's surrounding transaction.
    """

    key = idempotency_key.strip()
    if not key:
        raise ValueError("Outbox jobs require an idempotency key")

    existing = session.exec(
        select(OutboxJob).where(OutboxJob.idempotency_key == key)
    ).first()
    if existing is not None:
        return existing

    job = OutboxJob(
        kind=JobKind(kind),
        payload=dict(payload),
        idempotency_key=key,
        max_attempts=max(1, max_attempts or _max_attempts()),
        available_at=available_at or utcnow(),
    )
    try:
        # ``flush`` is intentional: it proves the unique key while leaving the
        # caller's report/audit/feed transaction uncommitted.
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        existing = session.exec(
            select(OutboxJob).where(OutboxJob.idempotency_key == key)
        ).first()
        if existing is not None:
            return existing
        raise
    return job


def enqueue_rss_poll(
    session: Session,
    *,
    scheduled: bool,
    now: datetime | None = None,
) -> OutboxJob:
    """Queue an RSS poll; scheduled ticks dedupe across app replicas.

    Manual fetches deliberately receive a fresh key, while recurring ticks use
    an interval bucket so several application workers only create one durable
    job for the same due period.
    """

    due = now or utcnow()
    if scheduled:
        interval_seconds = max(
            1, int(get_settings().rss_poll_interval_minutes * 60)
        )
        bucket = int(due.timestamp()) // interval_seconds
        key = f"rss-poll:scheduled:{bucket}"
    else:
        key = f"rss-poll:manual:{uuid4().hex}"
    return enqueue(
        session,
        kind=JobKind.RSS_POLL,
        payload={"scheduled": scheduled},
        idempotency_key=key,
        available_at=due,
    )


def schedule_worker(background_tasks, *, limit: int = 25) -> None:
    """Best-effort post-commit kick for a durable job row.

    Call this only after the transaction that inserted the job commits.  The
    independent ``iceberg-worker`` command remains the recovery path if the
    process exits before this FastAPI background task starts.
    """

    if background_tasks is not None:
        background_tasks.add_task(process_due_jobs, limit=limit)


def list_jobs(
    session: Session,
    *,
    status: JobStatus | None = None,
    limit: int = 100,
) -> list[OutboxJob]:
    """Return newest jobs for an operator/CLI inspection view."""

    statement = select(OutboxJob)
    if status is not None:
        statement = statement.where(OutboxJob.status == JobStatus(status))
    statement = statement.order_by(OutboxJob.created_at.desc()).limit(max(1, limit))
    return list(session.exec(statement).all())


def _claimable_clause(now: datetime):
    return or_(
        and_(
            OutboxJob.status == JobStatus.PENDING,
            OutboxJob.available_at <= now,
        ),
        and_(
            OutboxJob.status == JobStatus.RUNNING,
            OutboxJob.lease_expires_at.is_not(None),
            OutboxJob.lease_expires_at <= now,
        ),
    )


def _default_worker_id() -> str:
    return f"{socket.gethostname() or 'worker'}:{os.getpid()}"


def _claim_next(bind: Engine, worker_id: str) -> ClaimedJob | None:
    """Atomically lease one due job, including jobs orphaned by a dead worker."""

    now = utcnow()
    with Session(bind) as session:
        candidate_ids = list(
            session.exec(
                select(OutboxJob.id)
                .where(_claimable_clause(now))
                .order_by(OutboxJob.available_at, OutboxJob.id)
                # A short candidate list avoids a busy loop if another worker
                # wins the race for the oldest row between SELECT and UPDATE.
                .limit(16)
            ).all()
        )
        for job_id in candidate_ids:
            if job_id is None:  # defensive — primary keys are non-null in DB
                continue
            token = uuid4().hex
            claimed = session.execute(
                update(OutboxJob)
                .where(OutboxJob.id == job_id, _claimable_clause(now))
                .values(
                    status=JobStatus.RUNNING,
                    lease_token=token,
                    leased_by=worker_id[:255],
                    leased_at=now,
                    lease_expires_at=now + _lease_duration(),
                    started_at=now,
                    attempt_count=OutboxJob.attempt_count + 1,
                )
            )
            if not claimed.rowcount:
                session.rollback()
                continue
            session.commit()
            job = session.get(OutboxJob, job_id)
            if job is None:  # pragma: no cover - deleted between claim/read
                return None
            return ClaimedJob(
                id=job.id,
                kind=JobKind(job.kind),
                payload=dict(job.payload or {}),
                lease_token=token,
            )
    return None


def _mark_succeeded(bind: Engine, claim: ClaimedJob) -> bool:
    now = utcnow()
    with Session(bind) as session:
        result = session.execute(
            update(OutboxJob)
            .where(
                OutboxJob.id == claim.id,
                OutboxJob.status == JobStatus.RUNNING,
                OutboxJob.lease_token == claim.lease_token,
            )
            .values(
                status=JobStatus.SUCCEEDED,
                completed_at=now,
                lease_token="",  # nosec B106 -- cleared worker lease, not a password
                leased_by="",
                lease_expires_at=None,
                last_error="",
            )
        )
        session.commit()
        return bool(result.rowcount)


def _error_text(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}".strip()
    return text[:_MAX_ERROR_LENGTH] or type(exc).__name__


def _mark_failed(bind: Engine, claim: ClaimedJob, exc: Exception) -> bool | None:
    """Record one failure. ``True`` means terminal; ``False`` means retried."""

    now = utcnow()
    with Session(bind) as session:
        job = session.get(OutboxJob, claim.id)
        if (
            job is None
            or JobStatus(job.status) is not JobStatus.RUNNING
            or job.lease_token != claim.lease_token
        ):
            return None  # a replacement worker owns the lease now

        terminal = job.attempt_count >= job.max_attempts
        job.status = JobStatus.FAILED if terminal else JobStatus.PENDING
        job.retry_count += 1
        job.last_error = _error_text(exc)
        job.lease_token = ""  # nosec B105 -- cleared worker lease, not a password
        job.leased_by = ""
        job.lease_expires_at = None
        if terminal:
            job.completed_at = now
        else:
            job.available_at = now + _retry_delay(job.attempt_count)
        session.add(job)
        session.commit()
        return terminal


def _required_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool):
        raise ValueError(f"Outbox job payload {key!r} must be an integer")
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Outbox job payload {key!r} must be an integer") from exc


def _run_email_job(payload: Mapping[str, object], bind: Engine) -> None:
    report_id = _required_int(payload, "report_id")
    stakeholder_id = _required_int(payload, "stakeholder_id")
    with Session(bind) as session:
        report = session.get(Report, report_id)
        stakeholder = session.get(User, stakeholder_id)
        # Deleting either row before delayed delivery is not a delivery failure:
        # there is no longer an addressable product/recipient to notify.
        if report is None or stakeholder is None:
            return
        if ReportStatus(report.status) is not ReportStatus.PUBLISHED:
            return
        title = report.title
        recipient = (stakeholder.email, stakeholder.display_name)

    from . import dissemination

    dissemination.deliver_email_notification(*recipient, title, report_id)


def _webhook_snapshot(payload: Mapping[str, object]) -> tuple[WebhookSettings, ProxySettings]:
    webhook_raw = payload.get("webhook")
    proxy_raw = payload.get("proxy")
    if not isinstance(webhook_raw, Mapping) or not isinstance(proxy_raw, Mapping):
        raise ValueError("Webhook job is missing its non-secret settings snapshot")
    try:
        proxy_mode = ProxyMode(str(proxy_raw.get("mode", ProxyMode.SYSTEM)).upper())
    except ValueError as exc:
        raise ValueError("Webhook job has an invalid proxy mode") from exc
    webhook = WebhookSettings(
        id=1,
        enabled=bool(webhook_raw.get("enabled", False)),
        url=str(webhook_raw.get("url", "")),
        timeout=float(webhook_raw.get("timeout", 5.0)),
        format=str(webhook_raw.get("format", "generic")),
    )
    proxy = ProxySettings(
        id=1,
        mode=proxy_mode,
        proxy_url=str(proxy_raw.get("proxy_url", "")),
        no_proxy=str(proxy_raw.get("no_proxy", "")),
    )
    return webhook, proxy


def _run_webhook_job(payload: Mapping[str, object], bind: Engine) -> None:
    report_id = _required_int(payload, "report_id")
    recipient_count = _required_int(payload, "recipient_count")
    with Session(bind) as session:
        report = session.get(Report, report_id)
        if report is None or ReportStatus(report.status) is not ReportStatus.PUBLISHED:
            return
        title = report.title
    webhook, proxy = _webhook_snapshot(payload)

    from . import dissemination

    dissemination.deliver_webhook_notification(
        title,
        report_id,
        recipient_count,
        webhook,
        proxy,
    )


def _run_rss_poll_job(bind: Engine) -> None:
    from . import feeds

    with Session(bind) as session:
        feeds.fetch_all_enabled_for_job(session)


def _execute(claim: ClaimedJob, bind: Engine) -> None:
    payload: Mapping[str, object] = claim.payload
    if claim.kind is JobKind.DISSEMINATION_EMAIL:
        _run_email_job(payload, bind)
    elif claim.kind is JobKind.DISSEMINATION_WEBHOOK:
        _run_webhook_job(payload, bind)
    elif claim.kind is JobKind.RSS_POLL:
        _run_rss_poll_job(bind)
    else:  # pragma: no cover - enum protects this; defensive for a hand-edited DB
        raise ValueError(f"Unsupported outbox job kind: {claim.kind}")


def process_due_jobs(
    *,
    limit: int = 25,
    worker_id: str | None = None,
    bind: Engine | None = None,
) -> WorkerResult:
    """Lease and execute a bounded number of due jobs.

    The external operation is intentionally outside the claim/complete database
    transactions.  Delivery is therefore at-least-once at the boundary (as is
    normal for an outbox); downstream mail/webhook consumers should tolerate a
    retry after a worker crash between egress and success recording.
    """

    if bind is None:
        from ..db import engine as bind

    result = WorkerResult()
    worker = worker_id or _default_worker_id()
    for _ in range(max(1, limit)):
        claim = _claim_next(bind, worker)
        if claim is None:
            break
        result.processed += 1
        try:
            _execute(claim, bind)
        except Exception as exc:  # noqa: BLE001 - persisted retry state is the point
            logger.warning(
                "Outbox job %s (%s) failed", claim.id, claim.kind, exc_info=True
            )
            terminal = _mark_failed(bind, claim, exc)
            if terminal is True:
                result.failed += 1
            elif terminal is False:
                result.retried += 1
            else:
                logger.warning("Outbox job %s lost its lease before failure update", claim.id)
        else:
            if _mark_succeeded(bind, claim):
                result.succeeded += 1
            else:
                logger.warning("Outbox job %s lost its lease before success update", claim.id)
    return result
