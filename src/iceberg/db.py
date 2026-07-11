"""Database engine, session and schema initialisation (SQLite via SQLModel)."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine

from .config import get_settings
from .services import search as search_service

settings = get_settings()

# Stable key for the boot-time PostgreSQL advisory lock that serialises init_db
# (migrations + idempotent taxonomy seed) across concurrent uvicorn workers /
# replicas. Advisory-lock keys are bigint; this value is otherwise arbitrary.
_BOOT_LOCK_KEY = 0x1CEB_0001

# Alembic migrations ship inside the package, so this resolves for editable and
# wheel installs alike (no dependency on alembic.ini's location at runtime).
_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_bootstrap_ready = False

# Register the FTS table/trigger creation against Report-table creation at import
# time, so it fires for every create_all (app boot and the in-memory test engine).
search_service.register_fts_events()

_is_sqlite = settings.database_url.startswith("sqlite")
_connect_args = {"check_same_thread": False} if _is_sqlite else {}
# Networked backends (PostgreSQL) pool connections that can be dropped by the
# server/proxy; pre-ping discards a dead connection instead of erroring. SQLite
# is a local file/in-memory handle, so pre-ping is unnecessary there.
_engine_kwargs: dict = {} if _is_sqlite else {"pool_pre_ping": True}
engine = create_engine(
    settings.database_url, echo=False, connect_args=_connect_args, **_engine_kwargs
)


@event.listens_for(Engine, "connect")
def _configure_sqlite(dbapi_connection, _connection_record):
    """Tune each SQLite connection (no-op on other backends — guarded on the
    driver connection type so it's correct for the app engine and the tests'
    own engines alike, including a PostgreSQL test engine):

    - ``foreign_keys=ON`` so ON DELETE CASCADE is enforced.
    - ``journal_mode=WAL`` so readers don't block the writer (and vice versa) —
      without it the default rollback journal serialises everything and
      concurrent writes (editor autosave overlapping publish/dissemination, or
      multiple analysts) raise ``database is locked``. WAL is a persistent
      property of a file DB, so this is a no-op after the first connection; on an
      in-memory DB it harmlessly stays ``memory``.
    - ``busy_timeout`` so a writer waits-and-retries for up to 5s instead of
      failing immediately when the DB is momentarily locked.
    """
    if "sqlite3" not in type(dbapi_connection).__module__:
        return  # PostgreSQL (psycopg) etc. — these PRAGMAs are SQLite-only.
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


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


def packaged_schema_head() -> str:
    """Return the single Alembic head shipped with this application build."""
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    if not head:  # pragma: no cover - a packaged build always has migrations
        raise RuntimeError("No packaged Alembic head revision")
    return head


def schema_is_current(bind=None) -> bool:
    """Side-effect-free database revision check used by boot and readiness."""
    from alembic.migration import MigrationContext

    try:
        bind = bind or engine
        with bind.connect() as connection:
            current = MigrationContext.configure(connection).get_current_revision()
        return current == packaged_schema_head()
    except Exception:  # noqa: BLE001 - unreachable/unmigrated means not current
        return False


def application_ready(bind=None) -> bool:
    return _bootstrap_ready and schema_is_current(bind)


@contextmanager
def _boot_serialised() -> Iterator[None]:
    """Serialise boot init across concurrent uvicorn workers / replicas.

    The app runs with multiple workers, and every worker calls ``init_db`` on
    startup. Without coordination two workers run the migrations and the
    check-then-insert taxonomy seed at the same time and race the ``(kind, slug)``
    unique constraint (an ``IntegrityError`` that crashes a worker on PostgreSQL).
    A PostgreSQL **session-level advisory lock** (held on a dedicated connection,
    independent of transaction commits, auto-released if the process dies) makes
    the second worker wait until the first finishes, after which the migrations
    are at head and the seed is a no-op. A no-op on SQLite — that's the local
    dev/test backend and its single-writer locking already serialises writers."""
    if engine.dialect.name != "postgresql":
        yield
        return
    conn = engine.connect()
    try:
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _BOOT_LOCK_KEY})
        conn.commit()  # end the txn; the session-level lock persists past commit
        yield
    finally:
        conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _BOOT_LOCK_KEY})
        conn.commit()
        conn.close()


def init_db() -> None:
    # Importing models registers them on SQLModel.metadata (needed by env.py and
    # the in-memory test engine's create_all).
    from . import models  # noqa: F401
    from .services.publication import backfill_snapshots
    from .services.tags import seed_default_taxonomy

    global _bootstrap_ready
    _bootstrap_ready = False
    with _boot_serialised():
        # Apply schema migrations. In production set ICEBERG_AUTO_MIGRATE=false and
        # run `alembic upgrade head` in the deploy step instead.
        if get_settings().auto_migrate:
            run_migrations()
        elif not schema_is_current():
            return

        # Seed the controlled taxonomy and backfill the FTS index for any rows
        # that predate it. Both are idempotent.
        with Session(engine) as session:
            seed_default_taxonomy(session)
            search_service.reindex(session)
            # Existing published reports predate the immutable snapshot model.
            # Freeze their current approved representation once at upgrade boot.
            backfill_snapshots(session)
        _bootstrap_ready = True


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
