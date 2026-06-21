"""Global HTTP security response headers.

Iceberg owns its security headers in the application (one place, version
controlled, testable, deployment-agnostic) rather than at a reverse proxy —
nginx ``add_header`` *appends* rather than overrides, so setting them in both
layers would emit duplicates. The app is also the only layer that knows its own
content surface, which the Content-Security-Policy has to match exactly.

The policy is **strict**: ``script-src 'self'`` with no ``'unsafe-inline'`` and
no ``'unsafe-eval'``. That is only possible because the portal carries no inline
JavaScript — Alpine runs from its CSP build (expressions are evaluated without
``eval``) and every component is registered in a same-origin ``/static`` script
(see ``static/js/tags.js`` / ``static/js/report_edit.js``). ``style-src`` keeps
``'unsafe-inline'`` deliberately: the templates use many dynamic ``style=``
attributes (e.g. progress-bar widths) and inline-style attributes cannot be
nonced; CSS injection is a far lower risk than script injection, so this is the
standard "strict CSP targets scripts" carve-out.

The lone transport-tied header, HSTS, is emitted only in production (the app
behind a TLS-terminating proxy sees ``http``, so it can't key off the request
scheme — ``settings.is_prod`` is the operator's "this instance is HTTPS"
assertion, mirroring the Secure-cookie gate).
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from ..config import Settings, get_settings

# Disable browser features the app never uses, so a future injection can't reach
# them either. Empty allowlist ``()`` = deny for all origins (incl. self).
_PERMISSIONS_POLICY = ", ".join(
    f"{feature}=()"
    for feature in (
        "accelerometer",
        "autoplay",
        "camera",
        "display-capture",
        "encrypted-media",
        "fullscreen",
        "geolocation",
        "gyroscope",
        "magnetometer",
        "microphone",
        "midi",
        "payment",
        "usb",
        "xr-spatial-tracking",
    )
)

# Content-Security-Policy directives, in declaration order. ``upgrade-insecure-requests``
# is appended only in production (omitted in dev so plain-http static assets load).
_CSP_DIRECTIVES: tuple[tuple[str, str], ...] = (
    ("default-src", "'self'"),
    ("script-src", "'self'"),  # no 'unsafe-inline' / 'unsafe-eval' — Alpine CSP build + external JS only
    ("style-src", "'self' 'unsafe-inline'"),  # dynamic inline style= attributes (see module docstring)
    ("img-src", "'self' data:"),  # base64 figure data: URIs (services/figures.py)
    ("font-src", "'self'"),  # self-hosted woff2
    ("connect-src", "'self'"),  # live-preview / autosave fetches
    ("object-src", "'none'"),
    ("base-uri", "'self'"),
    ("form-action", "'self'"),
    ("frame-ancestors", "'none'"),
)


def build_csp(*, is_prod: bool) -> str:
    parts = [f"{name} {value}" for name, value in _CSP_DIRECTIVES]
    if is_prod:
        parts.append("upgrade-insecure-requests")
    return "; ".join(parts)


def build_security_headers(settings: Settings) -> dict[str, str]:
    """The security headers applied to every response. Pure function of settings
    so it is unit-testable without a live app."""
    headers = {
        "Content-Security-Policy": build_csp(is_prod=settings.is_prod),
        # Legacy backstop for frame-ancestors 'none' (pre-CSP-Level-2 browsers).
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": _PERMISSIONS_POLICY,
        "Cross-Origin-Opener-Policy": "same-origin",
    }
    if settings.is_prod:
        headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return headers


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        # setdefault: never clobber a header a route set deliberately (e.g. the
        # per-download X-Content-Type-Options on file responses).
        for name, value in build_security_headers(get_settings()).items():
            response.headers.setdefault(name, value)
        return response
