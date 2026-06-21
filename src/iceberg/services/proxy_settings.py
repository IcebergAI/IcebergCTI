"""Global outbound-proxy configuration — the single ``ProxySettings`` row.

Holds only non-secret routing config (mode, proxy URL without credentials, the
no-proxy exclusion list). Proxy credentials stay in the environment and are
injected by ``services/proxy.py`` at call time, so they are never persisted here.
Mirrors ``services/audit_settings.py``.
"""

from sqlmodel import Session

from ..config import get_settings
from ..models import ProxyMode, ProxySettings, utcnow

_SINGLETON_ID = 1


def get(session: Session) -> ProxySettings:
    """Return the settings row, seeding it from env defaults on first read."""
    row = session.get(ProxySettings, _SINGLETON_ID)
    if row is None:
        cfg = get_settings()
        try:
            mode = ProxyMode(cfg.proxy_mode.upper())
        except ValueError:
            mode = ProxyMode.SYSTEM
        row = ProxySettings(
            id=_SINGLETON_ID,
            mode=mode,
            proxy_url=cfg.proxy_url,
            no_proxy=cfg.proxy_no_proxy,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def update(session: Session, **fields) -> ProxySettings:
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
