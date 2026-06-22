"""Publication-webhook configuration — the single ``WebhookSettings`` row.

Holds only non-secret config (enabled flag, endpoint URL, timeout). The bearer
token stays in the environment (``ICEBERG_WEBHOOK_TOKEN``) and is injected by
``services/dissemination.py`` at call time, so it is never persisted here.
Mirrors ``services/misp_settings.py`` / ``services/proxy_settings.py``.
"""

from sqlmodel import Session

from ..config import get_settings
from ..models import WebhookSettings, utcnow

_SINGLETON_ID = 1


def get(session: Session) -> WebhookSettings:
    """Return the settings row, seeding it from env defaults on first read.

    For backwards compatibility with env-only deployments, ``enabled`` seeds to
    true when ``ICEBERG_WEBHOOK_URL`` is set — so a deployment that only ever set
    the env var keeps firing the webhook after the row is introduced."""
    row = session.get(WebhookSettings, _SINGLETON_ID)
    if row is None:
        cfg = get_settings()
        row = WebhookSettings(
            id=_SINGLETON_ID,
            enabled=bool(cfg.webhook_url),
            url=cfg.webhook_url,
            timeout=cfg.webhook_timeout,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def update(session: Session, **fields) -> WebhookSettings:
    """Patch the settings row with the given (validated) fields."""
    row = get(session)
    for key, value in fields.items():
        if value is not None and hasattr(row, key):
            setattr(row, key, value)
    row.updated_at = utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row
