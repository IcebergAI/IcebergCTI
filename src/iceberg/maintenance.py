"""Operational maintenance commands for derived Iceberg data."""

from sqlmodel import Session

from .db import engine, init_db
from .services import related
from .services.reports import prune_rendered_products


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
