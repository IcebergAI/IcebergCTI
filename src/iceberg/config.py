"""Application configuration loaded from environment / .env (ICEBERG_ prefix)."""

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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

    # Typst rendering
    typst_bin: str = "typst"
    render_output_dir: str = "./rendered"
    cmarker_version: str = "0.1.1"
    typst_timeout: int = 60  # seconds; guards against a runaway compile

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

    # Inbound collection — RSS feed ingestion (FR #50). The poller is opt-in
    # (off by default, so tests/dev never reach out to the network); fetches are
    # bounded and per-feed isolated. Feed URLs are admin-configured only, which is
    # the SSRF-containment boundary — ``rss_allow_private_hosts`` is the escape
    # hatch for genuinely-internal feeds. See services/feeds.py.
    rss_poll_enabled: bool = False
    rss_poll_interval_minutes: int = 30
    rss_fetch_timeout: float = 10.0
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

    @model_validator(mode="after")
    def _guard_production(self) -> "Settings":
        """Fail fast rather than boot a production instance with an unsafe
        signing key — the default is public, so it would allow JWT forgery."""
        if self.is_prod and (
            self.secret_key == _INSECURE_DEFAULT_SECRET or len(self.secret_key) < 32
        ):
            raise ValueError(
                "ICEBERG_SECRET_KEY must be a unique value of at least 32 "
                "characters in production (the built-in default is public)."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
