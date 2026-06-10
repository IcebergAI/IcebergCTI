"""Application configuration loaded from environment / .env (ICEBERG_ prefix)."""

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The default signing key shipped for local dev. It is public (it's in source
# control), so running with it in production would let anyone forge JWTs.
_INSECURE_DEFAULT_SECRET = "dev-insecure-change-me-0123456789abcdef"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="ICEBERG_", extra="ignore"
    )

    # Core
    app_name: str = "Iceberg"
    environment: str = "dev"
    secret_key: str = _INSECURE_DEFAULT_SECRET
    database_url: str = "sqlite:///./iceberg.db"

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
