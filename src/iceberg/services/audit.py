"""Security audit logging — the local trail + SIEM dispatch.

``record`` persists one :class:`AuditEvent` (the source of truth, surviving a
SIEM outage). ``record_and_emit`` additionally schedules SIEM emission on a
background task so the network I/O stays off the response path. Capture sites
(``auth/audit_middleware.py`` and the explicitly instrumented routes) call into
here so the OWASP event shape lives in exactly one place.

OWASP discipline: the ``detail`` dict is curated by callers and must never carry
secrets, tokens, passwords, JWTs or file bytes.
"""

import socket
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

from sqlmodel import Session

from ..models import (
    AuditAction,
    AuditCategory,
    AuditEvent,
    AuditOutcome,
    AuditSeverity,
    ReportStatus,
    User,
    utcnow,
)
from . import audit_settings, proxy_settings, siem

# Static deployment identity for the OWASP "Where" attributes — resolved once.
APP_NAME = "iceberg"
try:
    APP_VERSION = version("iceberg")
except PackageNotFoundError:  # pragma: no cover — only when not pip-installed
    APP_VERSION = "0.0.0"
HOSTNAME = socket.gethostname() or "-"
SERVICE = "iceberg/http"

# Maps the post-transition report status to the audit action it represents. A
# transition to DRAFT in this lifecycle is always a reviewer "send back".
_LIFECYCLE_ACTIONS = {
    ReportStatus.IN_REVIEW: AuditAction.REPORT_SUBMITTED,
    ReportStatus.APPROVED: AuditAction.REPORT_APPROVED,
    ReportStatus.DRAFT: AuditAction.REPORT_SENT_BACK,
    ReportStatus.PUBLISHED: AuditAction.REPORT_PUBLISHED,
}


def lifecycle_action(status: ReportStatus) -> AuditAction | None:
    return _LIFECYCLE_ACTIONS.get(ReportStatus(status))

if TYPE_CHECKING:  # avoid importing Starlette/FastAPI types at module load
    from fastapi import BackgroundTasks
    from starlette.requests import Request


def _iso_utc(dt: datetime) -> str:
    """ISO-8601 in UTC ("international format"). DB round-trips drop the tzinfo,
    so a naive value is treated as the UTC it was written in."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _describe(
    action: str,
    outcome: AuditOutcome,
    actor: User | None,
    resource_type: str,
    resource_id: str | int | None,
) -> str:
    """A human-readable one-line summary (OWASP "Description")."""
    who = actor.email if actor else "anonymous"
    target = f" on {resource_type} #{resource_id}" if resource_type else ""
    verb = "was denied" if outcome == AuditOutcome.FAILURE else "performed"
    return f"{who} {verb} {action}{target}"


def _request_fields(request: "Request | None") -> dict:
    if request is None:
        return {}
    client = request.client
    return {
        "source_ip": client.host if client else "",
        "user_agent": request.headers.get("user-agent", "")[:512],
        "request_method": request.method,
        "request_path": request.url.path,
    }


def record(
    session: Session,
    *,
    action: str,
    category: AuditCategory = AuditCategory.SYSTEM,
    severity: AuditSeverity = AuditSeverity.INFO,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    actor: User | None = None,
    request: "Request | None" = None,
    resource_type: str = "",
    resource_id: str | int | None = None,
    status_code: int | None = None,
    correlation_id: str = "",
    description: str = "",
    detail: dict | None = None,
    commit: bool = True,
) -> AuditEvent:
    """Persist a security-relevant event and return it."""
    if not correlation_id and request is not None:
        correlation_id = getattr(request.state, "correlation_id", "")
    if not description:
        description = _describe(action, outcome, actor, resource_type, resource_id)
    event = AuditEvent(
        action=str(action),
        category=category,
        severity=severity,
        outcome=outcome,
        description=description,
        actor_id=actor.id if actor else None,
        actor_email=actor.email if actor else "",
        actor_role=str(actor.role) if actor else "",
        resource_type=resource_type,
        resource_id="" if resource_id is None else str(resource_id),
        status_code=status_code,
        correlation_id=correlation_id,
        detail=detail or {},
        **_request_fields(request),
    )
    session.add(event)
    if commit:
        session.commit()
        session.refresh(event)
    return event


def to_owasp_dict(event: AuditEvent) -> dict:
    """Render an event as the structured-JSON payload sent to the SIEM.

    Carries the full OWASP Logging Cheat Sheet event attributes:
      - **when**  — event + log datetime (international format) + interaction id
      - **where** — application (name/version/host), service, entry point (URL/method)
      - **who**   — source address + user identity
      - **what**  — type, severity, security-relevance flag, description, result
    """
    return {
        # when
        "event_datetime": _iso_utc(event.occurred_at),
        "logged_datetime": _iso_utc(utcnow()),
        "interaction_id": event.correlation_id,
        # where
        "application": {"name": APP_NAME, "version": APP_VERSION, "host": HOSTNAME},
        "service": SERVICE,
        "request": {
            "method": event.request_method,
            "path": event.request_path,
            "status_code": event.status_code,
        },
        # who
        "source_ip": event.source_ip,
        "user_agent": event.user_agent,
        "actor": {
            "id": event.actor_id,
            "email": event.actor_email,
            "role": event.actor_role,
        },
        # what
        "action": event.action,
        "category": str(event.category),
        "severity": str(event.severity),
        "security_relevant": True,
        "outcome": str(event.outcome),
        "description": event.description,
        "resource": {"type": event.resource_type, "id": event.resource_id},
        "detail": event.detail,
    }


def schedule_emit(
    session: Session,
    event: AuditEvent,
    background_tasks: "BackgroundTasks | None" = None,
) -> None:
    """Dispatch ``event`` to the SIEM. Resolves settings + builds the payload
    *now* (while the session is live), then either schedules emission on the
    given background tasks or emits inline (best-effort)."""
    payload = to_owasp_dict(event)
    # Detach the settings from the session so the background task can read their
    # fields after the request's session is closed. The proxy snapshot is
    # resolved here too (live session) so siem stays off the DB in its task.
    snapshot = audit_settings.get(session).model_copy()
    proxy_snapshot = proxy_settings.get(session).model_copy()
    if background_tasks is not None:
        background_tasks.add_task(siem.emit, payload, snapshot, proxy_snapshot)
    else:
        siem.emit(payload, snapshot, proxy_snapshot)


def record_and_emit(
    session: Session,
    *,
    background_tasks: "BackgroundTasks | None" = None,
    **kwargs,
) -> AuditEvent:
    """Persist an event and dispatch it to the SIEM (convenience for routes)."""
    event = record(session, **kwargs)
    schedule_emit(session, event, background_tasks)
    return event
