"""FastAPI application factory: mounts the JSON API and the server-rendered
portal in a single ASGI app, wires session middleware, static files and an auth
redirect for browser requests."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from .api import api_router
from .auth.audit_middleware import AuditMiddleware
from .auth.csrf import SameOriginCSRFMiddleware
from .auth.security_headers import SecurityHeadersMiddleware
from .auth.routes import router as auth_router
from .config import get_settings
from .db import engine, init_db
from .health import router as health_router
from .web import web_router

logger = logging.getLogger("iceberg.feeds")
auth_logger = logging.getLogger("iceberg.auth")


def _warn_if_no_login_path(settings) -> None:
    """Warn (don't crash) when a prod instance has no usable login path — dev
    auth is hard-disabled in prod and OIDC is unset, so /auth/login is a dead
    end. Surfacing it in pod logs makes the lockout obvious instead of silent.
    A safe-but-incomplete state isn't a boot-fail (cf. config._guard_production,
    which only rejects genuinely-unsafe forgeable-key / SQLite states)."""
    if settings.is_prod and not settings.dev_login_enabled and not settings.oidc_enabled:
        auth_logger.warning(
            "No usable login path: ICEBERG_ENVIRONMENT=prod disables the dev-login "
            "bypass and OIDC is not enabled (ICEBERG_OIDC_ENABLED). Configure Entra "
            "OIDC, or apply the evaluation overlay (deploy/k8s/configmap.beta.yaml). "
            "See deploy/k8s/README.md > Authentication / Login."
        )


async def _rss_poll_loop(interval_seconds: float) -> None:
    """Periodically fetch all enabled RSS feeds. Opt-in (off by default) so the
    test suite and a default dev boot never reach out to the network. The sync
    fetch (httpx + DB) runs in a worker thread so it never blocks the event loop;
    each cycle is wrapped so a transient failure never kills the poller."""
    from .services import feeds as feeds_service

    def _cycle() -> int:
        with Session(engine) as session:
            return feeds_service.fetch_all_enabled(session)

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            new_items = await anyio.to_thread.run_sync(_cycle)
            logger.info("RSS poll cycle complete: %d new item(s)", new_items)
        except Exception:  # noqa: BLE001 — the poller must survive a bad cycle
            logger.exception("RSS poll cycle failed")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    settings = get_settings()
    _warn_if_no_login_path(settings)
    poller: asyncio.Task | None = None
    if settings.rss_poll_enabled and settings.rss_poll_interval_minutes > 0:
        poller = asyncio.create_task(
            _rss_poll_loop(settings.rss_poll_interval_minutes * 60)
        )
    try:
        yield
    finally:
        if poller is not None:
            poller.cancel()
            try:
                await poller
            except asyncio.CancelledError:
                pass


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    # CSRF defence for the cookie-authenticated portal (same-origin check on
    # state-changing requests). Added before SessionMiddleware so it runs after
    # it on the way in — it only needs cookies/headers, both already present.
    app.add_middleware(SameOriginCSRFMiddleware)
    # Used by Authlib for the OIDC state/nonce during the code flow.
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)
    # Security audit capture. Added before SecurityHeadersMiddleware so it still
    # observes the final response — including the 403 produced by the CSRF
    # middleware and role-guard denials raised deep in the app.
    app.add_middleware(AuditMiddleware)
    # Security response headers (CSP, HSTS, etc.). Added last so it is the
    # OUTERMOST middleware and stamps every response, including middleware-level
    # error responses (CSRF 403, auth 401) that never reach a route.
    app.add_middleware(SecurityHeadersMiddleware)

    # Unauthenticated liveness/readiness probes (root-level, no /api prefix).
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(api_router, prefix="/api")
    app.include_router(web_router)

    static_dir = Path(__file__).resolve().parent / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.exception_handler(StarletteHTTPException)
    async def _auth_redirect(request: Request, exc: StarletteHTTPException):
        """Redirect unauthenticated browser requests to the login page; API
        clients still receive JSON 401s."""
        accepts_html = "text/html" in request.headers.get("accept", "")
        is_api = request.url.path.startswith("/api")
        if exc.status_code == 401 and accepts_html and not is_api:
            return RedirectResponse("/auth/login", status_code=303)
        return await http_exception_handler(request, exc)

    return app


app = create_app()
