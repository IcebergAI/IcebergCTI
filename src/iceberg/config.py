"""Application configuration loaded from environment / .env (ICEBERG_ prefix)."""

import logging
import os
from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_AI_BACKENDS = {"none", "openai-compatible", "claude", "bedrock"}
_LOG_FORMATS = {"auto", "text", "json"}
_RATE_LIMIT_STORES = {"auto", "redis", "memory"}
_ENVIRONMENTS = {"dev", "test", "prod"}
_EMAIL_BACKENDS = {"console", "smtp"}
_TLP_VALUES = {"CLEAR", "GREEN", "AMBER", "AMBER_STRICT", "RED"}

# The default signing key shipped for local dev. It is public (it's in source
# control), so running with it in production would let anyone forge JWTs.
_INSECURE_DEFAULT_SECRET = "dev-insecure-change-me-0123456789abcdef"  # nosec B105 — public dev default, rejected in prod by _guard_production


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="ICEBERG_", extra="ignore"
    )

    # Core
    app_name: str = "Iceberg"
    environment: str = "dev"
    secret_key: str = _INSECURE_DEFAULT_SECRET
    database_url: str = "sqlite:///./iceberg.db"
    # Socket peers allowed to supply X-Forwarded-* to uvicorn. Wildcard trust is
    # rejected in production because client IPs key auth limits and audit data.
    forwarded_allow_ips: str = "127.0.0.1"

    # Application logs. ``auto`` keeps local/dev readable and makes production
    # container logs structured by default; uvicorn.* loggers are left alone.
    log_level: str = "INFO"
    log_format: str = "auto"  # auto | text | json

    # Schema migrations. When true, init_db() runs `alembic upgrade head` on boot
    # (idempotent) — convenient for local dev. Set false in production so the
    # deploy step owns migrations explicitly.
    auto_migrate: bool = True

    # App JWT (minted by us after OIDC or dev login)
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 480

    # Dev login bypass
    dev_auth: bool = True
    dev_user_email: str = "analyst@example.com"
    dev_user_name: str = "Dev Analyst"
    dev_user_role: str = "ANALYST"

    # OIDC. Multi-provider (Entra + Authentik + Auth0 + Okta), admin-configurable
    # on the OIDCSettings DB row (edit at /admin/oidc; env seeds the row on first
    # read). The Entra env fields below remain the back-compat seed for a single
    # existing Entra deployment. ``oidc_enabled`` is the master switch; a provider
    # additionally needs its own ``<provider>_enabled`` flag on the row.
    oidc_enabled: bool = False
    oidc_tenant_id: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""  # Entra client secret (env-only)
    oidc_redirect_uri: str = "http://localhost:8000/auth/callback"
    oidc_role_claim: str = "roles"
    oidc_department_claim: str = "department"
    oidc_title_claim: str = "jobTitle"
    oidc_company_claim: str = "companyName"
    oidc_office_claim: str = "officeLocation"
    # Per-provider client secrets — env-only (like the Entra one above), never a
    # DB column. ``ICEBERG_OIDC_<PROVIDER>_CLIENT_SECRET``.
    oidc_authentik_client_secret: str = ""
    oidc_auth0_client_secret: str = ""
    oidc_okta_client_secret: str = ""
    # Base URL the provider redirects back to; the per-provider callback path
    # (/auth/oidc/<provider>/callback) is appended. Blank derives from portal_base_url.
    oidc_redirect_base_url: str = ""

    def oidc_client_secret_for(self, provider: str) -> str:
        """The env-sourced client secret for a provider (never DB-persisted)."""
        return {
            "entra": self.oidc_client_secret,
            "authentik": self.oidc_authentik_client_secret,
            "auth0": self.oidc_auth0_client_secret,
            "okta": self.oidc_okta_client_secret,
        }.get(provider, "")

    # Typst rendering
    typst_bin: str = "typst"
    render_output_dir: str = "./rendered"
    cmarker_version: str = "0.1.1"
    typst_timeout: int = 60  # seconds; guards against a runaway compile
    render_retention_keep: int = 3
    render_retention_days: int = 90

    # STIX object ids are deterministic. New IcebergAI deployments use the
    # canonical repository namespace; deployments that already distributed ids
    # from the legacy project can pin the old namespace during their migration.
    stix_namespace: str = "https://github.com/IcebergAI/IcebergCTI"

    # Notebook attachments (uploaded reference files)
    attachments_dir: str = "./attachments"
    attachment_max_mb: int = 25
    # Comma-separated whitelist of accepted MIME types. SVG is deliberately
    # excluded (scriptable); executables/archives are not listed.
    attachment_allowed_types: str = (
        "application/pdf,"
        "image/png,image/jpeg,image/gif,image/webp,"
        "text/plain,text/markdown,text/csv,"
        "application/msword,"
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document,"
        "application/vnd.ms-excel,"
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,"
        "application/vnd.ms-powerpoint,"
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )

    # Notebook figures (uploaded images embedded inline into reports via the
    # [[figure:ID]] token). Restricted to PNG/JPEG/GIF in services/figures.py
    # (the browser-data-URI ∩ Typst-image() intersection); stored on disk like
    # attachments. Smaller default cap than attachments — figure bytes are
    # base64-inlined into the report HTML.
    figures_dir: str = "./figures"
    figure_max_mb: int = 10

    # Global request-body ceiling enforced by BodySizeLimitMiddleware. Uploads are
    # already streamed with a mid-stream cap, but every non-upload endpoint reads
    # the whole body into memory before validation — this backstops that against a
    # memory-exhaustion DoS regardless of which proxy fronts the app. Set just
    # above ``attachment_max_mb`` so real uploads still pass. 0 disables the cap.
    max_body_mb: int = 30

    # Dissemination (Milestone 3)
    portal_base_url: str = "http://localhost:8000"
    # Auto-disseminate reports at or below this TLP; RED / AMBER_STRICT are
    # withheld from broadcast by default (named sharing is out of scope).
    dissemination_max_tlp: str = "AMBER"
    # Email backend: "console" (logs + in-memory outbox, for dev/tests) or "smtp".
    email_backend: str = "console"
    email_from: str = "iceberg@example.com"
    smtp_host: str = "localhost"
    smtp_port: int = 25
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = False
    # Bounds SMTP connect/send so a stuck server can't hang the background task.
    smtp_timeout: float = 10.0
    webhook_url: str = ""
    webhook_token: str = ""
    webhook_timeout: float = 5.0
    # Payload envelope for publication webhooks. ``generic`` preserves the
    # original Iceberg JSON contract; ``slack`` and ``teams`` select their
    # respective incoming-webhook envelopes.
    webhook_format: str = "generic"

    # Durable external-work outbox.  HTTP requests only enqueue a row inside
    # their transaction; a worker claims it using a time-bounded lease.  These
    # deliberately small defaults suit a lightweight single-process worker but
    # remain safe when several workers race to claim the same database queue.
    jobs_lease_seconds: int = 120
    jobs_max_attempts: int = 5
    jobs_retry_base_seconds: int = 30
    jobs_worker_poll_seconds: float = 1.0

    # Rate limiting / abuse protection. ``None`` means "enabled in prod, off in
    # dev/test"; Redis is the production-grade shared store across uvicorn
    # workers, while memory is for local/dev/test isolation.
    rate_limit_enabled: bool | None = None
    rate_limit_store: str = "auto"  # auto | redis | memory
    rate_limit_redis_url: str = ""
    rate_limit_fail_open: bool = True
    rate_limit_auth_dev_login_per_minute: int = 5
    rate_limit_auth_oidc_per_minute: int = 20
    rate_limit_ai_per_hour: int = 60
    rate_limit_ai_burst: int = 10
    rate_limit_render_per_hour: int = 12
    rate_limit_render_burst: int = 3
    rate_limit_outbound_per_hour: int = 20
    rate_limit_outbound_burst: int = 5
    rate_limit_search_per_minute: int = 120
    rate_limit_search_burst: int = 60

    # Governed AI analyst assist. Off by default; every feature routes through
    # services/ai.py so advisory behavior, TLP egress and audit stay consistent.
    ai_backend: str = "none"  # none | openai-compatible | claude | bedrock
    ai_base_url: str = ""
    ai_api_key: str = ""  # Bearer key for openai-compatible / claude (env-only)
    ai_model: str = ""
    ai_aws_region: str = ""  # bedrock only; auth is the standard AWS credential chain
    ai_timeout: float = 20.0
    ai_max_tlp: str = "AMBER"
    ai_embeddings_enabled: bool = False
    ai_embedding_model: str = ""

    # Inbound collection — RSS feed ingestion (FR #50). The poller is opt-in
    # (off by default, so tests/dev never reach out to the network); fetches are
    # bounded and per-feed isolated. Feed URLs are admin-configured only, which is
    # the SSRF-containment boundary — ``rss_allow_private_hosts`` is the escape
    # hatch for genuinely-internal feeds. See services/feeds.py.
    rss_poll_enabled: bool = False
    rss_poll_interval_minutes: int = 30
    rss_fetch_timeout: float = 10.0
    rss_max_response_bytes: int = 2 * 1024 * 1024
    rss_max_items_per_feed: int = 100
    rss_allow_private_hosts: bool = False
    # Retention for fetched feed items. The per-fetch cap above bounds one poll;
    # this bounds accumulation across polls. Only un-ingested items are pruned —
    # anything captured into a notebook already became a durable Source. Age in
    # days; 0 = keep forever. Prune with ``iceberg-prune-audit`` (see #165).
    feed_item_retention_days: int = 90

    # Global outbound proxy connectivity. Routing config (mode/url/no-proxy) is
    # admin-editable on the ProxySettings DB row; these env values seed that row.
    # Proxy CREDENTIALS are a secret — read only from the environment, injected
    # into the proxy URL at call time, and never written to the DB. See
    # services/proxy.py. Modes: system (honour env proxy vars) | none (direct) |
    # explicit (use proxy_url, bypassing the no-proxy list).
    proxy_mode: str = "system"
    proxy_url: str = ""
    proxy_no_proxy: str = (
        "localhost,127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,"
        "192.168.0.0/16,169.254.0.0/16,::1"
    )
    proxy_username: str = ""
    proxy_password: str = ""

    # Outbound MISP push (light-touch IOC FR). Routing config (enabled/url/TLS +
    # event defaults) is admin-editable on the MISPSettings DB row; these env
    # values seed that row. The API KEY is a secret — read only from the
    # environment, sent as the Authorization header at call time, never written
    # to the DB. See services/misp.py.
    misp_enabled: bool = False
    misp_url: str = ""
    misp_api_key: str = ""
    misp_verify_tls: bool = True
    misp_timeout: float = 15.0
    # Cited IOCs above this TLP prompt the writer to confirm before the push
    # (MISP still receives them and honours the per-attribute TLP tag).
    misp_max_tlp: str = "AMBER"

    # Security audit logging → SIEM. Runtime routing config lives in the DB
    # (AuditSettings, admin-editable at /admin/audit); these env values are the
    # boot default and the secret. ``audit_enabled`` is the master kill switch
    # used until the DB settings row exists. The HTTP/HEC token is a secret and
    # is read only from the environment — never written to the DB.
    audit_enabled: bool = True
    audit_http_token: str = ""
    # Seed defaults for the initial AuditSettings row.
    audit_methods: str = "stdout"  # comma-separated: stdout,syslog,http
    audit_file_path: str = ""  # empty = stdout logger only
    audit_syslog_host: str = "localhost"
    audit_syslog_port: int = 514
    audit_syslog_protocol: str = "UDP"  # UDP | TCP
    audit_http_endpoint: str = ""
    # Retention for the local AuditEvent trail. The SIEM is the long-term store;
    # this table is the forensic buffer and needn't hold years of events (every
    # middleware-recorded 401/403 lands here, so it's the fastest-growing table on
    # a scanned public instance). Age in days; 0 = keep forever. Prune with
    # ``iceberg-prune-audit`` (see #165).
    audit_retention_days: int = 365

    @property
    def audit_default_methods(self) -> list[str]:
        return [m.strip().lower() for m in self.audit_methods.split(",") if m.strip()]

    @property
    def max_attachment_bytes(self) -> int:
        return self.attachment_max_mb * 1024 * 1024

    @property
    def max_figure_bytes(self) -> int:
        return self.figure_max_mb * 1024 * 1024

    @property
    def max_body_bytes(self) -> int:
        return self.max_body_mb * 1024 * 1024

    @property
    def allowed_attachment_types(self) -> frozenset[str]:
        return frozenset(
            t.strip().lower()
            for t in self.attachment_allowed_types.split(",")
            if t.strip()
        )

    @property
    def is_prod(self) -> bool:
        return self.environment.lower() in {"prod", "production"}

    @property
    def dev_login_enabled(self) -> bool:
        return self.dev_auth and not self.is_prod

    @property
    def rate_limit_active(self) -> bool:
        if self.rate_limit_enabled is None:
            return self.is_prod
        return self.rate_limit_enabled

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.lower().startswith("sqlite")

    @field_validator("ai_backend")
    @classmethod
    def _validate_ai_backend(cls, value: str) -> str:
        if value not in _AI_BACKENDS:
            raise ValueError(
                f"ICEBERG_AI_BACKEND must be one of {sorted(_AI_BACKENDS)}; got {value!r}."
            )
        return value

    @field_validator("environment")
    @classmethod
    def _validate_environment(cls, value: str) -> str:
        environment = (value or "").strip().lower()
        if environment == "production":
            environment = "prod"
        if environment not in _ENVIRONMENTS:
            raise ValueError(
                f"ICEBERG_ENVIRONMENT must be one of {sorted(_ENVIRONMENTS)}; got {value!r}."
            )
        return environment

    @field_validator("email_backend")
    @classmethod
    def _validate_email_backend(cls, value: str) -> str:
        backend = (value or "").strip().lower()
        if backend not in _EMAIL_BACKENDS:
            raise ValueError(
                f"ICEBERG_EMAIL_BACKEND must be one of {sorted(_EMAIL_BACKENDS)}; got {value!r}."
            )
        return backend

    @field_validator("ai_max_tlp", "dissemination_max_tlp", "misp_max_tlp")
    @classmethod
    def _validate_tlp_ceiling(cls, value: str) -> str:
        ceiling = (value or "").strip().upper().replace("+", "_STRICT")
        ceiling = ceiling.replace("-", "_")
        if ceiling not in _TLP_VALUES:
            raise ValueError(
                f"TLP ceilings must be one of {sorted(_TLP_VALUES)}; got {value!r}."
            )
        return ceiling

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        level = (value or "").upper()
        if level not in logging._nameToLevel:  # noqa: SLF001 - stdlib's canonical level map
            raise ValueError(f"ICEBERG_LOG_LEVEL must be a standard logging level; got {value!r}.")
        return level

    @field_validator("log_format")
    @classmethod
    def _validate_log_format(cls, value: str) -> str:
        fmt = (value or "").lower()
        if fmt not in _LOG_FORMATS:
            raise ValueError(
                f"ICEBERG_LOG_FORMAT must be one of {sorted(_LOG_FORMATS)}; got {value!r}."
            )
        return fmt

    @field_validator("rate_limit_store")
    @classmethod
    def _validate_rate_limit_store(cls, value: str) -> str:
        store = (value or "").lower()
        if store not in _RATE_LIMIT_STORES:
            raise ValueError(
                "ICEBERG_RATE_LIMIT_STORE must be one of "
                f"{sorted(_RATE_LIMIT_STORES)}; got {value!r}."
            )
        return store

    @field_validator(
        "rate_limit_auth_dev_login_per_minute",
        "rate_limit_auth_oidc_per_minute",
        "rate_limit_ai_per_hour",
        "rate_limit_ai_burst",
        "rate_limit_render_per_hour",
        "rate_limit_render_burst",
        "rate_limit_outbound_per_hour",
        "rate_limit_outbound_burst",
        "rate_limit_search_per_minute",
        "rate_limit_search_burst",
    )
    @classmethod
    def _validate_positive_rate_limit(cls, value: int) -> int:
        if value < 1:
            raise ValueError("ICEBERG_RATE_LIMIT_* values must be at least 1.")
        return value

    @field_validator("rss_max_response_bytes")
    @classmethod
    def _validate_rss_max_response_bytes(cls, value: int) -> int:
        if value < 1:
            raise ValueError("ICEBERG_RSS_MAX_RESPONSE_BYTES must be at least 1 byte.")
        return value

    @model_validator(mode="after")
    def _guard_production(self) -> "Settings":
        """Fail fast rather than boot an unsafe production instance."""
        if self.is_prod and (
            self.secret_key == _INSECURE_DEFAULT_SECRET or len(self.secret_key) < 32
        ):
            # The default signing key is public, so it would allow JWT forgery.
            raise ValueError(
                "ICEBERG_SECRET_KEY must be a unique value of at least 32 "
                "characters in production (the built-in default is public)."
            )
        if self.is_prod and self.is_sqlite:
            # SQLite is the dev/test default only — it is single-writer, has no
            # network/HA story, and the container path mounts a local file that
            # doesn't survive horizontal scaling. Production runs on PostgreSQL.
            raise ValueError(
                "ICEBERG_DATABASE_URL must be a PostgreSQL URL in production "
                "(postgresql+psycopg://…); SQLite is for local dev/test only."
            )
        forwarded_allow_ips = os.getenv(
            "FORWARDED_ALLOW_IPS", self.forwarded_allow_ips
        )
        if self.is_prod and "*" in {
            item.strip() for item in forwarded_allow_ips.split(",")
        }:
            raise ValueError(
                "FORWARDED_ALLOW_IPS cannot contain '*' in production; configure "
                "only the reverse-proxy addresses or CIDRs."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
