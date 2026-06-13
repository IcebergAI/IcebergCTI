"""Auth routes: login page, Entra OIDC code flow, dev-login bypass, logout."""

from typing import Annotated

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from ..config import get_settings
from ..db import get_session
from ..models import Role
from ..services.users import upsert_user
from ..templating import templates
from .dependencies import COOKIE_NAME
from .tokens import create_access_token

router = APIRouter(prefix="/auth", tags=["auth"])

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
    return Role.STAKEHOLDER


@router.get("/callback")
async def entra_callback(
    request: Request, session: Annotated[Session, Depends(get_session)]
):
    settings = get_settings()
    if not settings.oidc_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "OIDC is not enabled")
    oauth = _get_oauth()
    try:
        token = await oauth.entra.authorize_access_token(request)
    except OAuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc))

    claims = token.get("userinfo") or {}
    user = upsert_user(
        session,
        sub=claims.get("sub"),
        email=claims.get("email") or claims.get("preferred_username", ""),
        display_name=claims.get("name") or claims.get("email", "User"),
        role=_role_from_claims(claims),
    )
    app_token = create_access_token(
        user_id=user.id, email=user.email, role=user.role.value, name=user.display_name
    )
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookie(response, app_token)
    return response


@router.post("/dev-login")
def dev_login(
    session: Annotated[Session, Depends(get_session)],
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
        user_id=user.id, email=user.email, role=user.role.value, name=user.display_name
    )
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookie(response, app_token)
    return response


@router.post("/logout")
def logout():
    # POST (not GET) so logout can't be triggered cross-site by a stray link or
    # prefetch; the nav posts a small same-origin form.
    response = RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(COOKIE_NAME)
    return response
