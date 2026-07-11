"""Runtime SIEM-emit configuration — the single ``AuditSettings`` row.

Holds only non-secret routing config (enable flag, chosen methods, endpoints,
min severity). The HTTP/HEC token stays in the environment and is read by
``services/siem.py`` at emit time, so it is never persisted to the DB.
"""

from sqlmodel import Session

from ..config import get_settings
from ..models import AuditSettings, utcnow
from .singleton import get_or_create


def get(session: Session) -> AuditSettings:
    """Return the settings row, seeding it from env defaults on first read."""
    def defaults() -> dict:
        cfg = get_settings()
        return {
            "enabled": cfg.audit_enabled,
            "methods": cfg.audit_default_methods or ["stdout"],
            "file_path": cfg.audit_file_path,
            "syslog_host": cfg.audit_syslog_host,
            "syslog_port": cfg.audit_syslog_port,
            "syslog_protocol": cfg.audit_syslog_protocol,
            "http_endpoint": cfg.audit_http_endpoint,
        }

    return get_or_create(session, AuditSettings, defaults)


def update(session: Session, **fields) -> AuditSettings:
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


def list_methods() -> list[str]:
    """The supported emit-method identifiers (for the admin form)."""
    return ["stdout", "syslog", "http"]
