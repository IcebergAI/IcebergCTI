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
        assert _ai_tile(snap) == {
            "label": "AI backend",
            "value": "openai",
            "ok": True,
            "tone": "is-ok",
        }
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


# --------------------------------------------------------------------------- #
# Credential-bearing URLs (#273)
# --------------------------------------------------------------------------- #
def test_inline_proxy_credentials_are_scrubbed_from_every_copy(engine, monkeypatch):
    """``http://user:pass@proxy:3128`` is the standard proxy-URL form and it
    works, so a password reaches this page inside an ordinary non-secret setting.
    The page promises secrets are never shown — that has to hold for a credential
    smuggled into a URL, and for the ``data-search`` copy of it too."""
    from iceberg.services import proxy_settings

    secret = "hunter2SuperSecret"
    monkeypatch.setattr(get_settings(), "proxy_url", f"http://alice:{secret}@proxy:3128")
    with Session(engine) as session:
        proxy_settings.update(session, proxy_url=f"http://alice:{secret}@proxy:3128")
        rows = effective_config.snapshot(session)["rows"]

    for name in ("ICEBERG_PROXY_URL", "Proxy.proxy_url"):
        value = _row(rows, name)["value"]
        assert secret not in value, f"{name} leaked the proxy password"
        assert "alice" not in value
        # Still useful for debugging: the operator can see where it points.
        assert "proxy:3128" in value


def test_no_row_value_anywhere_carries_url_userinfo(engine, monkeypatch):
    """The scrub is applied to every value, not a listed few — a credential in a
    field nobody thought to classify must still not render."""
    from iceberg.services import misp_settings

    monkeypatch.setattr(get_settings(), "misp_url", "https://u:p4ssw0rd@misp.test")
    with Session(engine) as session:
        misp_settings.update(session, url="https://u:p4ssw0rd@misp.test")
        rows = effective_config.snapshot(session)["rows"]
    assert not [r for r in rows if "p4ssw0rd" in r["value"]]


def test_webhook_url_is_reduced_to_its_origin(engine, monkeypatch):
    """A Slack incoming-webhook URL is bearer-equivalent by design — its path is
    the credential. Show the origin so an operator can confirm the destination,
    never the path."""
    from iceberg.services import webhook_settings

    slack = "https://hooks.slack.com/services/T0000/B0000/xoxbSecretPathValue"
    monkeypatch.setattr(get_settings(), "webhook_url", slack)
    with Session(engine) as session:
        webhook_settings.update(session, url=slack)
        rows = effective_config.snapshot(session)["rows"]

    for name in ("ICEBERG_WEBHOOK_URL", "Webhook.url"):
        value = _row(rows, name)["value"]
        assert "xoxbSecretPathValue" not in value
        assert "T0000" not in value
        assert value == "https://hooks.slack.com/…"


def test_admin_config_page_never_renders_a_url_credential(client, login, monkeypatch):
    """End to end through the template, including the data-search attribute that
    carries a second copy of every value."""
    monkeypatch.setattr(
        get_settings(), "proxy_url", "http://alice:PageLeakSecret@proxy:3128"
    )
    monkeypatch.setattr(
        get_settings(), "webhook_url", "https://hooks.slack.com/services/T/B/PathLeak"
    )
    login("ADMIN")
    page = client.get("/admin/config").text
    assert "PageLeakSecret" not in page
    assert "PathLeak" not in page


def test_every_url_shaped_field_is_consciously_classified():
    """Redaction must not be allow-by-default. The old guard's name heuristic
    (`_key`/`_secret`/`_password`/`_token`) misses exactly the class that keeps
    biting — credential-bearing URLs — so every URL/DSN-shaped field, on
    ``Settings`` AND on the DB settings rows, has to be in one of the three sets.
    A future ``sentry_dsn`` then fails here instead of rendering in full (#273)."""
    classified = (
        effective_config.SECRET_FIELDS
        | effective_config.ORIGIN_ONLY_FIELDS
        | effective_config.ORIGIN_ONLY_DB_FIELDS
        | effective_config.PLAINTEXT_URL_FIELDS
    )
    suffixes = ("_url", "_uri", "_dsn", "_endpoint")
    candidates = [f for f in Settings.model_fields if f.endswith(suffixes)]
    db_models = [
        ("OIDC", OIDCSettings),
        ("AI", AISettings),
        ("Audit", AuditSettings),
        ("Proxy", ProxySettings),
        ("MISP", MISPSettings),
        ("Webhook", WebhookSettings),
    ]
    candidates += [
        f"{prefix}.{field}"
        for prefix, model in db_models
        for field in model.model_fields
        if field.endswith(suffixes) or field in {"url", "uri", "endpoint"}
    ]
    assert candidates, "the heuristic matched nothing — it has stopped working"
    unclassified = [f for f in candidates if f not in classified]
    assert not unclassified, (
        f"URL-shaped config is rendered in full without anyone deciding it is "
        f"safe to: {unclassified}. Add each to SECRET_FIELDS, ORIGIN_ONLY_*, or "
        f"PLAINTEXT_URL_FIELDS."
    )


