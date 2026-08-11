"""Bounded migration, verification, and orphan reconciliation for storage."""

from __future__ import annotations

import hashlib
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from itertools import islice
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import or_, text, update
from sqlmodel import Session, select

from ..config import get_settings
from ..models import Attachment, Figure, RenderedProduct, StorageDeletion, utcnow
from . import storage, storage_deletions, storage_metrics

MODELS = {
    "attachment": Attachment,
    "figure": Figure,
    "render": RenderedProduct,
}


@dataclass
class MigrationResult:
    examined: int = 0
    migrated: int = 0
    verified: int = 0
    skipped: int = 0
    conflicts: int = 0
    missing: int = 0


@dataclass
class ReconcileResult:
    listed: int = 0
    referenced: int = 0
    young: int = 0
    orphaned: int = 0
    enqueued: int = 0
    missing: int = 0
    invalid: int = 0
    cycle_complete: bool = False
    requires_clean_cycle: bool = False


def _rows(session: Session, kind: str, limit: int, destination: str) -> list:
    model = MODELS[kind]
    # Select only unfinished rows. Re-running with the same bound naturally
    # advances past previously committed batches instead of repeatedly skipping
    # the first IDs forever.
    return list(
        session.exec(
            select(model)
            .where(
                or_(
                    model.storage_backend != destination,
                    model.content_sha256 == "",
                    model.storage_finalized_at.is_(None),
                )
            )
            .order_by(model.id)
            .limit(max(1, limit))
        ).all()
    )


@contextmanager
def _migration_lock(session: Session):
    """Prevent concurrent PostgreSQL migrators without adding schema state."""
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        yield
        return
    # A dedicated connection holds this session-level advisory lock across the
    # per-row commits performed by the migration session.
    with bind.connect() as connection:
        lock_id = 0x4943454245524753  # stable signed-64-bit "ICEBERGS" key
        acquired = bool(
            connection.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"), {"lock_id": lock_id}
            ).scalar()
        )
        if not acquired:
            raise storage.StorageError("Another storage migration is already running")
        try:
            yield
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": lock_id}
            )
            connection.commit()


def _migration_key(
    *, kind: str, row_id: int | None, backend: str, current_key: str, digest: str, suffix: str
) -> str:
    """Return an opaque deterministic key so interrupted copies resume in place."""
    identity = f"{kind}\0{row_id}\0{backend}\0{current_key}\0{digest}".encode()
    opaque = hashlib.sha256(identity).hexdigest()[:32]
    return f"{opaque}-{digest[:16]}{suffix.lower()}"


def _object_suffix(item, kind: str, current_key: str) -> str:
    """Preserve a safe compatibility extension across remote materialization."""
    candidates = [current_key, getattr(item, "stored_filename", "")]
    if kind == "render":
        candidates.append(getattr(item, "pdf_path", ""))
        candidates.append("render.pdf")
    for candidate in candidates:
        suffix = Path(candidate or "").suffix.lower()
        if suffix and len(suffix) <= 16 and suffix[1:].isalnum():
            return suffix
    return ""


def _migrate_unlocked(
    session: Session,
    *,
    destination: str,
    kinds: tuple[str, ...] = ("attachment", "figure", "render"),
    limit: int = 100,
    execute: bool = False,
) -> MigrationResult:
    """Copy and conditionally repoint rows; source objects are never removed."""
    result = MigrationResult()
    destination_store = storage.get_store(destination, session=session)
    for kind in kinds:
        storage.validate_kind(kind)
        for item in _rows(session, kind, limit, destination):
            result.examined += 1
            current_backend = storage.object_backend(item)
            current_key = storage.object_key(item, kind)
            if (
                current_backend == destination
                and current_key
                and item.content_sha256
                and item.storage_finalized_at is not None
            ):
                result.skipped += 1
                continue
            try:
                with storage.materialized_object(item, kind, session=session) as path:
                    if not path.is_file():
                        raise storage.ObjectMissing("Persistent object is missing")
                    actual_size = path.stat().st_size
                    if item.file_size and item.file_size != actual_size:
                        result.conflicts += 1
                        continue
                    digest = storage.file_sha256(path)
                    new_key = (
                        current_key
                        if current_backend == destination and current_key
                        else _migration_key(
                            kind=kind,
                            row_id=item.id,
                            backend=current_backend,
                            current_key=current_key,
                            digest=digest,
                            suffix=_object_suffix(item, kind, current_key),
                        )
                    )
                    if not execute:
                        # Dry-run also checks whether a previous interrupted copy
                        # occupies the deterministic destination key.
                        if current_backend != destination or current_key != new_key:
                            try:
                                existing = destination_store.head(kind, new_key)
                            except storage.ObjectMissing:
                                pass
                            else:
                                if existing.size != actual_size:
                                    result.conflicts += 1
                                    continue
                                destination_store.read_bytes(
                                    kind,
                                    new_key,
                                    expected_sha256=digest,
                                    max_bytes=actual_size,
                                )
                        result.verified += 1
                        continue
                    destination_store.put_file(
                        kind,
                        new_key,
                        path,
                        sha256=digest,
                        content_type=(
                            item.content_type
                            if kind in {"attachment", "figure"}
                            else "application/pdf"
                        ),
                    )
            except storage.ObjectMissing:
                result.missing += 1
                continue
            except storage.StorageError:
                result.conflicts += 1
                continue

            values = {
                "storage_backend": destination,
                "storage_key": new_key,
                "content_sha256": digest,
                "storage_finalized_at": utcnow(),
            }
            if kind == "render":
                values["file_size"] = actual_size
                values["pdf_path"] = (
                    str(storage.LocalObjectStore().path("render", new_key))
                    if destination == "local"
                    else ""
                )
            elif destination == "local":
                # Keep the legacy path identifier aligned so a later safe
                # metadata downgrade still leaves old application code able to
                # locate the copied local object.
                values["stored_filename"] = new_key
            statement = update(type(item)).where(
                type(item).id == item.id,
                type(item).storage_backend == current_backend,
                type(item).storage_key == getattr(item, "storage_key", ""),
                type(item).content_sha256 == getattr(item, "content_sha256", ""),
                type(item).file_size == getattr(item, "file_size", 0),
            )
            if kind in {"attachment", "figure"}:
                statement = statement.where(
                    type(item).stored_filename == item.stored_filename
                )
            finalized_at = getattr(item, "storage_finalized_at", None)
            statement = statement.where(
                type(item).storage_finalized_at.is_(None)
                if finalized_at is None
                else type(item).storage_finalized_at == finalized_at
            )
            updated = session.execute(statement.values(**values))
            if not updated.rowcount:
                session.rollback()
                result.conflicts += 1
                continue
            session.commit()
            result.migrated += 1
    return result


