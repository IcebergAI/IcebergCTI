"""Operational maintenance commands for derived Iceberg data."""

import argparse
import time

from sqlmodel import Session, select

from .config import get_settings
from .db import engine, init_db, run_migrations, schema_is_current
from .models import Attachment, Figure, JobStatus, RenderedProduct
from .services import attachments, figures, related, storage
from .services import jobs
from .services import storage_deletions, storage_maintenance
from .services.audit import prune_audit_events
from .services.feeds import prune_feed_items
from .services.reports import prune_rendered_products


def migrate_main() -> None:
    """Apply Alembic migrations to ``head`` (the deploy-step migration entrypoint).

    Uses the app's in-code Alembic config (URL from ``ICEBERG_DATABASE_URL``, no
    dependency on a packaged ``alembic.ini``), so it's the migrate command for the
    container Job. Schema only — taxonomy seeding + FTS reindex happen on app boot
    (``init_db``)."""
    run_migrations()
    print("Migrations applied to head")


def prune_renders_main() -> None:
    """Prune rendered PDFs using the configured retention policy."""
    init_db()
    with Session(engine) as session:
        count = prune_rendered_products(session)
    print(f"Pruned {count} rendered product(s)")


def prune_audit_main() -> None:
    """Prune the append-forever tables per their retention windows.

    Bounds the local ``AuditEvent`` forensic buffer (the SIEM is the long-term
    store) and the un-ingested ``FeedItem`` reader inventory. Windows are set by
    ``ICEBERG_AUDIT_RETENTION_DAYS`` / ``ICEBERG_FEED_ITEM_RETENTION_DAYS`` (0 =
    keep forever). Made for a cron / Kubernetes CronJob alongside the app image.
    """
    init_db()
    with Session(engine) as session:
        audit_count = prune_audit_events(session)
        feed_count = prune_feed_items(session)
    print(f"Pruned {audit_count} audit event(s) and {feed_count} feed item(s)")


def rebuild_related_main() -> None:
    """Maintain the related-product index for published reports.

    Default: rebuild everything that is missing or stale. ``--batch N`` does one
    bounded pass instead, which an operator can repeat against a large corpus —
    staleness lives on the row, so an interrupted run simply leaves fewer stale
    entries and the next pass continues from there. ``--status`` reports the
    index's health without changing anything.
    """

    parser = argparse.ArgumentParser(
        prog="iceberg-rebuild-related",
        description="Rebuild or inspect the related-product index.",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=0,
        help="Index at most N stale entries in one pass (0 = until nothing is pending).",
    )
    parser.add_argument(
        "--status", action="store_true", help="Report index health and exit."
    )
    args = parser.parse_args()

    init_db()
    with Session(engine) as session:
        if args.status:
            health = related.index_health(session)
            for name, value in health.items():
                print(f"{name}: {value}")
            return
        if args.batch > 0:
            result = related.reindex(session, batch=args.batch)
            print(
                f"Indexed {result['indexed']} report(s), removed {result['removed']}, "
                f"{result['pending']} still pending, {result['up_to_date']} up to date"
            )
            return
        count = related.rebuild(session)
    print(f"Indexed {count} published report(s)")


def missing_persistent_files(session: Session) -> list[str]:
    """Return non-sensitive reference labels for DB rows missing their bytes."""
    missing: list[str] = []
    for item in session.exec(select(Attachment)).all():
        if not attachments.attachment_is_valid(session, item):
            missing.append(f"attachment:{item.id}")
    for item in session.exec(select(Figure)).all():
        if not figures.figure_is_valid(session, item):
            missing.append(f"figure:{item.id}")
    for item in session.exec(select(RenderedProduct)).all():
        if not storage.verify_object(item, "render", session=session):
            missing.append(f"rendered_product:{item.id}")
    return missing


def verify_files_main() -> None:
    """Fail restore workflows when DB file references are not present on disk."""
    if not schema_is_current():
        raise SystemExit("Database schema is not at the packaged Alembic head")
    with Session(engine) as session:
        missing = missing_persistent_files(session)
    if missing:
        print("Missing persistent files: " + ", ".join(missing))
        raise SystemExit(1)
    print("Persistent file references verified")


def _worker_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="iceberg-worker",
        description="Process Iceberg's durable external-work outbox.",
    )
    parser.add_argument(
        "--forever",
        action="store_true",
        help="keep polling instead of processing one bounded pass",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="maximum jobs per worker pass (default: 25)",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=None,
        help="sleep between --forever passes (default: ICEBERG_JOBS_WORKER_POLL_SECONDS)",
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="list recent jobs and exit without executing external work",
    )
    parser.add_argument(
        "--status",
        choices=[status.value for status in JobStatus],
        help="filter --inspect by job status",
    )
    return parser


def _print_worker_result(result: jobs.WorkerResult) -> None:
    print(
        "Processed "
        f"{result.processed} job(s): {result.succeeded} succeeded, "
        f"{result.retried} queued for retry, {result.failed} failed"
    )


def _inspect_jobs(status: str | None) -> None:
    selected = JobStatus(status) if status else None
    with Session(engine) as session:
        rows = jobs.list_jobs(session, status=selected)
    if not rows:
        print("No outbox jobs")
        return
    for row in rows:
        lease = row.lease_expires_at.isoformat() if row.lease_expires_at else "-"
        error = row.last_error.replace("\n", " ")[:160] or "-"
        print(
            f"{row.id}\t{row.kind}\t{row.status}\t"
            f"attempts={row.attempt_count}/{row.max_attempts}\t"
            f"retries={row.retry_count}\tlease={lease}\terror={error}"
        )