# --------------------------------------------------------------------------- #
# Capability tiles report the resolved state, not the stored flag (#274)
# --------------------------------------------------------------------------- #
def _tile(snapshot: dict, label: str) -> dict:
    return next(t for t in snapshot["tiles"] if t["label"] == label)


def test_misp_tile_is_not_green_when_enabled_but_unsendable(engine, monkeypatch):
    """The /admin hub already says NOT CONFIGURED for this state. /admin/config —
    the page an operator opens *to debug* — showed a green "on" for the same
    subsystem, because it read only the stored flag (#274)."""
    from iceberg.services import misp_settings

    monkeypatch.setattr(get_settings(), "misp_api_key", "")
    with Session(engine) as session:
        misp_settings.update(session, enabled=True, url="")
        tile = _tile(effective_config.snapshot(session), "MISP push")
        assert (tile["ok"], tile["tone"]) == (False, "is-warn")
        assert "URL" in tile["value"]

        # A URL but no env API key is equally unsendable — the key is read at
        # send time, so nothing on the row shows the problem.
        misp_settings.update(session, url="https://misp.test")
        tile = _tile(effective_config.snapshot(session), "MISP push")
        assert (tile["ok"], tile["tone"]) == (False, "is-warn")
        assert "API key" in tile["value"]

    monkeypatch.setattr(get_settings(), "misp_api_key", "k" * 20)
    with Session(engine) as session:
        tile = _tile(effective_config.snapshot(session), "MISP push")
        assert (tile["value"], tile["ok"], tile["tone"]) == ("on", True, "is-ok")


def test_switched_off_integration_reads_neutral_not_green_or_amber(engine):
    """Off is a deliberate choice, so it is neither a success nor a warning —
    the same tri-state the hub's _integration_tile uses."""
    with Session(engine) as session:
        snap = effective_config.snapshot(session)
    for label in ("MISP push", "Webhook"):
        tile = _tile(snap, label)
        assert (tile["value"], tile["tone"]) == ("off", "is-neutral")


def test_webhook_tile_flags_enabled_with_no_endpoint(engine):
    from iceberg.services import webhook_settings

    with Session(engine) as session:
        webhook_settings.update(session, enabled=True, url="")
        tile = _tile(effective_config.snapshot(session), "Webhook")
        assert (tile["ok"], tile["tone"]) == (False, "is-warn")

        webhook_settings.update(session, url="https://hooks.example.test/abc")
        tile = _tile(effective_config.snapshot(session), "Webhook")
        assert (tile["value"], tile["ok"]) == ("on", True)


def test_sso_tile_flags_a_provider_missing_its_locator(engine, monkeypatch):
    """Auth0 enabled with a client id + env secret but an empty domain: the save
    handler accepts it, `metadata_url` becomes `https:///.well-known/…`, and
    discovery fails at the first login — yet the tile read green (#274)."""
    from iceberg.services import oidc_settings

    monkeypatch.setattr(get_settings(), "oidc_auth0_client_secret", "s" * 24)
    with Session(engine) as session:
        oidc_settings.update(
            session, auth0_enabled=True, auth0_client_id="cid", auth0_domain=""
        )
        tile = _tile(effective_config.snapshot(session), "SSO providers")
        assert (tile["ok"], tile["tone"]) == (False, "is-warn")
        assert "domain" in tile["value"]

        oidc_settings.update(session, auth0_domain="acme.eu.auth0.com")
        tile = _tile(effective_config.snapshot(session), "SSO providers")
        assert (tile["value"], tile["ok"]) == ("auth0", True)


def test_an_unclassified_url_field_is_redacted_rather_than_rendered(engine):
    """Deny by default. The old posture rendered a new field in full unless
    someone remembered to classify it — the exact failure that put a Slack
    webhook URL on a page promising it never shows a secret. An unclassified
    URL-shaped field must fall back to origin-only (#273)."""
    assert effective_config._renders_origin_only("sentry_dsn") is True
    assert effective_config._renders_origin_only("Foo.url") is True
    # Acknowledged-plaintext and non-URL fields are unaffected.
    assert effective_config._renders_origin_only("misp_url") is False
    assert effective_config._renders_origin_only("MISP.url") is False
    assert effective_config._renders_origin_only("log_level") is False


def test_acknowledged_plaintext_urls_still_render_in_full(engine, monkeypatch):
    """The allowlist has to actually work — knowing exactly where a subsystem
    points is the reason this page exists."""
    monkeypatch.setattr(get_settings(), "misp_url", "https://misp.test/events/add")
    with Session(engine) as session:
        rows = effective_config.snapshot(session)["rows"]
    assert _row(rows, "ICEBERG_MISP_URL")["value"] == "https://misp.test/events/add"