def migrate(
    session: Session,
    *,
    destination: str,
    kinds: tuple[str, ...] = ("attachment", "figure", "render"),
    limit: int = 100,
    execute: bool = False,
) -> MigrationResult:
    """Run one resumable, mutually-exclusive migration batch."""
    with _migration_lock(session):
        result = _migrate_unlocked(
            session,
            destination=destination,
            kinds=kinds,
            limit=limit,
            execute=execute,
        )
    storage_metrics.persist_maintenance(
        session,
        "migration",
        destination,
        result.__dict__,
        success=result.conflicts == 0 and result.missing == 0,
    )
    return result


def _reference_sets(session: Session, backend: str) -> dict[str, set[str]]:
    refs: dict[str, set[str]] = {kind: set() for kind in MODELS}
    for kind, model in MODELS.items():
        for item in session.exec(select(model).where(model.storage_backend == backend)).all():
            try:
                refs[kind].add(storage.deletion_key(item, kind))
            except storage.StorageError:
                continue
    for row in session.exec(
        select(StorageDeletion).where(
            StorageDeletion.backend == backend,
            StorageDeletion.status.in_(
                [
                    storage_deletions.PENDING,
                    storage_deletions.RUNNING,
                    storage_deletions.FAILED,
                ]
            ),
        )
    ).all():
        if row.kind in refs:
            refs[row.kind].add(row.object_key)
    return refs


