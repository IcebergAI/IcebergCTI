"""Effective (resolved) runtime configuration — the read-only ``/admin/config``
snapshot (#245).

Answers "what config is this process actually using, where did each value come
from, and which optional features are available?" for an operator debugging a
beta instance — without shell access. It is **comprehensive**: one row for every
``Settings`` field AND every field on the six admin-editable DB settings rows
(Audit/Proxy/MISP/Webhook/AI/OIDC), each with a **provenance** — `database`
(a settings row is authoritative), `environment` (`settings.model_fields_set`),
or `built-in default`. A regression test (`test_effective_config.py`) asserts the
coverage so a future field can't be silently omitted.

Secrets never leave the server: a field flagged ``secret`` is coerced to a plain
``set`` / ``not set`` string here — no value or prefix crosses the boundary.
"""

import os
import shutil
from dataclasses import asdict, dataclass

from ..config import _INSECURE_DEFAULT_SECRET, get_settings
from . import (
    ai_settings,
    audit_settings,
    misp_settings,
    oidc_settings,
    proxy_settings,
    webhook_settings,
)

# ``Settings`` field names whose value is a secret — surfaced only as set/not-set.
SECRET_FIELDS: frozenset[str] = frozenset(
    {
        "secret_key",
        "database_url",
        "ai_api_key",
        "misp_api_key",
        "webhook_token",
        "audit_http_token",
        "proxy_username",
        "proxy_password",
        "smtp_password",
        "rate_limit_redis_url",
        "oidc_client_secret",
        "oidc_authentik_client_secret",
        "oidc_auth0_client_secret",
        "oidc_okta_client_secret",
    }
)

# The unprefixed env var (guard + uvicorn read this, NOT ICEBERG_FORWARDED_ALLOW_IPS).
_FORWARDED_FIELD = "forwarded_allow_ips"

_CATEGORY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("ai_", "AI provider"),
    ("oidc_", "SSO / auth"),
    ("dev_", "SSO / auth"),
    ("smtp_", "Email"),
    ("email_", "Email"),
    ("audit_", "Audit / SIEM"),
    ("siem_", "Audit / SIEM"),
    ("proxy_", "Proxy"),
    ("rate_limit", "Rate limiting"),
    ("rss_", "RSS ingestion"),
    ("feed_", "RSS ingestion"),
    ("misp_", "MISP"),
    ("webhook_", "Webhook"),
    ("render_", "Rendering"),
    ("typst_", "Rendering"),
    ("attachment", "Uploads"),
    ("attachments", "Uploads"),
    ("figure", "Uploads"),
    ("figures", "Uploads"),
    ("jobs_", "Durable jobs"),
    ("dissemination", "Dissemination"),
)
# Built from a category → fields mapping (avoids a dict with a password-like key
# paired with a string literal, which bandit's B105 would false-positive on).
_EXACT_CATEGORIES: dict[str, tuple[str, ...]] = {
    "Process / security": (
        "environment",
        "secret_key",
        "database_url",
        "forwarded_allow_ips",
        "portal_base_url",
        "log_level",
        "log_format",
        "cors_origins",
    ),
    "General runtime": ("stix_namespace",),
}
_CATEGORY_EXACT = {
    field: category
    for category, fields in _EXACT_CATEGORIES.items()
    for field in fields
}

# The DB settings rows, in display order: (category, getter, field-name prefix).
_DB_ROWS = (
    ("SSO / auth", oidc_settings.get, "OIDC"),
    ("AI provider", ai_settings.get, "AI"),
    ("Audit / SIEM", audit_settings.get, "Audit"),
    ("Proxy", proxy_settings.get, "Proxy"),
    ("MISP", misp_settings.get, "MISP"),
    ("Webhook", webhook_settings.get, "Webhook"),
)
_DB_SKIP = {"id", "updated_at"}


@dataclass(frozen=True)
class ConfigRow:
    name: str
    category: str
    secret: bool
    value: str
    provenance: str  # database | environment | built-in default


def _category(field: str) -> str:
    if field in _CATEGORY_EXACT:
        return _CATEGORY_EXACT[field]
    for prefix, cat in _CATEGORY_PREFIXES:
        if field.startswith(prefix):
            return cat
    return "General runtime"


def _display(value: object) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) or "(empty)"
    text = str(value)
    return text if text != "" else "(empty)"


