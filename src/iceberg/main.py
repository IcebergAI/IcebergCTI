"""FastAPI application factory: mounts the JSON API and the server-rendered
portal in a single ASGI app, wires session middleware, static files and an auth
redirect for browser requests."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from .api import api_router
from .auth.audit_middleware import AuditMiddleware
from .auth.csrf import SameOriginCSRFMiddleware
from .auth.routes import router as auth_router
from .config import get_settings
from .db import init_db
from .web import web_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    # CSRF defence for the cookie-authenticated portal (same-origin check on
    # state-changing requests). Added before SessionMiddleware so it runs after
    # it on the way in — it only needs cookies/headers, both already present.
    app.add_middleware(SameOriginCSRFMiddleware)
    # Used by Authlib for the OIDC state/nonce during the code flow.
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)
    # Security audit capture. Added last so it is the OUTERMOST middleware and
    # observes the final response — including the 403 produced by the CSRF
    # middleware and role-guard denials raised deep in the app.
    app.add_middleware(AuditMiddleware)

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
