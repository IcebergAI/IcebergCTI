"""Effective (resolved) configuration page (#245).

Covers comprehensive coverage (every Settings field + every DB-row field),
provenance layering (database / environment / built-in default), secret redaction
(no value ever crosses the boundary), the validation block reflecting the prod
boot-guards, and admin-only gating of /admin/config.
"""

from sqlmodel import Session

from iceberg.config import Settings, get_settings
from iceberg.models import (
    AISettings,
    AuditSettings,
    MISPSettings,
    OIDCSettings,
    ProxySettings,
    WebhookSettings,
)
from iceberg.services import ai_settings, effective_config


def _row(rows, name):
    return next(r for r in rows if r["name"] == name)


def test_snapshot_covers_every_settings_field_and_db_row(engine):
    """Regression guard: a new Settings field or DB column must appear, so the
    'every value' promise can't silently regress."""
    with Session(engine) as session:
        names = {r["name"] for r in effective_config.snapshot(session)["rows"]}
    for field in Settings.model_fields:
        if field == "forwarded_allow_ips":
            assert "FORWARDED_ALLOW_IPS" in names
            continue
        assert f"ICEBERG_{field.upper()}" in names, f"missing Settings field {field}"
    db_models = [
        ("OIDC", OIDCSettings),
        ("AI", AISettings),
        ("Audit", AuditSettings),
        ("Proxy", ProxySettings),
        ("MISP", MISPSettings),
        ("Webhook", WebhookSettings),
    ]
    for prefix, model in db_models:
        for field in model.model_fields:
            if field in {"id", "updated_at"}:
                continue
            assert f"{prefix}.{field}" in names, f"missing DB field {prefix}.{field}"


def test_provenance_database_environment_and_default(engine):
    with Session(engine) as session:
        ai_settings.update(session, backend="claude", model="claude-opus-4-8")
        snap = effective_config.snapshot(session)
    rows = snap["rows"]
    # The authoritative DB value has provenance "database".
    assert _row(rows, "AI.backend")["provenance"] == "database"
    assert _row(rows, "AI.backend")["value"] == "claude"
    # Set from the environment (conftest sets ICEBERG_DEV_AUTH).
    assert _row(rows, "ICEBERG_DEV_AUTH")["provenance"] == "environment"
    # Never set → built-in default.
    assert _row(rows, "ICEBERG_SMTP_HOST")["provenance"] == "built-in default"


def test_oidc_client_secrets_are_env_provenance_not_database(engine):
    """Review fix: env-only OIDC client secrets must not be labeled 'database'."""
    with Session(engine) as session:
        rows = effective_config.snapshot(session)["rows"]
    for provider in ("", "AUTHENTIK_", "AUTH0_", "OKTA_"):
        row = _row(rows, f"ICEBERG_OIDC_{provider}CLIENT_SECRET")
        assert row["secret"] is True
        assert row["provenance"] in ("environment", "built-in default")


def test_forwarded_allow_ips_reads_the_unprefixed_env_var(engine, monkeypatch):
    """Review fix: the guard + uvicorn consume the unprefixed FORWARDED_ALLOW_IPS."""
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.0.0.0/8")
    with Session(engine) as session:
        row = _row(effective_config.snapshot(session)["rows"], "FORWARDED_ALLOW_IPS")
    assert row["value"] == "10.0.0.0/8"
    assert row["provenance"] == "environment"


def test_secrets_are_never_serialized_as_values(engine, monkeypatch):
    monkeypatch.setattr(get_settings(), "misp_api_key", "SUPER-SECRET-VALUE")
    monkeypatch.setattr(get_settings(), "ai_api_key", "ANOTHER-SECRET")
    with Session(engine) as session:
        snap = effective_config.snapshot(session)
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


def _ai_tile(snapshot: dict) -> dict:
    return next(t for t in snapshot["tiles"] if t["label"] == "AI backend")


def test_ai_tile_reports_the_resolved_backend_not_the_stored_one(engine, monkeypatch):
    """A selected-but-invalid provider is the most misleading AI state there is:
    ``ai_settings.resolve`` fail-closes it to "none", so assist is off while the
    row still says "openai". The page must show what the runtime will use, or an
    operator debugging "why is AI doing nothing?" is sent the wrong way."""
    monkeypatch.setattr(get_settings(), "ai_api_key", "")  # openai needs a key
    with Session(engine) as session:
        ai_settings.update(session, backend="openai", model="gpt-5")
        row = ai_settings.get(session)
        assert ai_settings.validate_selection(row), "fixture must be invalid"
        # The runtime really does disable it — this is the behaviour being mirrored.
        assert ai_settings.resolve(session).ai_backend == "none"

        snap = effective_config.snapshot(session)
        tile = _ai_tile(snap)
        assert tile["ok"] is False
        assert tile["value"].startswith("none")
        assert "openai" in tile["value"]
        # …and the reason is spelled out, not just the red pill.
        assert any(
            "openai" in advisory and "fail-closed" in advisory
            for advisory in snap["advisories"]
        )


def test_ai_tile_is_green_only_for_a_selection_that_actually_resolves(
    engine, monkeypatch
):
    monkeypatch.setattr(get_settings(), "ai_api_key", "k" * 20)
    with Session(engine) as session:
        ai_settings.update(session, backend="openai", model="gpt-5")
        assert not ai_settings.validate_selection(ai_settings.get(session))

        snap = effective_config.snapshot(session)
        assert _ai_tile(snap) == {"label": "AI backend", "value": "openai", "ok": True}
        assert not [a for a in snap["advisories"] if "fail-closed" in a]


def test_ai_tile_off_when_no_backend_is_selected(engine):
    with Session(engine) as session:
        tile = _ai_tile(effective_config.snapshot(session))
        assert (tile["value"], tile["ok"]) == ("none", False)


def test_secret_looking_settings_are_all_classified_as_secret():
    """SECRET_FIELDS is a manual allowlist while the row set is built
    automatically, so a future ``*_api_key`` would be rendered in full unless a
    developer remembered to classify it. Catch that omission by name."""
    suspicious = [
        field
        for field in Settings.model_fields
        if field.endswith(("_key", "_secret", "_password", "_token"))
        or "password" in field
        or "secret" in field
    ]
    assert suspicious, "the heuristic matched nothing — it has stopped working"
    unclassified = [f for f in suspicious if f not in effective_config.SECRET_FIELDS]
    assert not unclassified, (
        f"secret-looking settings are not in SECRET_FIELDS and would be rendered "
        f"in full: {unclassified}"
    )
