"""Database engine, session and schema initialisation (SQLite via SQLModel)."""

from collections.abc import Iterator

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from .config import get_settings
from .services import search as search_service

settings = get_settings()

# Register the FTS table/trigger creation against Report-table creation at import
# time, so it fires for every create_all (app boot and the in-memory test engine).
search_service.register_fts_events()

_connect_args = (
    {"check_same_thread": False}
    if settings.database_url.startswith("sqlite")
    else {}
)
engine = create_engine(settings.database_url, echo=False, connect_args=_connect_args)


@event.listens_for(Engine, "connect")
def _enable_sqlite_fk(dbapi_connection, _connection_record):
    """Enforce foreign keys (and thus ON DELETE CASCADE) on SQLite connections."""
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
    except Exception:
        # Non-SQLite backends ignore this.
        pass


def init_db() -> None:
    # Importing models registers them on SQLModel.metadata before create_all.
    from . import models  # noqa: F401
    from .services.tags import seed_default_taxonomy

    SQLModel.metadata.create_all(engine)

    # Seed the controlled taxonomy and backfill the FTS index for any rows that
    # predate it. Both are idempotent.
    with Session(engine) as session:
        seed_default_taxonomy(session)
        search_service.reindex(session)


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
