"""Auth routes: login page, Entra OIDC code flow, dev-login bypass, logout."""

import logging
from typing import Annotated

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from ..config import get_settings
from ..db import get_session
from ..models import AuditAction, AuditCategory, AuditOutcome, Role, User
from ..services import audit
from ..services.users import upsert_user
from ..templating import templates
from .dependencies import COOKIE_NAME, _extract_token
from .tokens import create_access_token, decode_access_token

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger("iceberg.auth")

# Lazily-configured OAuth client for Microsoft Entra ID.
_oauth: OAuth | None = None


def _get_oauth() -> OAuth:
    global _oauth
    if _oauth is None:
        settings = get_settings()
        oauth = OAuth()
        oauth.register(
            name="entra",
            server_metadata_url=(
                f"https://login.microsoftonline.com/"
                f"{settings.oidc_tenant_id}/v2.0/.well-known/openid-configuration"
            ),
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
            client_kwargs={"scope": "openid email profile"},
        )
        _oauth = oauth
    return _oauth


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
def login_page(request: Request):
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "oidc_enabled": settings.oidc_enabled,
            "dev_login_enabled": settings.dev_login_enabled,
            "roles": list(Role),
            "default_role": settings.dev_user_role,
            "default_email": settings.dev_user_email,
            "default_name": settings.dev_user_name,
        },
    )


@router.get("/entra/login")
async def entra_login(request: Request):
    settings = get_settings()
    if not settings.oidc_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "OIDC is not enabled")
    oauth = _get_oauth()
    return await oauth.entra.authorize_redirect(request, settings.oidc_redirect_uri)


def _role_from_claims(claims: dict) -> Role:
    raw = claims.get(get_settings().oidc_role_claim) or []
    if isinstance(raw, str):
        raw = [raw]
    valid = {r.value for r in Role}
    for entry in raw:
        token = str(entry).upper()
        if token in valid:
            return Role(token)
    # No recognised role claim -> read-only stakeholder by default.
    logger.warning(
        "No recognised OIDC role claim found in %s; defaulting to STAKEHOLDER",
        get_settings().oidc_role_claim,
    )
    return Role.STAKEHOLDER


@router.get("/callback")
async def entra_callback(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    background_tasks: BackgroundTasks,
):
    settings = get_settings()
    if not settings.oidc_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "OIDC is not enabled")
    oauth = _get_oauth()
    try:
        token = await oauth.entra.authorize_access_token(request)
    except OAuthError as exc:
        audit.record_and_emit(
            session,
            background_tasks=background_tasks,
            action=AuditAction.AUTH_LOGIN,
            category=AuditCategory.AUTHENTICATION,
            outcome=AuditOutcome.FAILURE,
            request=request,
            detail={"method": "oidc", "error": str(exc)},
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc))

    claims = token.get("userinfo") or {}
    email = claims.get("email") or claims.get("preferred_username", "")
    if not email:
        audit.record_and_emit(
            session,
            background_tasks=background_tasks,
            action=AuditAction.AUTH_LOGIN,
            category=AuditCategory.AUTHENTICATION,
            outcome=AuditOutcome.FAILURE,
            request=request,
            detail={"method": "oidc", "error": "missing_email_claim"},
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "OIDC email claim is required")

    user = upsert_user(
        session,
        sub=claims.get("sub"),
        email=email,
        display_name=claims.get("name") or claims.get("email", "User"),
        role=_role_from_claims(claims),
        department=claims.get(settings.oidc_department_claim, ""),
        job_title=claims.get(settings.oidc_title_claim, ""),
        company_name=claims.get(settings.oidc_company_claim, ""),
        office_location=claims.get(settings.oidc_office_claim, ""),
    )
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
        detail={"method": "oidc"},
    )
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookie(response, app_token)
    return response


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

    user = upsert_user(
        session, sub=None, email=email, display_name=name, role=role_enum
    )
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
    actor = _user_from_cookie(request, session)
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


def _user_from_cookie(request: Request, session: Session) -> User | None:
    """Best-effort actor resolution for logout (no auth dependency on the route)."""
    import jwt

    token = _extract_token(request)
    if not token:
        return None
    try:
        payload = decode_access_token(token)
        return session.get(User, int(payload["sub"]))
    except (jwt.PyJWTError, KeyError, ValueError):
        return None
