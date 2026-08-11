"""Durable, lease-safe physical deletion of persistent objects."""

from __future__ import annotations

import os
import socket
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import and_, func, or_, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from ..config import get_settings
from ..models import Attachment, Figure, RenderedProduct, StorageDeletion, utcnow
from . import storage, storage_metrics

PENDING = "PENDING"
RUNNING = "RUNNING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"


def enqueue(
    session: Session,
    item,
    kind: str,
    *,
    grace_seconds: int | None = None,
) -> StorageDeletion:
    """Create a tombstone inside the caller's domain-deletion transaction."""
    storage.validate_kind(kind)
    backend = storage.object_backend(item)
    key = storage.deletion_key(item, kind)
    now = utcnow()
    delay = (
        get_settings().storage_deletion_grace_seconds
        if grace_seconds is None
        else max(0, grace_seconds)
    )
    existing = session.exec(
        select(StorageDeletion).where(
            StorageDeletion.kind == kind,
            StorageDeletion.backend == backend,
            StorageDeletion.object_key == key,
        )
    ).first()
    if existing is not None:
        if existing.status in {SUCCEEDED, CANCELLED}:
            # A restored/recreated object may legitimately reuse a terminal
            # tombstone's immutable key. Re-arm the durable row so it can be
            # removed again; FAILED remains fail-closed for operator review.
            existing.content_sha256 = getattr(item, "content_sha256", "") or ""
            existing.status = PENDING
            existing.not_before = now + timedelta(seconds=delay)
            existing.available_at = now + timedelta(seconds=delay)
            existing.attempt_count = 0
            existing.max_attempts = max(1, get_settings().jobs_max_attempts)
            existing.leased_at = None
            existing.lease_expires_at = None
            existing.lease_token = ""  # nosec B105 - cleared worker lease
            existing.leased_by = ""
            existing.last_error = ""
            existing.completed_at = None
            session.add(existing)
            session.flush()
        return existing
    row = StorageDeletion(
        kind=kind,
        backend=backend,
        object_key=key,
        content_sha256=getattr(item, "content_sha256", "") or "",
        not_before=now + timedelta(seconds=delay),
        available_at=now + timedelta(seconds=delay),
        max_attempts=max(1, get_settings().jobs_max_attempts),
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        existing = session.exec(
            select(StorageDeletion).where(
                StorageDeletion.kind == kind,
                StorageDeletion.backend == backend,
                StorageDeletion.object_key == key,
            )
        ).first()
        if existing is not None:
            return existing
        raise
    return row


def schedule_worker(background_tasks, *, limit: int = 25) -> None:
    if background_tasks is not None:
        background_tasks.add_task(process_due_deletions, limit=limit)


def _claimable(now: datetime):
    return or_(
        and_(
            StorageDeletion.status == PENDING,
            StorageDeletion.not_before <= now,
            StorageDeletion.available_at <= now,
        ),
        and_(
            StorageDeletion.status == RUNNING,
            StorageDeletion.lease_expires_at.is_not(None),
            StorageDeletion.lease_expires_at <= now,
        ),
    )


def _claim_next(bind: Engine, worker_id: str) -> tuple[int, str] | None:
    now = utcnow()
    lease = timedelta(seconds=max(1, get_settings().jobs_lease_seconds))
    with Session(bind) as session:
        ids = list(
            session.exec(
                select(StorageDeletion.id)
                .where(_claimable(now))
                .order_by(StorageDeletion.available_at, StorageDeletion.id)
                .limit(16)
            ).all()
        )
        for row_id in ids:
            if row_id is None:
                continue
            token = uuid4().hex
            result = session.execute(
                update(StorageDeletion)
                .where(StorageDeletion.id == row_id, _claimable(now))
                .values(
                    status=RUNNING,
                    lease_token=token,
                    leased_by=worker_id[:255],
                    leased_at=now,
                    lease_expires_at=now + lease,
                    attempt_count=StorageDeletion.attempt_count + 1,
                )
            )
            if not result.rowcount:
                session.rollback()
                continue
            session.commit()
            return row_id, token
    return None


def _referenced(session: Session, row: StorageDeletion) -> bool:
    model = {
        "attachment": Attachment,
        "figure": Figure,
        "render": RenderedProduct,
    }[row.kind]
    current = session.exec(
        select(model).where(
            model.storage_backend == row.backend,
            model.storage_key == row.object_key,
        )
    ).first()
    if current is not None:
        return True
    if row.kind == "render" and row.backend == "local":
        # Legacy render rows have a blank storage_key and retain pdf_path.
        legacy_path = str(storage.LocalObjectStore().path("render", row.object_key))
        return (
            session.exec(
                select(RenderedProduct).where(
                    RenderedProduct.storage_key == "",
                    RenderedProduct.pdf_path == legacy_path,
                )
            ).first()
            is not None
        )
    return False


def _execute(bind: Engine, row_id: int) -> str:
    with Session(bind) as session:
        row = session.get(StorageDeletion, row_id)
        if row is None:
            return SUCCEEDED
        if _referenced(session, row):
            return CANCELLED
        backend = storage.get_store(row.backend, session=session)
        try:
            info = backend.head(row.kind, row.object_key)
        except storage.ObjectMissing:
            return SUCCEEDED
        if row.content_sha256:
            # Delete only the exact bytes the tombstone describes. If the key
            # was tampered with, retain it and surface a terminal investigation.
            backend.read_bytes(
                row.kind,
                row.object_key,
                expected_sha256=row.content_sha256,
                max_bytes=info.size,
            )
        backend.delete(row.kind, row.object_key)
        return SUCCEEDED


def _mark_complete(bind: Engine, row_id: int, token: str, status: str) -> None:
    if status not in {SUCCEEDED, CANCELLED}:
        raise ValueError("Invalid storage deletion completion status")
    with Session(bind) as session:
        session.execute(
            update(StorageDeletion)
            .where(
                StorageDeletion.id == row_id,
                StorageDeletion.status == RUNNING,
                StorageDeletion.lease_token == token,
            )
            .values(
                status=status,
                completed_at=utcnow(),
                lease_token="",  # nosec B106 - cleared worker lease
                leased_by="",
                lease_expires_at=None,
                last_error="",
            )
        )
        session.commit()


def _mark_failed(bind: Engine, row_id: int, token: str, exc: Exception) -> bool:
    with Session(bind) as session:
        row = session.get(StorageDeletion, row_id)
        if row is None or row.status != RUNNING or row.lease_token != token:
            return False
        terminal = row.attempt_count >= row.max_attempts
        row.status = FAILED if terminal else PENDING
        # Unexpected provider exceptions can carry credential-bearing URLs or
        # signed query strings. Only our deliberately generic StorageError text
        # is safe to persist for operators.
        detail = str(exc) if isinstance(exc, storage.StorageError) else "unexpected failure"
        row.last_error = f"{type(exc).__name__}: {detail}"[:1000]
        row.lease_token = ""  # nosec B105 - cleared worker lease
        row.leased_by = ""
        row.lease_expires_at = None
        if terminal:
            row.completed_at = utcnow()
        else:
            delay = min(
                get_settings().jobs_retry_base_seconds * (2 ** max(0, row.attempt_count - 1)),
                3600,
            )
            row.available_at = utcnow() + timedelta(seconds=max(1, delay))
        session.add(row)
        session.commit()
        return terminal


def process_due_deletions(
    *,
    limit: int = 25,
    worker_id: str | None = None,
    bind: Engine | None = None,
) -> dict[str, int]:
    """Process a bounded pass; concurrent replicas claim disjoint tombstones."""
    if bind is None:
        from ..db import engine

        bind = engine
    worker = worker_id or f"{socket.gethostname() or 'worker'}:{os.getpid()}"
    result = {"processed": 0, "succeeded": 0, "cancelled": 0, "retried": 0, "failed": 0}
    for _ in range(max(1, limit)):
        claim = _claim_next(bind, worker)
        if claim is None:
            break
        row_id, token = claim
        result["processed"] += 1
        try:
            outcome = _execute(bind, row_id)
        except Exception as exc:  # bounded worker records a redacted/generic error
            terminal = _mark_failed(bind, row_id, token, exc)
            result["failed" if terminal else "retried"] += 1
        else:
            _mark_complete(bind, row_id, token, outcome)
            result[outcome.lower()] += 1
    with Session(bind) as session:
        storage_metrics.persist_maintenance(
            session,
            "deletion",
            "default",
            result,
            success=result["failed"] == 0,
        )
    return result


def queue_metrics(session: Session) -> dict[str, int | float]:
    pending = int(
        session.exec(
            select(func.count(StorageDeletion.id)).where(
                StorageDeletion.status.in_([PENDING, RUNNING])
            )
        ).one()
    )
    oldest = session.exec(
        select(func.min(StorageDeletion.created_at)).where(
            StorageDeletion.status.in_([PENDING, RUNNING])
        )
    ).one()
    if oldest and oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=timezone.utc)
    age = max(0.0, (utcnow() - oldest).total_seconds()) if oldest else 0.0
    failed = int(
        session.exec(
            select(func.count(StorageDeletion.id)).where(
                StorageDeletion.status == FAILED
            )
        ).one()
    )
    oldest_failed = session.exec(
        select(func.min(StorageDeletion.created_at)).where(
            StorageDeletion.status == FAILED
        )
    ).one()
    if oldest_failed and oldest_failed.tzinfo is None:
        oldest_failed = oldest_failed.replace(tzinfo=timezone.utc)
    failed_age = (
        max(0.0, (utcnow() - oldest_failed).total_seconds())
        if oldest_failed
        else 0.0
    )
    return {
        "depth": pending,
        "oldest_age_seconds": age,
        "failed": failed,
        "failed_oldest_age_seconds": failed_age,
    }
