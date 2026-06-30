"""Purpose-separated signing keys derived from the deployment secret."""

import hashlib
import hmac

from ..config import Settings, get_settings


def derive_signing_key(settings: Settings, purpose: str) -> str:
    """Derive a stable key for one signing context from ICEBERG_SECRET_KEY."""
    return hmac.new(
        settings.secret_key.encode("utf-8"),
        f"iceberg:{purpose}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def jwt_signing_key(settings: Settings | None = None) -> str:
    return derive_signing_key(settings or get_settings(), "jwt")


def session_signing_key(settings: Settings | None = None) -> str:
    return derive_signing_key(settings or get_settings(), "session")
