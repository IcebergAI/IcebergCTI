"""Operational maintenance commands for derived Iceberg data."""

from sqlmodel import Session

from .db import engine, init_db, run_migrations
from .services import related
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
