"""Test fixtures: isolated in-memory SQLite, a TestClient with the session
dependency overridden, and a dev-login helper."""

import os

# Must be set before importing the app/config (settings are cached at import).
os.environ["ICEBERG_SECRET_KEY"] = "test-secret-0123456789abcdef0123456789"
# Default to an in-memory SQLite DB, but honour an externally-provided Postgres
# URL (the CI postgres-smoke job sets ICEBERG_DATABASE_URL=postgresql+psycopg://…)
# so the same suite can exercise the Postgres datastore + FTS path.
if not os.environ.get("ICEBERG_DATABASE_URL", "").startswith("postgresql"):
    os.environ["ICEBERG_DATABASE_URL"] = "sqlite://"
os.environ["ICEBERG_DEV_AUTH"] = "true"
os.environ["ICEBERG_ENVIRONMENT"] = "dev"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from iceberg import db, models  # noqa: F401  -- register tables on metadata
from iceberg.db import get_session
from iceberg.main import create_app


def _pg_reset_and_upgrade(engine) -> None:
    """Rebuild a clean schema on the Postgres test DB, then run Alembic to head so
    the postgres_fts generated ``search_vector`` column + GIN index exist. The
    schema is shared, so the postgres-smoke job must run serially (``-n0``)."""
    from sqlalchemy import text

    from alembic import command

    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    cfg = db.alembic_config()
    with engine.connect() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")


@pytest.fixture(name="engine")
def engine_fixture():
    url = os.environ["ICEBERG_DATABASE_URL"]
    if url.startswith("postgresql"):
        # Postgres smoke path (CI): a real engine against the configured DB, the
        # schema rebuilt + migrated per test (Alembic, so the dialect-guarded
        # postgres_fts migration runs). Slower than the in-memory SQLite default,
        # so this path is used only for a focused subset.
        engine = create_engine(url)
        _pg_reset_and_upgrade(engine)
        try:
            yield engine
        finally:
            engine.dispose()
        return
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    try:
        yield engine
    finally:
        SQLModel.metadata.drop_all(engine)


@pytest.fixture(name="client")
def client_fixture(engine, monkeypatch):
    def _get_session():
        with Session(engine) as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = _get_session
    with TestClient(app) as client:
        # Startup (init_db / migrations) has now run against the module engine.
        # Repoint db.engine at the test's StaticPool engine so background tasks
        # (async source grading) — which open their own session via db.engine —
        # share one in-memory database with the request sessions.
        monkeypatch.setattr(db, "engine", engine)
        # Browsers send Origin on same-origin state-changing requests; mirror
        # that so the same-origin CSRF middleware admits the test's POSTs.
        client.headers["origin"] = "http://testserver"
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def login(client):
    """Authenticate the shared client as a given role via the dev-login bypass."""

    def _login(role: str = "ANALYST", email: str | None = None, name: str = "Tester"):
        email = email or f"{role.lower()}@example.com"
        resp = client.post(
            "/auth/dev-login", data={"role": role, "email": email, "name": name}
        )
        assert resp.status_code == 200, resp.text
        return email

    return _login
