"""Effective (resolved) runtime configuration — the read-only ``/admin/config``
snapshot (#245).

Answers "what config is this process actually using, where did each value come
from, and which optional features are available?" for an operator debugging a
beta instance — without shell access. Introspects the pydantic ``Settings`` **and**
the six admin-editable DB settings rows, and emits one row per operationally
meaningful field with a **provenance** (`database` / `environment` /
`built-in default`).

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


@dataclass(frozen=True)
class ConfigRow:
    name: str
    category: str
    secret: bool
    value: str
    provenance: str  # database | environment | built-in default


def _env_provenance(field: str) -> str:
    """Environment if the operator set it, else the built-in default."""
    return (
        "environment" if field in get_settings().model_fields_set else "built-in default"
    )


def _secret_display(value: object) -> str:
    return "set" if value else "not set"


def snapshot(session) -> dict:
    """Build the effective-config view: rows (with provenance), a validation
    block, advisories, and feature-capability tiles."""
    s = get_settings()
    ai = ai_settings.get(session)
    misp = misp_settings.get(session)
    proxy = proxy_settings.get(session)
    webhook = webhook_settings.get(session)
    audit = audit_settings.get(session)
    oidc = oidc_settings.get(session)
    providers = [p.name for p in oidc_settings.enabled_providers(session)]

    rows: list[ConfigRow] = []

    def env(name: str, field: str, category: str, *, secret: bool = False) -> None:
        value = getattr(s, field)
        rows.append(
            ConfigRow(
                name=name,
                category=category,
                secret=secret,
                value=_secret_display(value) if secret else str(value),
                provenance=_env_provenance(field),
            )
        )

    def db(name: str, value: object, category: str, *, secret: bool = False) -> None:
        rows.append(
            ConfigRow(
                name=name,
                category=category,
                secret=secret,
                value=_secret_display(value) if secret else str(value),
                provenance="database",
            )
        )

    # Process / security
    env("ICEBERG_ENVIRONMENT", "environment", "Process / security")
    env("ICEBERG_SECRET_KEY", "secret_key", "Process / security", secret=True)
    env("ICEBERG_DATABASE_URL", "database_url", "Process / security", secret=True)
    env("FORWARDED_ALLOW_IPS", "forwarded_allow_ips", "Process / security")
    env("ICEBERG_DEV_AUTH", "dev_auth", "Process / security")

    # SSO / auth (OIDC — DB-backed per provider; secrets are env-only)
    db("OIDC redirect base", oidc.redirect_base_url or "(portal base)", "SSO / auth")
    for name in oidc_settings.PROVIDERS:
        db(f"OIDC {name} enabled", getattr(oidc, f"{name}_enabled"), "SSO / auth")
        db(
            f"OIDC {name} client secret",
            s.oidc_client_secret_for(name),
            "SSO / auth",
            secret=True,
        )

    # AI provider (DB-backed; API key env-only)
    db("ICEBERG_AI_BACKEND", ai.backend, "AI provider")
    db("ICEBERG_AI_MODEL", ai.model or "(unset)", "AI provider")
    db("ICEBERG_AI_BASE_URL", ai.base_url or "(unset)", "AI provider")
    db("ICEBERG_AI_MAX_TLP", ai.max_tlp, "AI provider")
    env("ICEBERG_AI_API_KEY", "ai_api_key", "AI provider", secret=True)
    env("ICEBERG_AI_OLLAMA_BASE_URL", "ai_ollama_base_url", "AI provider")

    # Email
    env("ICEBERG_EMAIL_BACKEND", "email_backend", "Email")
    env("ICEBERG_SMTP_HOST", "smtp_host", "Email")
    env("ICEBERG_SMTP_PORT", "smtp_port", "Email")
    env("ICEBERG_SMTP_USER", "smtp_user", "Email")
    env("ICEBERG_SMTP_PASSWORD", "smtp_password", "Email", secret=True)

    # Audit / SIEM (DB-backed routing; HEC token env-only)
    db("Audit enabled", audit.enabled, "Audit / SIEM")
    db("Audit methods", ", ".join(audit.methods) or "(none)", "Audit / SIEM")
    db("Audit HTTP endpoint", audit.http_endpoint or "(unset)", "Audit / SIEM")
    env("ICEBERG_AUDIT_HTTP_TOKEN", "audit_http_token", "Audit / SIEM", secret=True)
    env("ICEBERG_AUDIT_RETENTION_DAYS", "audit_retention_days", "Audit / SIEM")

    # Proxy (DB-backed routing; credentials env-only)
    db("Proxy mode", proxy.mode, "Proxy")
    db("Proxy URL", proxy.proxy_url or "(unset)", "Proxy")
    env("ICEBERG_PROXY_USERNAME", "proxy_username", "Proxy", secret=True)
    env("ICEBERG_PROXY_PASSWORD", "proxy_password", "Proxy", secret=True)

    # MISP (DB-backed; API key env-only)
    db("MISP enabled", misp.enabled, "MISP")
    db("MISP URL", misp.url or "(unset)", "MISP")
    env("ICEBERG_MISP_API_KEY", "misp_api_key", "MISP", secret=True)

    # Webhook (DB-backed; token env-only)
    db("Webhook enabled", webhook.enabled, "Webhook")
    db("Webhook URL", webhook.url or "(unset)", "Webhook")
    env("ICEBERG_WEBHOOK_TOKEN", "webhook_token", "Webhook", secret=True)

    # Rate limiting
    env("ICEBERG_RATE_LIMIT_STORE", "rate_limit_store", "Rate limiting")
    env("ICEBERG_RATE_LIMIT_REDIS_URL", "rate_limit_redis_url", "Rate limiting", secret=True)

    # RSS ingestion + retention
    env("ICEBERG_RSS_POLL_ENABLED", "rss_poll_enabled", "RSS ingestion")
    env("ICEBERG_FEED_ITEM_RETENTION_DAYS", "feed_item_retention_days", "RSS ingestion")
    env("ICEBERG_MAX_BODY_MB", "max_body_mb", "General runtime")

    categories = list(dict.fromkeys(r.category for r in rows))

    return {
        "rows": [asdict(r) for r in rows],
        "categories": categories,
        "validation": _validation(s),
        "advisories": _advisories(s, providers),
        "tiles": _tiles(s, ai, misp, webhook, providers),
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


def _tiles(s, ai, misp, webhook, providers: list[str]) -> list[dict]:
    def onoff(flag: bool) -> str:
        return "on" if flag else "off"

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
