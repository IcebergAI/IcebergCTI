"""Configuration safety: production must refuse an unsafe JWT signing key and
SQLite (SQLite is the local dev/test datastore only)."""

import io
import json
import logging

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


def test_log_format_auto_is_text_in_dev_json_in_prod():
    from iceberg.logging_config import effective_log_format

    assert effective_log_format(Settings(environment="dev")) == "text"
    assert (
        effective_log_format(
            Settings(
                environment="prod",
                secret_key=_STRONG_SECRET,
                database_url=_PG_URL,
            )
        )
        == "json"
    )


def test_invalid_log_format_is_rejected():
    with pytest.raises(ValidationError):
        Settings(log_format="xml")


def test_invalid_log_level_is_rejected():
    with pytest.raises(ValidationError):
        Settings(log_level="chatty")


def test_json_app_logs_include_correlation_id():
    from iceberg.logging_config import (
        configure_logging,
        reset_correlation_id,
        set_correlation_id,
    )

    stream = io.StringIO()
    configure_logging(Settings(log_format="json"), stream=stream)
    token = set_correlation_id("cid-test")
    try:
        logging.getLogger("iceberg.test").warning("hello %s", "world")
    finally:
        reset_correlation_id(token)

    payload = json.loads(stream.getvalue())
    assert payload["level"] == "WARNING"
    assert payload["logger"] == "iceberg.test"
    assert payload["message"] == "hello world"
    assert payload["correlation_id"] == "cid-test"
    assert payload["module"] == "test_config"
    assert isinstance(payload["line"], int)


def test_logs_outside_request_use_empty_correlation_id():
    from iceberg.logging_config import configure_logging

    stream = io.StringIO()
    configure_logging(Settings(log_format="json"), stream=stream)
    logging.getLogger("iceberg.test").info("outside")
    assert json.loads(stream.getvalue())["correlation_id"] == "-"


def test_audit_stdout_payload_is_not_double_encoded():
    from iceberg.logging_config import configure_logging

    stream = io.StringIO()
    configure_logging(Settings(log_format="json"), stream=stream)
    logging.getLogger("iceberg.audit").info('{"action":"AUTH_LOGIN","ok":true}')
    assert stream.getvalue().strip() == '{"action":"AUTH_LOGIN","ok":true}'


def test_audit_stdout_not_silenced_by_app_log_level():
    # Raising ICEBERG_LOG_LEVEL (a common prod choice to quiet app noise) must not
    # drop INFO-severity audit events from the stdout SIEM line — audit emission is
    # gated only by services/siem.emit's min-severity, not app-log verbosity.
    from iceberg.logging_config import configure_logging

    stream = io.StringIO()
    configure_logging(Settings(log_format="json", log_level="WARNING"), stream=stream)
    logging.getLogger("iceberg.audit").info('{"action":"AUTH_LOGIN","ok":true}')
    assert stream.getvalue().strip() == '{"action":"AUTH_LOGIN","ok":true}'


def test_warns_when_prod_has_no_login_path(caplog):
    # prod disables the dev bypass and OIDC is unset -> /auth/login is a dead end;
    # the lockout must surface in the logs (issue #103) rather than fail silently.
    from iceberg.main import _warn_if_no_login_path

    settings = Settings(
        environment="prod",
        secret_key=_STRONG_SECRET,
        database_url=_PG_URL,
        dev_auth=False,
        oidc_enabled=False,
    )
    with caplog.at_level("WARNING", logger="iceberg.auth"):
        _warn_if_no_login_path(settings)
    assert any("No usable login path" in r.message for r in caplog.records)


def test_no_login_warning_when_oidc_enabled(caplog):
    from iceberg.main import _warn_if_no_login_path

    settings = Settings(
        environment="prod",
        secret_key=_STRONG_SECRET,
        database_url=_PG_URL,
        dev_auth=False,
        oidc_enabled=True,
    )
    with caplog.at_level("WARNING", logger="iceberg.auth"):
        _warn_if_no_login_path(settings)
    assert not caplog.records


def test_no_login_warning_for_eval_overlay(caplog):
    # The beta overlay (non-prod + dev_auth) has a working dev-login path.
    from iceberg.main import _warn_if_no_login_path

    settings = Settings(environment="dev", dev_auth=True)
    assert settings.dev_login_enabled
    with caplog.at_level("WARNING", logger="iceberg.auth"):
        _warn_if_no_login_path(settings)
    assert not caplog.records
