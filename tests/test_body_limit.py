"""Request-body size ceiling (issue #169).

Guards ``BodySizeLimitMiddleware``: a declared Content-Length over the cap is
rejected before the body is read, an under-declared chunked body is aborted mid
-stream, requests under the cap pass, and a zero cap disables the guard.
"""

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from iceberg.auth.body_limit import BodySizeLimitMiddleware
from iceberg.config import Settings


async def _echo(request):
    body = await request.body()
    return PlainTextResponse(f"got {len(body)}")


def _client(max_body_mb: int) -> TestClient:
    app = Starlette(routes=[Route("/echo", _echo, methods=["POST"])])
    app.add_middleware(
        BodySizeLimitMiddleware, settings=Settings(max_body_mb=max_body_mb)
    )
    return TestClient(app)


def test_rejects_oversized_declared_content_length():
    client = _client(max_body_mb=1)
    resp = client.post("/echo", content=b"x" * (2 * 1024 * 1024))
    assert resp.status_code == 413
    assert resp.json()["detail"] == "Request body too large"


def test_allows_body_under_cap():
    client = _client(max_body_mb=1)
    resp = client.post("/echo", content=b"x" * 1024)
    assert resp.status_code == 200
    assert resp.text == "got 1024"


def test_aborts_oversized_chunked_body_without_content_length():
    client = _client(max_body_mb=1)

    def _stream():
        # No Content-Length (chunked): the streaming guard must trip on the bytes.
        for _ in range(4):
            yield b"x" * (512 * 1024)  # 2 MB total, over the 1 MB cap

    resp = client.post("/echo", content=_stream())
    assert resp.status_code == 413


def test_zero_cap_disables_the_guard():
    client = _client(max_body_mb=0)
    resp = client.post("/echo", content=b"x" * (5 * 1024 * 1024))
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Through the REAL FastAPI stack (#272)
# --------------------------------------------------------------------------- #
def _capped_client(engine, monkeypatch, *, max_body_mb: int = 1) -> TestClient:
    """The real ``create_app()`` stack with a small body cap.

    The bare-Starlette client above misses the one framework this middleware
    actually protects in production. The default 30 MB cap is impractical to
    exceed in a test, so the app is built with a 1 MB one — everything else
    (middleware order, security headers, FastAPI body parsing) is the real thing.
    """
    from iceberg import db
    from iceberg.config import get_settings
    from iceberg.db import get_session
    from iceberg.main import create_app
    from sqlmodel import Session

    monkeypatch.setattr(get_settings(), "max_body_mb", max_body_mb)

    def _get_session():
        with Session(engine) as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = _get_session
    client = TestClient(app)
    with client:
        monkeypatch.setattr(db, "engine", engine)
        client.headers["origin"] = "http://testserver"
        resp = client.post(
            "/auth/dev-login",
            data={"role": "ANALYST", "email": "analyst@example.com", "name": "T"},
        )
        assert resp.status_code == 200, resp.text
        yield client
    app.dependency_overrides.clear()


@pytest.fixture(name="capped")
def capped_fixture(engine, monkeypatch):
    yield from _capped_client(engine, monkeypatch)


def _oversized_chunks(total_mb: int = 2):
    """A chunked body with NO Content-Length, so only the streaming guard trips."""
    for _ in range(total_mb * 2):
        yield b"x" * (512 * 1024)


def test_streaming_overflow_is_413_not_400_through_fastapi(capped):
    """FastAPI wraps body parsing in a broad `except Exception`, so the mid-stream
    abort was converted into `400 "There was an error parsing the body"` — telling
    the client its body was malformed when it was oversized (#272)."""
    resp = capped.post(
        "/api/notebooks",
        content=_oversized_chunks(),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 413, resp.text
    assert resp.json()["detail"] == "Request body too large"
    assert "parsing the body" not in resp.text


def test_the_413_still_carries_the_security_headers(capped):
    """The stated reason this middleware sits just inside SecurityHeaders — a
    rewritten response must not escape the header stamping."""
    resp = capped.post(
        "/api/notebooks",
        content=_oversized_chunks(),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 413
    assert "content-security-policy" in resp.headers
    assert resp.headers.get("x-content-type-options") == "nosniff"


def test_declared_length_path_still_413_through_fastapi(capped):
    """The Content-Length path was already correct — keep it that way."""
    resp = capped.post(
        "/api/notebooks",
        content=b"x" * (2 * 1024 * 1024),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 413


def test_the_rewrite_is_inert_on_ordinary_responses(capped):
    """A legitimate 422 from body validation must NOT become a 413, and a normal
    request must be untouched."""
    ok = capped.post("/api/notebooks", json={"title": "Under the cap"})
    assert ok.status_code == 201, ok.text
    bad = capped.post("/api/notebooks", json={"nope": 1})
    assert bad.status_code == 422
