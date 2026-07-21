"""Auth routes: login page, multi-provider OIDC code flow, dev-login, logout.

OIDC is generic across providers (Entra / Authentik / Auth0 / Okta): one Authlib
client is registered per enabled provider from the ``OIDCSettings`` row, and a
single parametrised route pair (``/auth/oidc/{provider}/login`` +
``/auth/oidc/{provider}/callback``) drives them all. The legacy ``/auth/entra/login``
and ``/auth/callback`` paths remain as Entra back-compat aliases (they use the
legacy ``ICEBERG_OIDC_REDIRECT_URI`` so an existing Entra app registration keeps
working). Identity extraction is delegated to the per-provider adapter.
"""

import logging
from datetime import datetime
from typing import Annotated

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from ..config import get_settings
from ..db import get_session
from ..models import AuditAction, AuditCategory, AuditOutcome, Role
from ..services import audit
from ..services import oidc_settings as oidc_settings_service
from ..services.users import OIDCIdentityError, upsert_user
from ..templating import templates
from .dependencies import COOKIE_NAME
from .oidc import get_adapter
from .request_actor import resolve_request_actor
from .tokens import create_access_token

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger("iceberg.auth")

# Human-readable button labels for the login page.
_PROVIDER_LABELS = {
    "entra": "Microsoft Entra ID",
    "authentik": "Authentik",
    "auth0": "Auth0",
    "okta": "Okta",
}

# Authlib OAuth registry, built lazily from the enabled providers. The cache is
# **versioned on ``OIDCSettings.updated_at``** so every uvicorn worker rebuilds
# when the admin config changes — not just the worker that handled the POST (a
# process-global reset only clears one worker; the DB timestamp is shared).
_oauth: OAuth | None = None
_oauth_version: datetime | None = None


def _build_oauth(session: Session) -> OAuth:
    oauth = OAuth()
    for provider in oidc_settings_service.enabled_providers(session):
        oauth.register(
            name=provider.name,
            server_metadata_url=provider.metadata_url,
            client_id=provider.client_id,
            client_secret=provider.client_secret,
            client_kwargs={
                "scope": provider.scopes,
                "code_challenge_method": "S256",
            },
        )
    return oauth


def _get_oauth(session: Session) -> OAuth:
    global _oauth, _oauth_version
    version = oidc_settings_service.get(session).updated_at
    if _oauth is None or _oauth_version != version:
        _oauth = _build_oauth(session)
        _oauth_version = version
    return _oauth


def reset_oauth() -> None:
    """Drop this worker's cached OAuth registry (immediate local effect; other
    workers self-heal via the ``updated_at`` version check in ``_get_oauth``)."""
    global _oauth, _oauth_version
    _oauth = None
    _oauth_version = None


def _client(session: Session, provider: str):
    """The registered Authlib client for a provider (rebuilding once if stale)."""
    oauth = _get_oauth(session)
    client = oauth.create_client(provider)
    if client is None:
        reset_oauth()
        client = _get_oauth(session).create_client(provider)
    return client


def _set_session_cookie(response: RedirectResponse, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.is_prod,
        max_age=settings.jwt_expire_minutes * 60,
    )


@router.get("/login")
def login_page(request: Request, session: Annotated[Session, Depends(get_session)]):
    settings = get_settings()
    providers = [
        {"name": p.name, "label": _PROVIDER_LABELS.get(p.name, p.name.title())}
        for p in oidc_settings_service.enabled_providers(session)
    ]
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "oidc_providers": providers,
            "oidc_enabled": bool(providers),
            "dev_login_enabled": settings.dev_login_enabled,
            "roles": list(Role),
            "default_role": settings.dev_user_role,
            "default_email": settings.dev_user_email,
            "default_name": settings.dev_user_name,
        },
    )


def _audit_login_failure(
    session: Session, background_tasks: BackgroundTasks, request: Request, *, error: str
) -> None:
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=AuditAction.AUTH_LOGIN,
        category=AuditCategory.AUTHENTICATION,
        outcome=AuditOutcome.FAILURE,
        request=request,
        detail={"method": "oidc", "error": error},
    )


async def _start_login(
    provider: str, request: Request, session: Session, *, redirect_uri: str
):
    if provider not in {p.name for p in oidc_settings_service.enabled_providers(session)}:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "OIDC provider is not enabled")
    client = _client(session, provider)
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "OIDC provider is not enabled")
    return await client.authorize_redirect(request, redirect_uri)


