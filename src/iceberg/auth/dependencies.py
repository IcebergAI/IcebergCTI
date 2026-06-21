"""FastAPI dependencies for authentication and role-based authorisation.

The token is accepted either as an ``Authorization: Bearer`` header (API
clients) or an ``iceberg_session`` cookie (portal). A missing/invalid token
raises 401; the app's exception handler turns that into a redirect to the login
page for browser (HTML) requests.
"""

from collections.abc import Callable
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlmodel import Session

from ..db import get_session
from ..models import Role, User
from .tokens import decode_access_token

COOKIE_NAME = "iceberg_session"


def _extract_token(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.cookies.get(COOKIE_NAME)


def get_optional_user(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
) -> User | None:
    token = _extract_token(request)
    if not token:
        return None
    try:
        payload = decode_access_token(token)
        user = session.get(User, int(payload["sub"]))
        if user is not None and int(payload.get("ver", 0)) != user.token_version:
            return None
    except (jwt.PyJWTError, KeyError, ValueError):
        # A malformed/expired/forged token (or a non-int subject) means
        # "anonymous"; anything else (e.g. a DB error) should propagate rather
        # than be silently downgraded to an unauthenticated request.
        return None
    return user


def get_current_user(
    user: Annotated[User | None, Depends(get_optional_user)],
) -> User:
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def user_in_roles(user: User, *roles: Role) -> bool:
    """True if the user holds one of ``roles``. ADMIN always qualifies."""
    return user.role == Role.ADMIN or user.role in roles


def ensure_role(user: User, *roles: Role, detail: str = "Insufficient role") -> User:
    """Imperative role guard (raises 403). Shared by the JSON API's
    :func:`require_role` dependency and the portal's inline checks so the rule
    lives in exactly one place."""
    if not user_in_roles(user, *roles):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=detail)
    return user


def require_role(*roles: Role) -> Callable[[User], User]:
    """Dependency factory: allow only the given roles (ADMIN always passes)."""

    def checker(user: CurrentUser) -> User:
        return ensure_role(user, *roles)

    return checker
