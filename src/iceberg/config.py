"""Application configuration loaded from environment / .env (ICEBERG_ prefix)."""

import logging
from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_AI_BACKENDS = {"none", "openai-compatible", "claude", "bedrock"}
_LOG_FORMATS = {"auto", "text", "json"}

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

    # Microsoft Entra ID / OIDC
    oidc_enabled: bool = False
    oidc_tenant_id: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = "http://localhost:8000/auth/callback"
    oidc_role_claim: str = "roles"
    oidc_department_claim: str = "department"
    oidc_title_claim: str = "jobTitle"
    oidc_company_claim: str = "companyName"
    oidc_office_claim: str = "officeLocation"

    # Typst rendering
    typst_bin: str = "typst"
    render_output_dir: str = "./rendered"
    cmarker_version: str = "0.1.1"
    typst_timeout: int = 60  # seconds; guards against a runaway compile
    render_retention_keep: int = 3
    render_retention_days: int = 90

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
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
