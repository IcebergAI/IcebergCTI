"""Liveness/readiness probes: unauthenticated, cheap, and readiness reflects DB
connectivity (regression for FR #61)."""

from iceberg.db import get_session


def test_healthz_ok_without_auth(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_healthz_not_redirected_for_html_clients(client):
    """The auth-redirect handler only fires on 401, so a browser-style request
    still gets a plain 200, not a 303 to /auth/login."""
    resp = client.get(
        "/healthz", headers={"accept": "text/html"}, follow_redirects=False
    )
    assert resp.status_code == 200


def test_readyz_ready_with_db(client):
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


def test_readyz_503_when_db_unavailable(client):
    """Readiness reports 503 when the DB round-trip fails."""

    class _BrokenSession:
        def exec(self, *args, **kwargs):
            raise RuntimeError("db down")

    def _broken_session():
        yield _BrokenSession()

    client.app.dependency_overrides[get_session] = _broken_session
    try:
        resp = client.get("/readyz")
    finally:
        client.app.dependency_overrides.pop(get_session, None)
    assert resp.status_code == 503
    assert resp.json() == {"status": "not ready"}
