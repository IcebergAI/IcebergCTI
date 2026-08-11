"""Unauthenticated operational probes for container orchestration.

Two endpoints, both at the app root (outside ``/api``) and free of any auth
dependency, so a liveness/readiness probe never needs a token:

- ``GET /healthz`` — **liveness**: process-up only, no DB touch. A transient DB
  blip must not restart the pod, so this stays trivial and always ``200``.
- ``GET /readyz`` — **readiness**: a cheap DB round-trip against a core table, so
  it reflects *schema* readiness (the prod deploy runs migrations separately with
  ``ICEBERG_AUTO_MIGRATE=false``, so "process up" ≠ "schema ready"). Returns
  ``503`` when the database is unreachable or not yet migrated, pulling the pod
  out of rotation without killing it.

Kept out of the OpenAPI schema (``include_in_schema=False``) — operational, not
part of the public API surface. ``/metrics`` is disabled by default and requires
an operator-configured bearer token in production.
"""

import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlmodel import Session

from . import db
from .config import get_settings
from .db import get_session
from .services import storage, storage_deletions, storage_metrics

router = APIRouter(include_in_schema=False)
SessionDep = Annotated[Session, Depends(get_session)]


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness: the process is up and serving requests."""
    return {"status": "ok"}


@router.get("/readyz")
def readyz(session: SessionDep):
    """Readiness: database/schema and configured storage are reachable.

    Queries a core table rather than a bare ``SELECT 1`` so a connected-but-
    unmigrated database (``ICEBERG_AUTO_MIGRATE=false`` before the deploy step
    has run migrations) reports *not ready*."""
    try:
        if not db.application_ready(session.get_bind()):
            raise RuntimeError("schema/bootstrap is not ready")
        storage.get_store(session=session).ready()
    except Exception:  # noqa: BLE001 — any DB error means not ready
        return JSONResponse({"status": "not ready"}, status_code=503)
    return {"status": "ready"}


@router.get("/metrics")
def metrics(request: Request, session: SessionDep):
    """Authenticated Prometheus text exposition for operational collectors."""
    settings = get_settings()
    if not settings.metrics_enabled:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    supplied = request.headers.get("authorization", "")
    expected = f"Bearer {settings.metrics_token}"
    if not settings.metrics_token or not hmac.compare_digest(supplied, expected):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    try:
        queue = storage_deletions.queue_metrics(session)
        maintenance = storage_metrics.maintenance_states(session)
    except Exception:  # noqa: BLE001 — do not leak database details
        return JSONResponse({"status": "unavailable"}, status_code=503)
    body = storage_metrics.render_prometheus(
        maintenance=maintenance,
        deletion_depth=int(queue["depth"]),
        deletion_oldest_age=float(queue["oldest_age_seconds"]),
        deletion_failed=int(queue["failed"]),
        deletion_failed_oldest_age=float(queue["failed_oldest_age_seconds"]),
    )
    return PlainTextResponse(
        body,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
