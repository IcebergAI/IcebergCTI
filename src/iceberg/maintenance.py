"""Operational maintenance commands for derived Iceberg data."""

import argparse
import time

from sqlmodel import Session

from .db import engine, init_db, run_migrations
from .models import JobStatus
from .services import related
from .services import jobs
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


def rebuild_related_main() -> None:
    """Rebuild the local related-report vector index for published reports."""
    init_db()
    with Session(engine) as session:
        count = related.rebuild(session)
    print(f"Indexed {count} published report(s)")


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
