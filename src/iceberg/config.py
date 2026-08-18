"""Application configuration loaded from environment / .env (ICEBERG_ prefix)."""

import logging
import os
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ``openai``/``gemini``/``ollama`` are first-class selectable providers that ride
# the OpenAI-compatible path; ``openai-compatible`` stays the generic escape hatch
# for a self-hosted gateway. Keep in sync with ``services/ai._BACKENDS``.
_AI_BACKENDS = {
    "none",
    "openai-compatible",
    "openai",
    "ollama",
    "gemini",
    "claude",
    "bedrock",
}
_LOG_FORMATS = {"auto", "text", "json"}
_RATE_LIMIT_STORES = {"auto", "redis", "memory"}
_ENVIRONMENTS = {"dev", "test", "prod"}
_EMAIL_BACKENDS = {"console", "smtp"}
# Related-product index providers. "local" is a deterministic, non-egress
# hash embedding; "none" disables the vector index entirely and leaves the
# lexical fallback serving related products (#311).
_RELATED_BACKENDS = {"local", "none"}
_STORAGE_BACKENDS = {"local", "s3"}
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
    # Prometheus-compatible operational metrics. Disabled unless deliberately
    # enabled; production requires a long bearer token because the main ingress
    # otherwise exposes every application path.
    metrics_enabled: bool = False
    metrics_token: str = ""

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

    # Persistent binary storage. ``local`` preserves the development and
    # single-node layout above. ``s3`` stores attachments, figures, and retained
    # rendered products under separate prefixes in one private S3-compatible
    # bucket, allowing every application replica to observe the same bytes.
    # Credentials remain environment-only and are never persisted or rendered.
    storage_backend: str = "local"  # local | s3
    storage_s3_bucket: str = ""
    storage_s3_prefix: str = "iceberg"
    storage_s3_region: str = ""
    storage_s3_endpoint_url: str = ""
    storage_s3_access_key_id: str = ""
    storage_s3_secret_access_key: str = ""
    storage_s3_session_token: str = ""
    storage_s3_force_path_style: bool = False
    storage_s3_verify_tls: bool = True
    storage_s3_ca_bundle: str = ""
    storage_s3_connect_timeout: float = 3.0
    storage_s3_read_timeout: float = 15.0
    # Reconciliation never removes an unreferenced object until this grace
    # window has elapsed. That protects an upload finalized by one replica while
    # its database transaction is still committing on another.
    storage_orphan_grace_seconds: int = 3600
    # Logical deletion is immediate; physical removal waits long enough for a
    # normal backup/snapshot window and is executed by the leased storage worker.
    storage_deletion_grace_seconds: int = 86400
    # Legacy rows may not have a trustworthy size/digest yet. Reads remain
    # bounded until the resumable migration verifies and stamps them.
    storage_legacy_read_max_mb: int = 100

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
    # These env values SEED the admin-editable AISettings DB row on first read;
    # the row is then the source of truth (edit at /admin/ai). See
    # services/ai_settings.py.
    ai_backend: str = "none"  # none|openai-compatible|openai|ollama|gemini|claude|bedrock
    ai_base_url: str = ""
    ai_api_key: str = ""  # Bearer key for openai(-compatible)/gemini/claude (env-only)
    ai_model: str = ""
    ai_aws_region: str = ""  # bedrock only; auth is the standard AWS credential chain
    ai_timeout: float = 20.0
    ai_max_tlp: str = "AMBER"
    ai_embeddings_enabled: bool = False
    ai_embedding_model: str = ""
    # Related-product index provider. Selecting "none" turns the vector index
    # off; retrieval then falls back to lexical overlap over the same
    # permission-filtered candidate set, so the feature never disappears.
    related_backend: str = "local"
    # Operator-approved Ollama base URL. The DB-editable AISettings.base_url for
    # the ``ollama`` provider must match this exact value — so a DB edit can't
    # redirect a real key to an attacker host (anti-SSRF; base-URL pinning).
    ai_ollama_base_url: str = "http://localhost:11434/v1"
    # Operator-approved base URL for the generic ``openai-compatible`` escape
    # hatch (self-hosted vLLM/LiteLLM/…). Same trust anchor as the Ollama pin:
    # the DB value must match this exactly, and while this is unset the backend
    # is refused outright. Without it a DB edit could ship ICEBERG_AI_API_KEY
    # plus report content up to the TLP ceiling to any host — or turn every
    # assist into an authenticated request to a link-local address (#270).
    ai_openai_compatible_base_url: str = ""

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
    def max_storage_legacy_bytes(self) -> int:
        return self.storage_legacy_read_max_mb * 1024 * 1024

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

    @field_validator("related_backend")
    @classmethod
    def _validate_related_backend(cls, value: str) -> str:
        backend = (value or "").strip().lower()
        if backend not in _RELATED_BACKENDS:
            raise ValueError(
                f"ICEBERG_RELATED_BACKEND must be one of {sorted(_RELATED_BACKENDS)}; "
                f"got {value!r}."
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

    @field_validator("storage_backend")
    @classmethod
    def _validate_storage_backend(cls, value: str) -> str:
        backend = (value or "").strip().lower()
        if backend not in _STORAGE_BACKENDS:
            raise ValueError(
                "ICEBERG_STORAGE_BACKEND must be one of "
                f"{sorted(_STORAGE_BACKENDS)}; got {value!r}."
            )
        return backend

    @field_validator("storage_s3_prefix")
    @classmethod
    def _validate_storage_prefix(cls, value: str) -> str:
        prefix = (value or "").strip().strip("/")
        if ".." in prefix.split("/"):
            raise ValueError("ICEBERG_STORAGE_S3_PREFIX cannot contain '..'.")
        return prefix

    @field_validator("storage_s3_endpoint_url")
    @classmethod
    def _validate_storage_endpoint(cls, value: str) -> str:
        endpoint = (value or "").strip().rstrip("/")
        if not endpoint:
            return ""
        parts = urlsplit(endpoint)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or (parts.path and parts.path != "/")
        ):
            raise ValueError(
                "ICEBERG_STORAGE_S3_ENDPOINT_URL must be an http(s) origin "
                "without credentials, path, query, or fragment."
            )
        return endpoint

    @field_validator("storage_orphan_grace_seconds", "storage_deletion_grace_seconds")
    @classmethod
    def _validate_storage_grace(cls, value: int) -> int:
        if value < 0:
            raise ValueError(
                "ICEBERG_STORAGE_*_GRACE_SECONDS cannot be negative."
            )
        return value

    @field_validator("storage_legacy_read_max_mb")
    @classmethod
    def _validate_storage_legacy_limit(cls, value: int) -> int:
        if value < 1:
            raise ValueError("ICEBERG_STORAGE_LEGACY_READ_MAX_MB must be at least 1.")
        return value

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
        """Fail fast rather than boot an unsafe production instance.

        The checks themselves live in :func:`production_guard_errors` so the
        read-only ``/admin/config`` validation panel can list them **all**,
        non-fatally, from the same source. They were previously duplicated by
        hand, so a fourth guard would silently not appear on that page and the
        admin hub would report "0 issues" for a config that refuses to boot
        (#282).
        """
        errors = production_guard_errors(self)
        if errors:
            raise ValueError(" ".join(errors))
        return self


