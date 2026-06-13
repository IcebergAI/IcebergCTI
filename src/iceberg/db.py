"""Database engine, session and schema initialisation (SQLite via SQLModel)."""

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine

from .config import get_settings
from .services import search as search_service

settings = get_settings()

# Alembic migrations ship inside the package, so this resolves for editable and
# wheel installs alike (no dependency on alembic.ini's location at runtime).
_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

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
    except Exception:  # nosec B110 — best-effort: non-SQLite backends ignore the pragma
        # Non-SQLite backends ignore this.
        pass


def alembic_config():
    """Alembic Config built in code (pointing at the packaged migrations) so it
    works without alembic.ini on PATH. env.py also reads the URL from settings."""
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    return cfg


def run_migrations() -> None:
    """Bring the application database up to ``head``. Idempotent. Used at boot
    (when ``ICEBERG_AUTO_MIGRATE`` is true) and by the seed CLI. Runs against the
    module ``engine`` via a shared connection so it works for an in-memory
    database too (each fresh connection to ``sqlite://`` is a separate DB)."""
    from alembic import command

    cfg = alembic_config()
    with engine.connect() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")


def init_db() -> None:
    # Importing models registers them on SQLModel.metadata (needed by env.py and
    # the in-memory test engine's create_all).
    from . import models  # noqa: F401
    from .services.tags import seed_default_taxonomy

    # Apply schema migrations. In production set ICEBERG_AUTO_MIGRATE=false and
    # run `alembic upgrade head` in the deploy step instead.
    if get_settings().auto_migrate:
        run_migrations()

    # Seed the controlled taxonomy and backfill the FTS index for any rows that
    # predate it. Both are idempotent.
    with Session(engine) as session:
        seed_default_taxonomy(session)
        search_service.reindex(session)


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
