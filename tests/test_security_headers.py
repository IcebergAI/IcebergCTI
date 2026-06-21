"""HTTP security response headers (CSP / HSTS / X-Frame-Options / etc.).

Unit-tests the pure header builder for dev vs prod, and an integration check that
the middleware stamps every response — including the CSRF 403 produced before any
route runs.
"""

from iceberg.auth.security_headers import build_csp, build_security_headers
from iceberg.config import Settings

# A 32+ char key so the prod model-validator (config._guard_production) accepts it.
_PROD_KEY = "x" * 40


def _dev_settings() -> Settings:
    return Settings(environment="dev", secret_key=_PROD_KEY)


def _prod_settings() -> Settings:
    return Settings(environment="prod", secret_key=_PROD_KEY)


def test_strict_script_src_has_no_unsafe():
    csp = build_csp(is_prod=False)
    assert "script-src 'self'" in csp
    assert "unsafe-inline" not in csp.split("style-src")[0]  # not in script-src
    assert "'unsafe-eval'" not in csp


def test_csp_core_directives_present():
    csp = build_csp(is_prod=False)
    for directive in (
        "default-src 'self'",
        "img-src 'self' data:",  # base64 figure embeds
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "style-src 'self' 'unsafe-inline'",  # documented carve-out
    ):
        assert directive in csp, f"missing CSP directive: {directive}"


def test_upgrade_insecure_requests_is_prod_only():
    assert "upgrade-insecure-requests" not in build_csp(is_prod=False)
    assert "upgrade-insecure-requests" in build_csp(is_prod=True)


def test_hsts_present_only_in_prod():
    assert "Strict-Transport-Security" not in build_security_headers(_dev_settings())
    prod = build_security_headers(_prod_settings())
    assert prod["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains"


def test_static_security_headers():
    h = build_security_headers(_dev_settings())
    assert h["X-Frame-Options"] == "DENY"
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert h["Cross-Origin-Opener-Policy"] == "same-origin"
    assert "camera=()" in h["Permissions-Policy"]
    assert "geolocation=()" in h["Permissions-Policy"]


def test_headers_present_on_normal_response(client):
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert "Content-Security-Policy" in resp.headers
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    # dev/test environment: no HSTS
    assert "Strict-Transport-Security" not in resp.headers


def test_headers_present_on_middleware_error_response(client, login):
    """A cross-origin state-changing request is blocked by the CSRF middleware
    (403) before reaching a route — the security headers must still be stamped."""
    login("ANALYST")
    resp = client.post(
        "/api/notebooks",
        json={"title": "x"},
        headers={"origin": "http://evil.example"},
    )
    assert resp.status_code == 403
    assert "Content-Security-Policy" in resp.headers
    assert resp.headers["X-Frame-Options"] == "DENY"


def test_per_download_nosniff_not_duplicated(client, login):
    """File downloads set their own nosniff; setdefault must not duplicate it."""
    resp = client.get("/auth/login")
    # Starlette MutableHeaders dedupes single-value sets; assert exactly one value.
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert len(resp.headers.get_list("X-Content-Type-Options")) == 1