def reconcile(
    session: Session,
    *,
    backend: str,
    kinds: tuple[str, ...] = ("attachment", "figure", "render"),
    limit: int = 500,
    execute: bool = False,
) -> ReconcileResult:
    """List only managed prefixes; dry-run by default, enqueue on apply."""
    kinds = tuple(dict.fromkeys(kinds))
    result = ReconcileResult()
    store = storage.get_store(backend, session=session)
    refs = _reference_sets(session, backend)
    cutoff = utcnow() - timedelta(seconds=get_settings().storage_orphan_grace_seconds)
    maintenance_scope = storage_metrics.reconcile_scope(backend, kinds)
    cursor = storage_metrics.load_cursor(session, "reconcile", maintenance_scope)
    object_cursors = dict(cursor.get("objects") or {})
    row_cursors = dict(cursor.get("rows") or {})
    object_done = dict(cursor.get("object_done") or {})
    row_done = dict(cursor.get("row_done") or {})
    prior_cycle = dict(cursor.get("cycle_result") or {})
    prior_requires_clean = bool(cursor.get("requires_clean_cycle"))
    batch_limit = max(1, limit)
    for kind in kinds:
        storage.validate_kind(kind)
        if object_done.get(kind):
            continue
        after_key = str(object_cursors.get(kind) or "")
        page = list(islice(store.list(kind, after_key=after_key), batch_limit + 1))
        if not page and after_key:
            # The previous boundary may have been the final key or may have
            # disappeared. Wrap in the same invocation instead of wasting a run.
            after_key = ""
            page = list(islice(store.list(kind), batch_limit + 1))
        batch = page[:batch_limit]
        if len(page) > batch_limit:
            object_cursors[kind] = batch[-1].key
            object_done[kind] = False
        else:
            object_cursors[kind] = ""
            object_done[kind] = True
        for info in batch:
            result.listed += 1
            if info.key in refs[kind]:
                result.referenced += 1
                continue
            if info.modified_at > cutoff:
                result.young += 1
                continue
            result.orphaned += 1
            if not execute:
                continue
            # Re-query immediately before enqueueing to close the list/commit race.
            if info.key in _reference_sets(session, backend)[kind]:
                result.referenced += 1
                result.orphaned -= 1
                continue
            synthetic = SimpleNamespace(
                storage_backend=backend,
                storage_key=info.key,
                content_sha256=info.sha256,
            )
            storage_deletions.enqueue(session, synthetic, kind, grace_seconds=0)
            session.commit()
            result.enqueued += 1

    # Exact-key verification finds missing or tampered references independently
    # of eventually consistent LIST behavior.
    for kind, model in MODELS.items():
        if kind not in kinds:
            continue
        if row_done.get(kind):
            continue
        try:
            after_id = max(0, int(row_cursors.get(kind) or 0))
        except (TypeError, ValueError):
            after_id = 0
        statement = (
            select(model)
            .where(model.storage_backend == backend, model.id > after_id)
            .order_by(model.id)
            .limit(batch_limit + 1)
        )
        page = list(session.exec(statement).all())
        if not page and after_id:
            after_id = 0
            page = list(
                session.exec(
                    select(model)
                    .where(model.storage_backend == backend)
                    .order_by(model.id)
                    .limit(batch_limit + 1)
                ).all()
            )
        batch = page[:batch_limit]
        if len(page) > batch_limit:
            row_cursors[kind] = int(batch[-1].id or 0)
            row_done[kind] = False
        else:
            row_cursors[kind] = 0
            row_done[kind] = True
        for item in batch:
            try:
                store.head(kind, storage.deletion_key(item, kind))
            except storage.ObjectMissing:
                result.missing += 1
                continue
            if not storage.verify_object(item, kind, session=session):
                result.invalid += 1
    count_fields = (
        "listed",
        "referenced",
        "young",
        "orphaned",
        "enqueued",
        "missing",
        "invalid",
    )
    cycle_values = {
        name: max(0, int(prior_cycle.get(name, 0))) + int(getattr(result, name))
        for name in count_fields
    }
    for name, value in cycle_values.items():
        setattr(result, name, value)
    result.cycle_complete = all(
        object_done.get(kind) and row_done.get(kind) for kind in kinds
    )
    found_failure = result.missing > 0 or result.invalid > 0
    if result.cycle_complete:
        # A prior failure is cleared only by a complete clean pass. A failed
        # complete pass arms the next cycle so retries cannot turn green after
        # inspecting only an unrelated clean page.
        result.requires_clean_cycle = found_failure
        next_cycle: dict[str, int] = {}
        for kind in kinds:
            object_cursors[kind] = ""
            row_cursors[kind] = 0
            object_done[kind] = False
            row_done[kind] = False
    else:
        result.requires_clean_cycle = prior_requires_clean or found_failure
        next_cycle = cycle_values
    storage_metrics.persist_maintenance(
        session,
        "reconcile",
        maintenance_scope,
        cycle_values,
        success=not result.requires_clean_cycle,
        cursor={
            "objects": object_cursors,
            "rows": row_cursors,
            "object_done": object_done,
            "row_done": row_done,
            "cycle_result": next_cycle,
            "requires_clean_cycle": result.requires_clean_cycle,
        },
    )
    return result


def active_check(session: Session) -> None:
    """PUT/HEAD/GET/hash/DELETE canary for deploy and restore acceptance."""
    store = storage.get_store(session=session)
    data = b"iceberg-storage-canary-v1"
    digest = hashlib.sha256(data).hexdigest()
    key = storage.new_key(".bin", sha256=digest)
    with tempfile.NamedTemporaryFile(prefix="iceberg-storage-check-", delete=False) as handle:
        handle.write(data)
        path = Path(handle.name)
    uploaded = False
    try:
        store.put_file(
            "attachment",
            key,
            path,
            sha256=digest,
            content_type="application/octet-stream",
        )
        uploaded = True
        store.head("attachment", key)
        returned = store.read_bytes(
            "attachment", key, expected_sha256=digest, max_bytes=len(data)
        )
        if returned != data:
            raise storage.StorageError("Storage canary verification failed")
        store.delete("attachment", key)
        uploaded = False
        storage_metrics.persist_maintenance(
            session,
            "active_check",
            get_settings().storage_backend,
            {"succeeded": 1},
            success=True,
        )
    except Exception:
        storage_metrics.persist_maintenance(
            session,
            "active_check",
            get_settings().storage_backend,
            {"failed": 1},
            success=False,
        )
        raise
    finally:
        if uploaded:
            try:
                store.delete("attachment", key)
            except storage.StorageError:
                pass
        path.unlink(missing_ok=True)
