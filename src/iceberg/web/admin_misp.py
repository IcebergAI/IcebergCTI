"""Admin-only MISP push console (light-touch IOC FR).

Mirrors ``/admin/proxy`` (inline ``_require_admin`` guard, design-system template,
no JSON API). Secrets are not handled here — the MISP API key lives in the
environment (``ICEBERG_MISP_API_KEY``); the form only edits the non-secret
config persisted on the ``MISPSettings`` row.
"""

from typing import Annotated

from fastapi import BackgroundTasks, Form, Request

from ..auth.dependencies import CurrentUser
from ..config import get_settings
from ..models import AuditAction, AuditCategory, AuditSeverity
from ..services import audit, misp, misp_settings, proxy_settings
from ..templating import templates
from .common import SessionDep, _redirect, _require_admin, router


@router.get("/admin/misp")
def admin_misp_view(request: Request, session: SessionDep, user: CurrentUser):
    _require_admin(user)
    return templates.TemplateResponse(
        request,
        "admin_misp.html",
        {
            "user": user,
            "settings": misp_settings.get(session),
            "key_configured": bool(get_settings().misp_api_key),
            "test_result": request.query_params.get("test", ""),
        },
    )


@router.post("/admin/misp")
def admin_misp_save(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
    enabled: Annotated[bool, Form()] = False,
    url: Annotated[str, Form()] = "",
    verify_tls: Annotated[bool, Form()] = False,
    default_distribution: Annotated[int, Form()] = 0,
    default_threat_level: Annotated[int, Form()] = 4,
    default_published: Annotated[bool, Form()] = False,
):
    _require_admin(user)
    misp_settings.update(
        session,
        enabled=enabled,
        url=url.strip(),
        verify_tls=verify_tls,
        default_distribution=default_distribution,
        default_threat_level=default_threat_level,
        default_published=default_published,
    )
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=AuditAction.MISP_SETTINGS_UPDATED,
        category=AuditCategory.ADMIN,
        severity=AuditSeverity.WARNING,
        actor=user,
        request=request,
        detail={"enabled": enabled},
    )
    return _redirect("/admin/misp")


@router.post("/admin/misp/test")
def admin_misp_test(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
):
    """Probe the configured MISP instance (best-effort; never raises)."""
    _require_admin(user)
    result = misp.test_connection(
        misp_settings.get(session), proxy_settings.get(session)
    )
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=AuditAction.MISP_TEST,
        category=AuditCategory.ADMIN,
        actor=user,
        request=request,
        detail={"result": result},
    )
    return _redirect(f"/admin/misp?test={result}")