def snapshot(session) -> dict:
    """Build the effective-config view: rows (with provenance), a validation
    block, advisories, and feature-capability tiles."""
    s = get_settings()
    rows: list[ConfigRow] = []

    # 1. Every Settings field — the environment / built-in-default layer.
    for field in type(s).model_fields:
        if field == _FORWARDED_FIELD:
            continue  # emitted specially below (the guard reads the unprefixed var)
        secret = field in SECRET_FIELDS
        value = getattr(s, field)
        rows.append(
            ConfigRow(
                name=f"ICEBERG_{field.upper()}",
                category=_category(field),
                secret=secret,
                value="set" if (secret and value) else ("not set" if secret else _display(value)),
                provenance="environment" if field in s.model_fields_set else "built-in default",
            )
        )

    # FORWARDED_ALLOW_IPS: the prod guard + uvicorn consume the UNPREFIXED env var.
    fwd_env = os.getenv("FORWARDED_ALLOW_IPS")
    rows.append(
        ConfigRow(
            name="FORWARDED_ALLOW_IPS",
            category="Process / security",
            secret=False,
            value=_display(fwd_env if fwd_env is not None else s.forwarded_allow_ips),
            provenance=(
                "environment"
                if fwd_env is not None or _FORWARDED_FIELD in s.model_fields_set
                else "built-in default"
            ),
        )
    )

    # 2. Every field on the six admin-editable DB rows — the authoritative layer.
    for category, getter, prefix in _DB_ROWS:
        row = getter(session)
        for field, value in row.model_dump().items():
            if field in _DB_SKIP:
                continue
            rows.append(
                ConfigRow(
                    name=f"{prefix}.{field}",
                    category=category,
                    secret=False,  # DB rows never hold secrets
                    value=_display(value),
                    provenance="database",
                )
            )

    categories = list(dict.fromkeys(r.category for r in rows))
    providers = [p.name for p in oidc_settings.enabled_providers(session)]
    return {
        "rows": [asdict(r) for r in rows],
        "categories": categories,
        "validation": _validation(s),
        "advisories": _advisories(s, providers),
        "tiles": _tiles(session, s, providers),
    }


def _validation(s) -> dict:
    """Re-run the prod boot-guards non-fatally so the page can list every issue."""
    errors: list[str] = []
    if s.is_prod and (
        s.secret_key == _INSECURE_DEFAULT_SECRET or len(s.secret_key) < 32
    ):
        errors.append(
            "ICEBERG_SECRET_KEY must be a unique value of at least 32 characters in "
            "production (the built-in default is public)."
        )
    if s.is_prod and s.is_sqlite:
        errors.append(
            "ICEBERG_DATABASE_URL must be a PostgreSQL URL in production; SQLite is "
            "for local dev/test only."
        )
    forwarded = os.getenv("FORWARDED_ALLOW_IPS", s.forwarded_allow_ips)
    if s.is_prod and "*" in {item.strip() for item in forwarded.split(",")}:
        errors.append("FORWARDED_ALLOW_IPS cannot contain '*' in production.")
    return {"ok": not errors, "errors": errors}


def _advisories(s, providers: list[str]) -> list[str]:
    out: list[str] = []
    if not s.dev_login_enabled and not providers:
        out.append(
            "No login path is configured: dev-auth is off and no OIDC provider is "
            "enabled. Users cannot sign in."
        )
    if s.is_prod and s.email_backend == "console":
        out.append(
            "Email backend is 'console' in production — notifications are only "
            "logged, never delivered."
        )
    if s.is_prod and s.rate_limit_active and s.rate_limit_store == "memory":
        out.append(
            "Rate limiting uses the in-memory store in production — buckets are not "
            "shared across workers."
        )
    return out


def _tiles(session, s, providers: list[str]) -> list[dict]:
    def onoff(flag: bool) -> str:
        return "on" if flag else "off"

    ai = ai_settings.get(session)
    misp = misp_settings.get(session)
    webhook = webhook_settings.get(session)
    typst = shutil.which(s.typst_bin) is not None
    return [
        {"label": "Environment", "value": s.environment, "ok": s.is_prod},
        {"label": "AI backend", "value": ai.backend, "ok": ai.backend != "none"},
        {
            "label": "SSO providers",
            "value": ", ".join(providers) or "none",
            "ok": bool(providers),
        },
        {"label": "Dev login", "value": onoff(s.dev_login_enabled), "ok": not s.dev_login_enabled},
        {"label": "Rate limiting", "value": onoff(s.rate_limit_active), "ok": s.rate_limit_active},
        {"label": "Email", "value": s.email_backend, "ok": s.email_backend == "smtp"},
        {"label": "RSS poll", "value": onoff(s.rss_poll_enabled), "ok": True},
        {"label": "MISP push", "value": onoff(misp.enabled), "ok": True},
        {"label": "Webhook", "value": onoff(webhook.enabled), "ok": True},
        {"label": "PDF (Typst)", "value": "available" if typst else "missing", "ok": typst},
    ]
