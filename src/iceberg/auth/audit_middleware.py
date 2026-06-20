"""Central capture of authorization-failure audit events.

Registered **outermost** (see ``main.py``) so it observes the final response of
every request — including the 403 returned by :class:`SameOriginCSRFMiddleware`
and 403s raised by role guards deep in the app. It records the scattered
*failure* outcomes (``AUTHZ_DENIED`` / ``CSRF_BLOCKED``) uniformly; successful
security actions are instrumented explicitly at their event sites instead, so
there is no double-logging.

Every request is stamped with a ``correlation_id`` (on ``request.state``) so an
explicitly-recorded success and a middleware-recorded failure from the same
request can be tied together in the SIEM.
"""

import uuid

import jwt
from sqlmodel import Session
from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from .. import db
from ..models import AuditAction, AuditCategory, AuditOutcome, AuditSeverity, User
from ..services import audit, audit_settings, siem
from .csrf import _SAFE_METHODS, _same_origin
from .dependencies import COOKIE_NAME, _extract_token
from .tokens import decode_access_token


def _looks_like_csrf_block(request: Request) -> bool:
    """Replicate the CSRF middleware's block condition so a 403 can be classed
    as a cross-origin block rather than a role denial."""
    if request.method in _SAFE_METHODS:
        return False
    has_session = COOKIE_NAME in request.cookies
    is_bearer = request.headers.get("authorization", "").lower().startswith("bearer ")
    return has_session and not is_bearer and not _same_origin(request)


def _resolve_actor(request: Request, session: Session) -> User | None:
    token = _extract_token(request)
    if not token:
        return None
    try:
        payload = decode_access_token(token)
        return session.get(User, int(payload["sub"]))
    except (jwt.PyJWTError, KeyError, ValueError):
        return None


class AuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.correlation_id = uuid.uuid4().hex
        response = await call_next(request)

        if response.status_code in (401, 403):
            self._record_denial(request, response)
        return response

    def _record_denial(self, request: Request, response) -> None:
        is_csrf = response.status_code == 403 and _looks_like_csrf_block(request)
        action = AuditAction.CSRF_BLOCKED if is_csrf else AuditAction.AUTHZ_DENIED
        with Session(db.engine) as session:
            actor = _resolve_actor(request, session)
            event = audit.record(
                session,
                action=action,
                category=AuditCategory.AUTHORIZATION,
                severity=AuditSeverity.WARNING,
                outcome=AuditOutcome.FAILURE,
                actor=actor,
                request=request,
                status_code=response.status_code,
                correlation_id=getattr(request.state, "correlation_id", ""),
            )
            payload = audit.to_owasp_dict(event)
            snapshot = audit_settings.get(session).model_copy()
        # Emit off the response path; never block the (already-formed) response.
        response.background = BackgroundTask(siem.emit, payload, snapshot)
