"""Best-effort actor lookup for unauthenticated request paths."""

import jwt
from sqlmodel import Session
from starlette.requests import Request

from ..models import User
from .dependencies import _extract_token
from .tokens import decode_access_token


def resolve_request_actor(request: Request, session: Session) -> User | None:
    """Resolve the request JWT to a user when possible, otherwise return None."""
    token = _extract_token(request)
    if not token:
        return None
    try:
        payload = decode_access_token(token)
        return session.get(User, int(payload["sub"]))
    except (jwt.PyJWTError, KeyError, TypeError, ValueError):
        return None