def production_guard_errors(settings: "Settings") -> list[str]:
    """Every production boot-guard violation, as human-readable messages.

    The single source for both consumers: ``Settings._guard_production`` raises
    on any of these (fail fast rather than boot an unsafe instance), and
    ``services/effective_config._validation`` lists them non-fatally on
    ``/admin/config`` so an operator can see every problem at once. Keeping one
    implementation means a new guard cannot appear in one place and silently not
    the other (#282) — add a check here and both surfaces gain it.

    Empty outside production: every guard here is prod-only by design.
    """
    if not settings.is_prod:
        return []
    errors: list[str] = []
    if (
        settings.secret_key == _INSECURE_DEFAULT_SECRET
        or len(settings.secret_key) < 32
    ):
        # The default signing key is public, so it would allow JWT forgery.
        errors.append(
            "ICEBERG_SECRET_KEY must be a unique value of at least 32 "
            "characters in production (the built-in default is public)."
        )
    if settings.is_sqlite:
        # SQLite is the dev/test default only — it is single-writer, has no
        # network/HA story, and the container path mounts a local file that
        # doesn't survive horizontal scaling. Production runs on PostgreSQL.
        errors.append(
            "ICEBERG_DATABASE_URL must be a PostgreSQL URL in production "
            "(postgresql+psycopg://…); SQLite is for local dev/test only."
        )
    if settings.storage_backend == "s3" and not settings.storage_s3_bucket.strip():
        errors.append(
            "ICEBERG_STORAGE_S3_BUCKET is required when "
            "ICEBERG_STORAGE_BACKEND=s3."
        )
    if settings.storage_backend == "s3" and settings.storage_s3_endpoint_url:
        if not (
            settings.storage_s3_access_key_id
            and settings.storage_s3_secret_access_key
        ):
            errors.append(
                "A custom S3 endpoint requires dedicated "
                "ICEBERG_STORAGE_S3_ACCESS_KEY_ID and "
                "ICEBERG_STORAGE_S3_SECRET_ACCESS_KEY credentials; ambient AWS "
                "credentials are not sent to operator-defined endpoints."
            )
        if not settings.storage_s3_endpoint_url.startswith("https://"):
            errors.append(
                "ICEBERG_STORAGE_S3_ENDPOINT_URL must use HTTPS in production."
            )
    if settings.storage_backend == "s3" and not settings.storage_s3_verify_tls:
        errors.append(
            "ICEBERG_STORAGE_S3_VERIFY_TLS cannot be disabled in production; "
            "configure ICEBERG_STORAGE_S3_CA_BUNDLE for a private CA."
        )
    if settings.metrics_enabled and len(settings.metrics_token) < 32:
        errors.append(
            "ICEBERG_METRICS_TOKEN must be a unique value of at least 32 "
            "characters when metrics are enabled in production."
        )
    # The guard and uvicorn both read the UNPREFIXED env var.
    forwarded_allow_ips = os.getenv(
        "FORWARDED_ALLOW_IPS", settings.forwarded_allow_ips
    )
    if "*" in {item.strip() for item in forwarded_allow_ips.split(",")}:
        # Client IPs key rate limits and audit data, so wildcard proxy trust
        # lets any client forge them.
        errors.append(
            "FORWARDED_ALLOW_IPS cannot contain '*' in production; configure "
            "only the reverse-proxy addresses or CIDRs."
        )
    return errors


@lru_cache
def get_settings() -> Settings:
    return Settings()
