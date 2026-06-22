"""Admin-only publication-webhook console.

Mirrors ``/admin/misp`` (inline ``_require_admin`` guard, design-system template,
no JSON API). Secrets are not handled here — the bearer token lives in the
environment (``ICEBERG_WEBHOOK_TOKEN``); the form only edits the non-secret
config persisted on the ``WebhookSettings`` row.
"""

from typing import Annotated

from fastapi import BackgroundTasks, Form, Request

from ..auth.dependencies import CurrentUser
from ..config import get_settings
from ..models import AuditAction, AuditCategory, AuditSeverity
from ..services import audit, dissemination, proxy_settings, webhook_settings
from ..templating import templates
from .common import SessionDep, _redirect, _require_admin, router


@router.get("/admin/webhook")
def admin_webhook_view(request: Request, session: SessionDep, user: CurrentUser):
    _require_admin(user)
    return templates.TemplateResponse(
        request,
        "admin_webhook.html",
        {
            "user": user,
            "settings": webhook_settings.get(session),
            "token_configured": bool(get_settings().webhook_token),
            "test_result": request.query_params.get("test", ""),
        },
    )


@router.post("/admin/webhook")
def admin_webhook_save(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
    enabled: Annotated[bool, Form()] = False,
    url: Annotated[str, Form()] = "",
    timeout: Annotated[float, Form()] = 5.0,
):
    _require_admin(user)
    webhook_settings.update(
        session,
        enabled=enabled,
        url=url.strip(),
        timeout=timeout,
    )
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=AuditAction.WEBHOOK_SETTINGS_UPDATED,
        category=AuditCategory.ADMIN,
        severity=AuditSeverity.WARNING,
        actor=user,
        request=request,
        detail={"enabled": enabled},
    )
    return _redirect("/admin/webhook")


@router.post("/admin/webhook/test")
def admin_webhook_test(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
):
    """Probe the configured webhook with a test payload (best-effort; never raises)."""
    _require_admin(user)
    result = dissemination.test_webhook_connection(
        webhook_settings.get(session), proxy_settings.get(session)
    )
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=AuditAction.WEBHOOK_TEST,
        category=AuditCategory.ADMIN,
        actor=user,
        request=request,
        detail={"result": result},
    )
    return _redirect(f"/admin/webhook?test={result}")
