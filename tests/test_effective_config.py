"""Effective (resolved) configuration page (#245).

Covers provenance layering (database / environment / built-in default), secret
redaction (no value ever crosses the boundary), the validation block reflecting
the prod boot-guards, and admin-only gating of /admin/config.
"""

from sqlmodel import Session

from iceberg.config import get_settings
from iceberg.services import ai_settings, effective_config


def _row(rows, name):
    return next(r for r in rows if r["name"] == name)


def test_provenance_database_environment_and_default(engine):
    with Session(engine) as session:
        # A DB-backed value is authoritative → provenance "database".
        ai_settings.update(session, backend="claude", model="claude-opus-4-8")
        snap = effective_config.snapshot(session)
    rows = snap["rows"]
    assert _row(rows, "ICEBERG_AI_BACKEND")["provenance"] == "database"
    assert _row(rows, "ICEBERG_AI_BACKEND")["value"] == "claude"
    # Set from the environment (conftest sets ICEBERG_DEV_AUTH).
    assert _row(rows, "ICEBERG_DEV_AUTH")["provenance"] == "environment"
    # Never set → built-in default.
    assert _row(rows, "ICEBERG_SMTP_HOST")["provenance"] == "built-in default"


def test_secrets_are_never_serialized_as_values(engine, monkeypatch):
    monkeypatch.setattr(get_settings(), "misp_api_key", "SUPER-SECRET-VALUE")
    monkeypatch.setattr(get_settings(), "ai_api_key", "ANOTHER-SECRET")
    with Session(engine) as session:
        snap = effective_config.snapshot(session)
    # Every secret row carries only a set/not-set status, and no secret value or
    # prefix appears anywhere in the serialized snapshot.
    for row in snap["rows"]:
        if row["secret"]:
            assert row["value"] in ("set", "not set")
    blob = str(snap)
    assert "SUPER-SECRET-VALUE" not in blob
    assert "ANOTHER-SECRET" not in blob
    assert _row(snap["rows"], "ICEBERG_MISP_API_KEY")["value"] == "set"


def test_validation_reflects_prod_guards(engine, monkeypatch):
    monkeypatch.setattr(get_settings(), "environment", "prod")
    monkeypatch.setattr(get_settings(), "secret_key", "short")
    monkeypatch.setattr(get_settings(), "database_url", "sqlite:///./x.db")
    with Session(engine) as session:
        snap = effective_config.snapshot(session)
    assert snap["validation"]["ok"] is False
    joined = " ".join(snap["validation"]["errors"])
    assert "ICEBERG_SECRET_KEY" in joined
    assert "PostgreSQL" in joined


def test_admin_config_is_admin_only(client, login):
    login("ANALYST")
    assert client.get("/admin/config").status_code == 403
    login("ADMIN")
    assert client.get("/admin/config").status_code == 200


def test_admin_config_page_shows_status_not_secret(client, login, monkeypatch):
    monkeypatch.setattr(get_settings(), "webhook_token", "TOP-SECRET-TOKEN")
    login("ADMIN")
    page = client.get("/admin/config").text
    assert "TOP-SECRET-TOKEN" not in page
    assert "Effective configuration" in page
