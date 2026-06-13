"""Test fixtures: isolated in-memory SQLite, a TestClient with the session
dependency overridden, and a dev-login helper."""

import os

# Must be set before importing the app/config (settings are cached at import).
os.environ["ICEBERG_SECRET_KEY"] = "test-secret-0123456789abcdef0123456789"
os.environ["ICEBERG_DATABASE_URL"] = "sqlite://"
os.environ["ICEBERG_DEV_AUTH"] = "true"
os.environ["ICEBERG_ENVIRONMENT"] = "dev"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from iceberg import models  # noqa: F401  -- register tables on metadata
from iceberg.db import get_session
from iceberg.main import create_app


@pytest.fixture(name="engine")
def engine_fixture():
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
def client_fixture(engine):
    def _get_session():
        with Session(engine) as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = _get_session
    with TestClient(app) as client:
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
