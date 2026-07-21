"""Request-body size ceiling (issue #169).

Guards ``BodySizeLimitMiddleware``: a declared Content-Length over the cap is
rejected before the body is read, an under-declared chunked body is aborted mid
-stream, requests under the cap pass, and a zero cap disables the guard.
"""

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
