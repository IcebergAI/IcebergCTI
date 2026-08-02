"""Global request-body size ceiling.

File uploads (attachments/figures) are streamed with a mid-stream cap, but every
*non-upload* endpoint reads the whole request body into memory before validation
— neither uvicorn nor Starlette imposes a default limit, so a single
authenticated client could POST an arbitrarily large JSON/form body (report
autosave, preview, requirement text …) and exhaust process memory. This
app-level backstop rejects oversized requests with **413** so the guarantee
holds regardless of which proxy fronts the app (#169).

A pure ASGI middleware (not ``BaseHTTPMiddleware``) so it can enforce the cap on
the receive channel: the declared ``Content-Length`` is rejected up front, and a
chunked body with no/under-declared length is aborted the moment the streamed
bytes cross the cap — before the wrapped app finishes buffering them.

**Why the mid-stream abort is signalled out-of-band.** Raising an exception from
``receive`` is not enough to guarantee the 413. FastAPI wraps ``request.body()``
/ ``request.form()`` in a broad ``except Exception`` and turns anything raised
there into ``400 "There was an error parsing the body"`` — so the exception never
reaches this middleware, and the client is told its body was *malformed* when it
was in fact *too large* (#272). The guard therefore records the overflow in a
closure flag and, if the wrapped app responds anyway, **rewrites that response**
to the documented 413. The exception is still raised, so a framework that lets it
propagate short-circuits immediately rather than buffering further.
"""

from ..config import Settings, get_settings

# ASGI type aliases kept loose — Starlette doesn't export concrete ones.
Scope = dict
Message = dict


class _BodyTooLarge(Exception):
    """Internal signal: streamed body exceeded the cap mid-flight."""


def _declared_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


class BodySizeLimitMiddleware:
    """Reject requests whose body exceeds ``ICEBERG_MAX_BODY_MB`` with 413.

    ``max_body_mb <= 0`` disables the guard. Sits just inside
    ``SecurityHeadersMiddleware`` so the 413 still carries the security headers.
    """

    def __init__(self, app, *, settings: Settings | None = None) -> None:
        self.app = app
        self._max_bytes = (settings or get_settings()).max_body_bytes

    async def __call__(self, scope: Scope, receive, send) -> None:
        if scope["type"] != "http" or self._max_bytes <= 0:
            await self.app(scope, receive, send)
            return

        declared = _declared_length(scope)
        if declared is not None and declared > self._max_bytes:
            await self._reject(send)
            return

        seen = 0
        # Per-request state lives in this closure, never on ``self`` — one
        # middleware instance serves every concurrent request.
        overflowed = False
        started = False
        rejected = False

        async def receive_guarded() -> Message:
            nonlocal seen, overflowed
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self._max_bytes:
                    overflowed = True
                    raise _BodyTooLarge
            return message

        async def send_tracked(message: Message) -> None:
            nonlocal started, rejected
            if rejected:
                return  # 413 already sent; discard the app's own response
            if message["type"] == "http.response.start":
                if overflowed:
                    # The app swallowed our abort and is answering in its own
                    # words (FastAPI: 400 "error parsing the body"). Replace it —
                    # "too large" and "malformed" are different problems and the
                    # client can only act on the right one.
                    rejected = True
                    started = True
                    await self._reject(send)
                    return
                started = True
            await send(message)

        try:
            await self.app(scope, receive_guarded, send_tracked)
        except _BodyTooLarge:
            # Propagated cleanly (pure-ASGI / Starlette). Only safe to synthesise
            # a 413 if the app hasn't already begun responding.
            if not started:
                await self._reject(send)

    async def _reject(self, send) -> None:
        body = b'{"detail":"Request body too large"}'
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
