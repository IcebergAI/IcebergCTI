"""Configuration safety: production must refuse an unsafe JWT signing key and
SQLite (SQLite is the local dev/test datastore only)."""

import pytest
from pydantic import ValidationError

from iceberg.config import _INSECURE_DEFAULT_SECRET, Settings

_STRONG_SECRET = "x" * 40
_PG_URL = "postgresql+psycopg://iceberg:iceberg@postgres:5432/iceberg"


def test_prod_rejects_public_default_secret():
    with pytest.raises(ValidationError):
        Settings(
            environment="prod",
            secret_key=_INSECURE_DEFAULT_SECRET,
            database_url=_PG_URL,
        )


def test_prod_rejects_short_secret():
    with pytest.raises(ValidationError):
        Settings(environment="prod", secret_key="too-short", database_url=_PG_URL)


def test_prod_rejects_sqlite():
    # SQLite is the dev/test default only — production must run on PostgreSQL.
    with pytest.raises(ValidationError):
        Settings(
            environment="prod",
            secret_key=_STRONG_SECRET,
            database_url="sqlite:////data/iceberg.db",
        )


def test_prod_rejects_default_sqlite_url():
    # The built-in default is SQLite, so prod must not silently accept it.
    with pytest.raises(ValidationError):
        Settings(environment="prod", secret_key=_STRONG_SECRET)


def test_prod_accepts_strong_secret_and_postgres():
    settings = Settings(
        environment="prod", secret_key=_STRONG_SECRET, database_url=_PG_URL
    )
    assert settings.is_prod
    assert not settings.is_sqlite
    assert not settings.dev_login_enabled  # dev login is always off in prod


def test_dev_tolerates_default_secret_and_sqlite():
    settings = Settings(environment="dev", secret_key=_INSECURE_DEFAULT_SECRET)
    assert not settings.is_prod
    assert settings.is_sqlite