async def _finish_login(
    provider: str,
    request: Request,
    session: Session,
    background_tasks: BackgroundTasks,
):
    configs = {p.name: p for p in oidc_settings_service.enabled_providers(session)}
    provider_config = configs.get(provider)
    adapter = get_adapter(provider)
    if provider_config is None or adapter is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "OIDC provider is not enabled")

    client = _client(session, provider)
    try:
        token = await client.authorize_access_token(request)
    except OAuthError as exc:
        _audit_login_failure(session, background_tasks, request, error=str(exc))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc))

    claims = token.get("userinfo") or {}
    identity = adapter.identity(provider_config, claims)

    if not identity.issuer or not identity.subject:
        _audit_login_failure(session, background_tasks, request, error="missing_identity_claim")
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "OIDC issuer and subject claims are required"
        )
    if not identity.email:
        _audit_login_failure(session, background_tasks, request, error="missing_email_claim")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "OIDC email claim is required")
    # Verified-email gate: deny JIT provisioning on an explicitly unverified email.
    if not identity.email_verified:
        _audit_login_failure(session, background_tasks, request, error="unverified_email")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "OIDC email is not verified")

    try:
        user = upsert_user(
            session,
            auth_provider=provider,
            issuer=identity.issuer,
            sub=identity.subject,
            email=identity.email,
            display_name=identity.display_name,
            role=identity.role,
            department=identity.department,
            job_title=identity.job_title,
            company_name=identity.company_name,
            office_location=identity.office_location,
        )
    except OIDCIdentityError as exc:
        _audit_login_failure(session, background_tasks, request, error=exc.reason)
        # Do not reveal whether the collision is an account, email, or subject.
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "OIDC identity cannot be provisioned"
        ) from exc

    app_token = create_access_token(
        user_id=user.id,
        email=user.email,
        role=user.role.value,
        name=user.display_name,
        token_version=user.token_version,
    )
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=AuditAction.AUTH_LOGIN,
        category=AuditCategory.AUTHENTICATION,
        actor=user,
        request=request,
        detail={"method": "oidc", "provider": provider},
    )
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookie(response, app_token)
    return response


@router.get("/oidc/{provider}/login")
async def oidc_login(
    provider: str,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
):
    redirect_uri = oidc_settings_service.redirect_uri(session, provider)
    return await _start_login(provider, request, session, redirect_uri=redirect_uri)


@router.get("/oidc/{provider}/callback")
async def oidc_callback(
    provider: str,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    background_tasks: BackgroundTasks,
):
    return await _finish_login(provider, request, session, background_tasks)


# -- Legacy Entra aliases (back-compat with existing app registrations) ------ #
@router.get("/entra/login")
async def entra_login(
    request: Request, session: Annotated[Session, Depends(get_session)]
):
    return await _start_login(
        "entra", request, session, redirect_uri=get_settings().oidc_redirect_uri
    )


@router.get("/callback")
async def entra_callback(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    background_tasks: BackgroundTasks,
):
    return await _finish_login("entra", request, session, background_tasks)


@router.post("/dev-login")
def dev_login(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    background_tasks: BackgroundTasks,
    email: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "",
):
    settings = get_settings()
    if not settings.dev_login_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dev login is disabled")

    email = email or settings.dev_user_email
    name = name or settings.dev_user_name
    try:
        role_enum = Role(role) if role else Role(settings.dev_user_role)
    except ValueError:
        role_enum = Role.ANALYST

    try:
        user = upsert_user(
            session, sub=None, email=email, display_name=name, role=role_enum
        )
    except OIDCIdentityError as exc:
        audit.record_and_emit(
            session,
            background_tasks=background_tasks,
            action=AuditAction.AUTH_LOGIN,
            category=AuditCategory.AUTHENTICATION,
            outcome=AuditOutcome.FAILURE,
            request=request,
            detail={"method": "dev", "error": exc.reason},
        )
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Email is bound to an external identity"
        ) from exc
    app_token = create_access_token(
        user_id=user.id,
        email=user.email,
        role=user.role.value,
        name=user.display_name,
        token_version=user.token_version,
    )
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=AuditAction.AUTH_LOGIN,
        category=AuditCategory.AUTHENTICATION,
        actor=user,
        request=request,
        detail={"method": "dev"},
    )
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookie(response, app_token)
    return response


@router.post("/logout")
def logout(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    background_tasks: BackgroundTasks,
):
    # POST (not GET) so logout can't be triggered cross-site by a stray link or
    # prefetch; the nav posts a small same-origin form.
    actor = resolve_request_actor(request, session)
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=AuditAction.AUTH_LOGOUT,
        category=AuditCategory.AUTHENTICATION,
        actor=actor,
        request=request,
    )
    if actor is not None:
        actor.token_version += 1
        session.add(actor)
        session.commit()
    response = RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(COOKIE_NAME)
    return response
