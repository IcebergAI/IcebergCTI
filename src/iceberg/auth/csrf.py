"""Same-origin CSRF defence for the cookie-authenticated portal.

The portal authenticates browsers with the ``iceberg_session`` cookie, which the
browser attaches automatically — the classic CSRF surface. ``SameSite=Lax`` on
that cookie is the first line of defence; this middleware is the second: for any
state-changing method it requires the request to be same-origin (``Origin`` /
``Referer`` matching the host) whenever the session cookie is present.

It deliberately does **not** touch:
- safe methods (GET/HEAD/OPTIONS),
- ``Authorization: Bearer`` API clients (token auth is not browser-CSRF-prone),
- anonymous requests with no session cookie (nothing to forge — they fall
  through to the normal 401).

This is stateless (no per-form tokens); per-form tokens can layer on later if a
stricter posture is wanted.
"""

from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .dependencies import COOKIE_NAME

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def _same_origin(request: Request) -> bool:
    """True if the request's Origin (or Referer fallback) matches its Host."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return False  # a cookie-authenticated writer with neither header
    host = (request.headers.get("host") or "").lower()
    return bool(host) and urlsplit(source).netloc.lower() == host


class SameOriginCSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method not in _SAFE_METHODS:
            has_session = COOKIE_NAME in request.cookies
            is_bearer = request.headers.get("authorization", "").lower().startswith(
                "bearer "
            )
            if has_session and not is_bearer and not _same_origin(request):
                return JSONResponse(
                    {"detail": "Cross-origin request blocked"}, status_code=403
                )
        return await call_next(request)
