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

from sqlmodel import col, select

from ..config import _INSECURE_DEFAULT_SECRET, get_settings
from ..models import Feed, ProxyMode
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
    # The AI tile must report what ``ai_settings.resolve`` will actually use, not
    # what the row says: resolve fail-closes an invalid selection to "none".
    ai_row = ai_settings.get(session)
    ai_errors = ai_settings.validate_selection(ai_row)
    return {
        "rows": [asdict(r) for r in rows],
        "categories": categories,
        "validation": _validation(s),
        "advisories": _advisories(s, providers, ai_row, ai_errors),
        "tiles": _tiles(session, s, providers, ai_row, ai_errors),
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


def _advisories(s, providers: list[str], ai_row, ai_errors: list[str]) -> list[str]:
    out: list[str] = []
    # A selected-but-invalid provider is the most misleading AI state there is:
    # the row says "openai", the runtime resolves to "none", and assist silently
    # does nothing. Name the provider and every reason it was rejected.
    if ai_row.backend != "none" and ai_errors:
        out.append(
            f"AI provider {ai_row.backend!r} is selected but invalid, so AI assist "
            f"is disabled at runtime (fail-closed): {'; '.join(ai_errors)}"
        )
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


@dataclass(frozen=True)
class HubTile:
    """One subsystem on the ``/admin`` Settings hub: where to configure it, what
    state it is in right now, and one line of context."""

    group: str  # "Outbound integrations" | "Governance"
    title: str
    href: str
    status: str  # short pill label (OFF / ENABLED / 3 ACTIVE …)
    tone: str  # tag modifier: is-neutral | is-warn | is-ok
    meta: str


def admin_hub_tiles(session) -> list[dict]:
    """Status pills for the ``/admin`` hub — one per admin-configurable subsystem.

    Reads the same settings singletons the deep config pages own, so the hub can
    never disagree with them, and it introduces no new state. Secrets are only
    ever reported as set/not-set (an unset env key is what makes an otherwise
    "enabled" integration NOT CONFIGURED).
    """
    s = get_settings()
    ai = ai_settings.get(session)
    # Same discipline as the /admin/config AI tile: report what the runtime will
    # actually use. ``ai_settings.resolve`` fail-closes an invalid selection to
    # "none", so a green pill on a row that says "openai" would be a lie.
    ai_errors = ai_settings.validate_selection(ai)
    misp = misp_settings.get(session)
    webhook = webhook_settings.get(session)
    audit = audit_settings.get(session)
    proxy = proxy_settings.get(session)
    # A provider with a client id but no env client secret cannot complete the
    # authorization-code flow — it looks enabled and fails at login, so it must
    # not read green here either.
    configs = oidc_settings.enabled_providers(session)
    providers = [c.name for c in configs]
    unusable_sso = [c.name for c in configs if not c.client_secret]
    active_feeds = len(
        list(session.exec(select(Feed).where(col(Feed.enabled).is_(True))).all())
    )
    # An HTTP sink with no endpoint claims off-box forwarding it cannot perform.
    audit_broken = (
        "HTTP sink selected but no endpoint is set — nothing is forwarded"
        if "http" in audit.methods and not audit.http_endpoint.strip()
        else ""
    )
    issues = len(_validation(s)["errors"])

    tiles = [
        HubTile(
            group="Outbound integrations",
            title="AI provider",
            href="/admin/ai",
            status=(
                "OFF"
                if ai.backend == "none"
                else "NOT CONFIGURED"
                if ai_errors
                else ai.backend.upper()
            ),
            tone=(
                "is-neutral"
                if ai.backend == "none"
                else "is-warn"
                if ai_errors
                else "is-ok"
            ),
            meta=(
                "Governed AI assist · backend not selected"
                if ai.backend == "none"
                else f"{ai.backend} selected but disabled at runtime: {ai_errors[0]}"
                if ai_errors
                else f"Governed AI assist · egress ceiling TLP:{ai.max_tlp}"
            ),
        ),
        _integration_tile(
            title="MISP push",
            href="/admin/misp",
            enabled=misp.enabled,
            configured=bool(misp.url.strip() and s.misp_api_key),
            ready_meta="Indicator egress · one event per report",
            unconfigured_meta=(
                "Indicator egress · no MISP URL set"
                if not misp.url.strip()
                else "Indicator egress · no API key set"
            ),
            off_meta="Indicator egress · push disabled",
        ),
        HubTile(
            group="Outbound integrations",
            title="Outbound proxy",
            href="/admin/proxy",
            status=(
                "DIRECT"
                if proxy.mode == ProxyMode.NONE
                else ("EXPLICIT" if proxy.proxy_url.strip() else "NOT CONFIGURED")
                if proxy.mode == ProxyMode.EXPLICIT
                else "SYSTEM"
            ),
            tone=(
                "is-warn"
                if proxy.mode == ProxyMode.EXPLICIT and not proxy.proxy_url.strip()
                else "is-ok"
            ),
            meta="RSS · SIEM · MISP · AI · webhook routing",
        ),
        HubTile(
            group="Outbound integrations",
            title="RSS feeds",
            href="/admin/feeds",
            status=f"{active_feeds} ACTIVE" if active_feeds else "NONE",
            tone="is-ok" if active_feeds else "is-neutral",
            meta="Inbound collection · SSRF-guarded fetcher",
        ),
        _integration_tile(
            title="Publication webhook",
            href="/admin/webhook",
            enabled=webhook.enabled,
            configured=bool(webhook.url.strip()),
            ready_meta=f"{webhook.format.capitalize()} envelope on publish",
            unconfigured_meta="No endpoint URL set",
            off_meta="No publication callout",
        ),
        HubTile(
            group="Outbound integrations",
            title="Single sign-on",
            href="/admin/oidc",
            status=(
                "NOT CONFIGURED"
                if not providers or unusable_sso
                else ", ".join(p.upper() for p in providers)
            ),
            tone="is-warn" if (not providers or unusable_sso) else "is-ok",
            meta=(
                f"No client secret set for {', '.join(unusable_sso)} — sign-in will fail"
                if unusable_sso
                else f"{len(providers)} provider{'' if len(providers) == 1 else 's'} · "
                f"dev-login {'on' if s.dev_login_enabled else 'off'}"
            ),
        ),
        HubTile(
            group="Governance",
            title="Audit log & SIEM",
            href="/admin/audit",
            status=(
                "OFF"
                if not audit.enabled
                else "LOCAL ONLY"
                if set(audit.methods) <= {"stdout"}
                else "NOT CONFIGURED"
                if audit_broken
                else ", ".join(m.upper() for m in audit.methods)
            ),
            tone=(
                "is-neutral"
                if not audit.enabled
                else "is-warn"
                if set(audit.methods) <= {"stdout"} or audit_broken
                else "is-ok"
            ),
            meta=(
                "Security events are not being recorded"
                if not audit.enabled
                else "No SIEM sink enabled"
                if set(audit.methods) <= {"stdout"}
                else audit_broken
                if audit_broken
                else "Forensic trail forwarded off-box"
            ),
        ),
        HubTile(
            group="Governance",
            title="Effective config",
            href="/admin/config",
            status=f"{issues} ISSUE{'' if issues == 1 else 'S'}" if issues else "VIEW",
            tone="is-warn" if issues else "is-neutral",
            meta="Resolved runtime settings, provenance + prod guards",
        ),
    ]
    return [asdict(t) for t in tiles]


def _integration_tile(
    *,
    title: str,
    href: str,
    enabled: bool,
    configured: bool,
    ready_meta: str,
    unconfigured_meta: str,
    off_meta: str,
) -> HubTile:
    """An opt-in outbound integration: off → not configured → enabled."""
    if not enabled:
        return HubTile(
            group="Outbound integrations",
            title=title,
            href=href,
            status="OFF",
            tone="is-neutral",
            meta=off_meta,
        )
    if not configured:
        return HubTile(
            group="Outbound integrations",
            title=title,
            href=href,
            status="NOT CONFIGURED",
            tone="is-warn",
            meta=unconfigured_meta,
        )
    return HubTile(
        group="Outbound integrations",
        title=title,
        href=href,
        status="ENABLED",
        tone="is-ok",
        meta=ready_meta,
    )


def _tiles(session, s, providers: list[str], ai_row, ai_errors: list[str]) -> list[dict]:
    def onoff(flag: bool) -> str:
        return "on" if flag else "off"

    misp = misp_settings.get(session)
    webhook = webhook_settings.get(session)
    typst = shutil.which(s.typst_bin) is not None
    return [
        {"label": "Environment", "value": s.environment, "ok": s.is_prod},
        {
            "label": "AI backend",
            # Mirror the fail-closed resolution, so the tile can never show a
            # green backend while ``resolve`` is handing "none" to services/ai.
            "value": (
                f"none (selected {ai_row.backend}: invalid)"
                if ai_errors and ai_row.backend != "none"
                else ai_row.backend
            ),
            "ok": not ai_errors and ai_row.backend != "none",
        },
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
