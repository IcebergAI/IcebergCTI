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
part of the public API surface.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlmodel import Session, select

from .db import get_session
from .models import User

router = APIRouter(include_in_schema=False)
SessionDep = Annotated[Session, Depends(get_session)]


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness: the process is up and serving requests."""
    return {"status": "ok"}


@router.get("/readyz")
def readyz(session: SessionDep):
    """Readiness: the database is reachable and the schema is queryable.

    Queries a core table rather than a bare ``SELECT 1`` so a connected-but-
    unmigrated database (``ICEBERG_AUTO_MIGRATE=false`` before the deploy step
    has run migrations) reports *not ready*."""
    try:
        session.exec(select(User).limit(1)).first()
    except Exception:  # noqa: BLE001 — any DB error means not ready
        return JSONResponse({"status": "not ready"}, status_code=503)
    return {"status": "ready"}