def worker_main() -> None:
    """Console entrypoint for the durable email/webhook/RSS worker.

    ``iceberg-worker`` is intentionally useful both under a process manager
    (``--forever``) and as a cron/Kubernetes Job (the default one bounded pass).
    """

    args = _worker_parser().parse_args()
    init_db()
    if args.inspect:
        _inspect_jobs(args.status)
        return

    delay = (
        args.poll_seconds
        if args.poll_seconds is not None
        else jobs.get_settings().jobs_worker_poll_seconds
    )
    delay = max(0.1, float(delay))
    try:
        while True:
            _print_worker_result(jobs.process_due_jobs(limit=max(1, args.limit)))
            if not args.forever:
                return
            time.sleep(delay)
    except KeyboardInterrupt:
        print("Worker stopped")


def storage_worker_main() -> None:
    parser = argparse.ArgumentParser(
        prog="iceberg-storage-worker",
        description="Process durable persistent-object deletion tombstones.",
    )
    parser.add_argument("--forever", action="store_true")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--poll-seconds", type=float, default=None)
    args = parser.parse_args()
    init_db()
    delay = max(
        0.1,
        args.poll_seconds
        if args.poll_seconds is not None
        else get_settings().jobs_worker_poll_seconds,
    )
    try:
        while True:
            result = storage_deletions.process_due_deletions(limit=max(1, args.limit))
            print(
                "Processed {processed} storage deletion(s): {succeeded} succeeded, "
                "{cancelled} cancelled, {retried} retried, {failed} failed".format(
                    **result
                )
            )
            if not args.forever:
                if result["failed"]:
                    raise SystemExit(1)
                return
            time.sleep(delay)
    except KeyboardInterrupt:
        print("Storage worker stopped")


def storage_check_main() -> None:
    """Active permission/integrity canary for deployment and restore gates."""
    if not schema_is_current():
        raise SystemExit("Database schema is not at the packaged Alembic head")
    try:
        with Session(engine) as session:
            storage_maintenance.active_check(session)
    except storage.StorageError:
        print("Persistent storage active check failed")
        raise SystemExit(1)
    print("Persistent storage active check passed")


def _storage_kinds(values: list[str] | None) -> tuple[str, ...]:
    return tuple(values or ["attachment", "figure", "render"])


def migrate_storage_main() -> None:
    parser = argparse.ArgumentParser(
        prog="iceberg-migrate-storage",
        description="Resumably verify/copy persistent objects and repoint rows.",
    )
    parser.add_argument("--execute", action="store_true", help="apply changes (default: dry-run)")
    parser.add_argument("--destination", choices=["local", "s3"], default=None)
    parser.add_argument(
        "--kind", choices=["attachment", "figure", "render"], action="append"
    )
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--until-complete",
        action="store_true",
        help="execute successive bounded batches until no unfinished rows remain",
    )
    args = parser.parse_args()
    if args.until_complete and not args.execute:
        parser.error("--until-complete requires --execute")
    if not schema_is_current():
        raise SystemExit("Database schema is not at the packaged Alembic head")
    destination = args.destination or get_settings().storage_backend
    totals = storage_maintenance.MigrationResult()
    with Session(engine) as session:
        while True:
            result = storage_maintenance.migrate(
                session,
                destination=destination,
                kinds=_storage_kinds(args.kind),
                limit=max(1, args.limit),
                execute=args.execute,
            )
            for field in totals.__dataclass_fields__:
                setattr(totals, field, getattr(totals, field) + getattr(result, field))
            if result.conflicts or result.missing or not args.until_complete:
                break
            if result.examined == 0:
                break
    mode = "executed" if args.execute else "dry-run"
    print(
        f"Storage migration {mode}: examined={totals.examined} "
        f"migrated={totals.migrated} verified={totals.verified} "
        f"skipped={totals.skipped} conflicts={totals.conflicts} missing={totals.missing}"
    )
    if totals.conflicts or totals.missing:
        raise SystemExit(1)


def reconcile_storage_main() -> None:
    parser = argparse.ArgumentParser(
        prog="iceberg-reconcile-storage",
        description="Bounded orphan and integrity reconciliation (dry-run by default).",
    )
    parser.add_argument("--execute", action="store_true", help="enqueue old orphans")
    parser.add_argument("--backend", choices=["local", "s3"], default=None)
    parser.add_argument(
        "--kind", choices=["attachment", "figure", "render"], action="append"
    )
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()
    if not schema_is_current():
        raise SystemExit("Database schema is not at the packaged Alembic head")
    backend = args.backend or get_settings().storage_backend
    with Session(engine) as session:
        result = storage_maintenance.reconcile(
            session,
            backend=backend,
            kinds=_storage_kinds(args.kind),
            limit=max(1, args.limit),
            execute=args.execute,
        )
    mode = "execute" if args.execute else "dry-run"
    print(
        f"Storage reconciliation {mode}: listed={result.listed} "
        f"referenced={result.referenced} young={result.young} "
        f"orphaned={result.orphaned} enqueued={result.enqueued} "
        f"missing={result.missing} invalid={result.invalid}"
    )
    if result.missing or result.invalid or result.requires_clean_cycle:
        raise SystemExit(1)
