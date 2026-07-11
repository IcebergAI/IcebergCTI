"""Outbound MISP push configuration — the single ``MISPSettings`` row.

Holds only non-secret config (enabled flag, base URL, TLS verify, event
defaults). The MISP API key stays in the environment and is injected by
``services/misp.py`` at call time, so it is never persisted here. Mirrors
``services/proxy_settings.py`` / ``services/audit_settings.py``.
"""

from sqlmodel import Session

from ..config import get_settings
from ..models import MISPSettings, utcnow
from .singleton import get_or_create


def get(session: Session) -> MISPSettings:
    """Return the settings row, seeding it from env defaults on first read."""
    def defaults() -> dict:
        cfg = get_settings()
        return {
            "enabled": cfg.misp_enabled,
            "url": cfg.misp_url,
            "verify_tls": cfg.misp_verify_tls,
        }

    return get_or_create(session, MISPSettings, defaults)


def update(session: Session, **fields) -> MISPSettings:
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
