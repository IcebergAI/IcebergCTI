"""Admin-only multi-provider OIDC console.

Mirrors ``/admin/misp`` (inline ``_require_admin`` guard, design-system template,
no JSON API). Secrets are not handled here — each provider's client secret lives
in the environment (``ICEBERG_OIDC_<PROVIDER>_CLIENT_SECRET``); the form only
edits the non-secret config persisted on the ``OIDCSettings`` row.
"""

from typing import Annotated

from fastapi import BackgroundTasks, Form, Request

from ..auth.routes import reset_oauth
from ..auth.dependencies import CurrentUser
from ..config import get_settings
from ..models import AuditAction, AuditCategory, AuditSeverity
from ..services import audit, oidc_settings
from ..templating import templates
from .common import SessionDep, _redirect, _require_admin, router


@router.get("/admin/oidc")
def admin_oidc_view(request: Request, session: SessionDep, user: CurrentUser):
    _require_admin(user)
    cfg = get_settings()
    return templates.TemplateResponse(
        request,
        "admin_oidc.html",
        {
            "user": user,
            "settings": oidc_settings.get(session),
            "secret_status": {
                name: bool(cfg.oidc_client_secret_for(name))
                for name in oidc_settings.PROVIDERS
            },
            "enabled_providers": [
                p.name for p in oidc_settings.enabled_providers(session)
            ],
        },
    )


@router.post("/admin/oidc")
def admin_oidc_save(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
    redirect_base_url: Annotated[str, Form()] = "",
    # Entra
    entra_enabled: Annotated[bool, Form()] = False,
    entra_client_id: Annotated[str, Form()] = "",
    entra_tenant_id: Annotated[str, Form()] = "",
    entra_scopes: Annotated[str, Form()] = "openid email profile",
    entra_role_claim: Annotated[str, Form()] = "roles",
    entra_role_map: Annotated[str, Form()] = "",
    # Authentik
    authentik_enabled: Annotated[bool, Form()] = False,
    authentik_client_id: Annotated[str, Form()] = "",
    authentik_base_url: Annotated[str, Form()] = "",
    authentik_app_slug: Annotated[str, Form()] = "",
    authentik_scopes: Annotated[str, Form()] = "openid email profile",
    authentik_role_claim: Annotated[str, Form()] = "groups",
    authentik_role_map: Annotated[str, Form()] = "",
    # Auth0
    auth0_enabled: Annotated[bool, Form()] = False,
    auth0_client_id: Annotated[str, Form()] = "",
    auth0_domain: Annotated[str, Form()] = "",
    auth0_scopes: Annotated[str, Form()] = "openid email profile",
    auth0_role_claim: Annotated[str, Form()] = "roles",
    auth0_role_map: Annotated[str, Form()] = "",
    # Okta
    okta_enabled: Annotated[bool, Form()] = False,
    okta_client_id: Annotated[str, Form()] = "",
    okta_domain: Annotated[str, Form()] = "",
    okta_auth_server: Annotated[str, Form()] = "default",
    okta_scopes: Annotated[str, Form()] = "openid email profile",
    okta_role_claim: Annotated[str, Form()] = "groups",
    okta_role_map: Annotated[str, Form()] = "",
):
    _require_admin(user)
    oidc_settings.update(
        session,
        redirect_base_url=redirect_base_url.strip(),
        entra_enabled=entra_enabled,
        entra_client_id=entra_client_id.strip(),
        entra_tenant_id=entra_tenant_id.strip(),
        entra_scopes=entra_scopes.strip() or "openid email profile",
        entra_role_claim=entra_role_claim.strip() or "roles",
        entra_role_map=entra_role_map.strip(),
        authentik_enabled=authentik_enabled,
        authentik_client_id=authentik_client_id.strip(),
        authentik_base_url=authentik_base_url.strip(),
        authentik_app_slug=authentik_app_slug.strip(),
        authentik_scopes=authentik_scopes.strip() or "openid email profile",
        authentik_role_claim=authentik_role_claim.strip() or "groups",
        authentik_role_map=authentik_role_map.strip(),
        auth0_enabled=auth0_enabled,
        auth0_client_id=auth0_client_id.strip(),
        auth0_domain=auth0_domain.strip(),
        auth0_scopes=auth0_scopes.strip() or "openid email profile",
        auth0_role_claim=auth0_role_claim.strip() or "roles",
        auth0_role_map=auth0_role_map.strip(),
        okta_enabled=okta_enabled,
        okta_client_id=okta_client_id.strip(),
        okta_domain=okta_domain.strip(),
        okta_auth_server=okta_auth_server.strip() or "default",
        okta_scopes=okta_scopes.strip() or "openid email profile",
        okta_role_claim=okta_role_claim.strip() or "groups",
        okta_role_map=okta_role_map.strip(),
    )
    # The registered Authlib clients are cached; drop them so the next login
    # rebuilds from the new config.
    reset_oauth()
    enabled = {
        "entra": entra_enabled,
        "authentik": authentik_enabled,
        "auth0": auth0_enabled,
        "okta": okta_enabled,
    }
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=AuditAction.OIDC_SETTINGS_UPDATED,
        category=AuditCategory.ADMIN,
        severity=AuditSeverity.WARNING,
        actor=user,
        request=request,
        detail={"enabled": [name for name, on in enabled.items() if on]},
    )
    return _redirect("/admin/oidc")
