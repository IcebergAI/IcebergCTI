"""Publication-webhook configuration — the single ``WebhookSettings`` row.

Holds only non-secret config (enabled flag, endpoint URL, timeout). The bearer
token stays in the environment (``ICEBERG_WEBHOOK_TOKEN``) and is injected by
``services/dissemination.py`` at call time, so it is never persisted here.
Mirrors ``services/misp_settings.py`` / ``services/proxy_settings.py``.
"""

from sqlmodel import Session

from ..config import get_settings
from ..models import WebhookSettings, utcnow
from .singleton import get_or_create


DEFAULT_WEBHOOK_FORMAT = "generic"
WEBHOOK_FORMATS = (DEFAULT_WEBHOOK_FORMAT, "slack", "teams")


def normalise_format(value: str | None) -> str:
    """Return a safe, supported payload format.

    The database can contain a value written by an older/manual deployment, so
    delivery must fail closed to the stable generic contract rather than emit an
    accidental channel-specific shape. The admin form validates choices before
    calling this helper.
    """
    candidate = (value or "").strip().lower()
    return candidate if candidate in WEBHOOK_FORMATS else DEFAULT_WEBHOOK_FORMAT


def get(session: Session) -> WebhookSettings:
    """Return the settings row, seeding it from env defaults on first read.

    For backwards compatibility with env-only deployments, ``enabled`` seeds to
    true when ``ICEBERG_WEBHOOK_URL`` is set — so a deployment that only ever set
    the env var keeps firing the webhook after the row is introduced."""
    def defaults() -> dict:
        cfg = get_settings()
        return {
            "enabled": bool(cfg.webhook_url),
            "url": cfg.webhook_url,
            "timeout": cfg.webhook_timeout,
            "format": normalise_format(cfg.webhook_format),
        }

    return get_or_create(session, WebhookSettings, defaults)


def update(session: Session, **fields) -> WebhookSettings:
    """Patch the settings row with the given (validated) fields."""
    row = get(session)
    for key, value in fields.items():
        if value is not None and hasattr(row, key):
            if key == "format":
                value = normalise_format(value)
            setattr(row, key, value)
    row.updated_at = utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row
