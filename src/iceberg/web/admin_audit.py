"""Admin-only audit-log console: SIEM emit configuration + the local trail.

Mirrors the ``/admin/tags`` pattern (inline ``_require_admin`` guard, design-system
template, no JSON API). Secrets are not handled here — the HTTP/HEC token lives in
the environment (``ICEBERG_AUDIT_HTTP_TOKEN``), so the form only edits non-secret
routing config persisted on the single ``AuditSettings`` row.
"""

from typing import Annotated

from fastapi import BackgroundTasks, Form, Request
from sqlmodel import col, select

from ..auth.dependencies import CurrentUser
from ..models import (
    AuditAction,
    AuditCategory,
    AuditEvent,
    AuditOutcome,
    AuditSeverity,
)
from ..services import audit, audit_settings
from ..templating import templates
from .common import SessionDep, _redirect, _require_admin, router

_EVENT_LIMIT = 200


@router.get("/admin/audit")
def admin_audit_view(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    action: str = "",
    severity: str = "",
    outcome: str = "",
    actor: str = "",
):
    _require_admin(user)
    stmt = select(AuditEvent).order_by(col(AuditEvent.occurred_at).desc())
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    if severity:
        stmt = stmt.where(AuditEvent.severity == severity)
    if outcome:
        stmt = stmt.where(AuditEvent.outcome == outcome)
    if actor:
        stmt = stmt.where(col(AuditEvent.actor_email).contains(actor))
    events = list(session.exec(stmt.limit(_EVENT_LIMIT)).all())
    return templates.TemplateResponse(
        request,
        "admin_audit.html",
        {
            "user": user,
            "settings": audit_settings.get(session),
            "events": events,
            "methods": audit_settings.list_methods(),
            "severities": list(AuditSeverity),
            "outcomes": list(AuditOutcome),
            "actions": list(AuditAction),
            "filters": {
                "action": action,
                "severity": severity,
                "outcome": outcome,
                "actor": actor,
            },
        },
    )


@router.post("/admin/audit/settings")
def admin_audit_settings(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
    enabled: Annotated[bool, Form()] = False,
    methods: Annotated[list[str], Form()] = [],  # noqa: B006 (FastAPI Form list)
    min_severity: Annotated[str, Form()] = AuditSeverity.INFO,
    file_path: Annotated[str, Form()] = "",
    syslog_host: Annotated[str, Form()] = "localhost",
    syslog_port: Annotated[int, Form()] = 514,
    syslog_protocol: Annotated[str, Form()] = "UDP",
    syslog_facility: Annotated[int, Form()] = 13,
    http_endpoint: Annotated[str, Form()] = "",
    http_verify_tls: Annotated[bool, Form()] = False,
):
    _require_admin(user)
    audit_settings.update(
        session,
        enabled=enabled,
        methods=[m for m in methods if m in audit_settings.list_methods()] or ["stdout"],
        min_severity=AuditSeverity(min_severity),
        file_path=file_path,
        syslog_host=syslog_host,
        syslog_port=syslog_port,
        syslog_protocol=syslog_protocol,
        syslog_facility=syslog_facility,
        http_endpoint=http_endpoint,
        http_verify_tls=http_verify_tls,
    )
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=AuditAction.AUDIT_SETTINGS_UPDATED,
        category=AuditCategory.ADMIN,
        severity=AuditSeverity.WARNING,
        actor=user,
        request=request,
    )
    return _redirect("/admin/audit")


@router.post("/admin/audit/test")
def admin_audit_test(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
):
    """Emit a synthetic event to verify SIEM connectivity end-to-end."""
    _require_admin(user)
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=AuditAction.AUDIT_TEST,
        category=AuditCategory.SYSTEM,
        actor=user,
        request=request,
        detail={"note": "manual SIEM connectivity test"},
    )
    return _redirect("/admin/audit")
