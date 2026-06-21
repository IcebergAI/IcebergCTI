"""Admin-only outbound-proxy console (global connectivity option).

Mirrors ``/admin/audit`` (inline ``_require_admin`` guard, design-system template,
no JSON API). Secrets are not handled here — proxy credentials live in the
environment (``ICEBERG_PROXY_USERNAME`` / ``ICEBERG_PROXY_PASSWORD``); the form
only edits the non-secret routing config persisted on the ``ProxySettings`` row.
"""

import logging
from typing import Annotated

import httpx
from fastapi import BackgroundTasks, Form, Request

from ..auth.dependencies import CurrentUser
from ..config import get_settings
from ..models import AuditAction, AuditCategory, AuditSeverity, ProxyMode
from ..services import audit, proxy, proxy_settings
from ..templating import templates
from .common import SessionDep, _redirect, _require_admin, router

logger = logging.getLogger("iceberg.proxy")


@router.get("/admin/proxy")
def admin_proxy_view(request: Request, session: SessionDep, user: CurrentUser):
    _require_admin(user)
    return templates.TemplateResponse(
        request,
        "admin_proxy.html",
        {
            "user": user,
            "settings": proxy_settings.get(session),
            "modes": list(ProxyMode),
            "creds_configured": bool(get_settings().proxy_username),
            "test_result": request.query_params.get("test", ""),
        },
    )


@router.post("/admin/proxy")
def admin_proxy_save(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
    mode: Annotated[str, Form()] = ProxyMode.SYSTEM,
    proxy_url: Annotated[str, Form()] = "",
    no_proxy: Annotated[str, Form()] = "",
):
    _require_admin(user)
    proxy_settings.update(
        session,
        mode=ProxyMode(mode),
        proxy_url=proxy_url.strip(),
        no_proxy=no_proxy.strip(),
    )
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=AuditAction.PROXY_SETTINGS_UPDATED,
        category=AuditCategory.ADMIN,
        severity=AuditSeverity.WARNING,
        actor=user,
        request=request,
        detail={"mode": mode},
    )
    return _redirect("/admin/proxy")


@router.post("/admin/proxy/test")
def admin_proxy_test(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
    test_url: Annotated[str, Form()] = "",
):
    """Fetch an admin-supplied URL through the configured proxy and report the
    outcome (best-effort; never raises)."""
    _require_admin(user)
    settings = proxy_settings.get(session)
    result = "no-url"
    if test_url.strip():
        try:
            resp = httpx.get(
                test_url.strip(),
                timeout=10.0,
                follow_redirects=True,
                **proxy.resolve(settings, test_url.strip()),
            )
            result = f"ok: HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001 — surface the failure, don't 500
            logger.warning("proxy test failed: %s", exc)
            result = f"error: {exc}"
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=AuditAction.PROXY_TEST,
        category=AuditCategory.ADMIN,
        actor=user,
        request=request,
        detail={"result": result},
    )
    return _redirect(f"/admin/proxy?test={result}")
