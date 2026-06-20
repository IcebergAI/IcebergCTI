"""Shared infrastructure for the portal route modules.

Every ``web/*.py`` route module decorates the single :data:`router` defined here
and pulls the cross-cutting dependencies/guards from this module, so the portal is
one router assembled from domain-grouped files (see ``web/__init__.py``).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import RedirectResponse
from sqlmodel import Session, col, select

from ..auth.dependencies import ensure_role
from ..db import get_session
from ..models import (
    Requirement,
    RequirementStatus,
    Role,
    User,
    board_rank,
    kind_rank,
)
from ..services import notebooks as notebook_service

router = APIRouter(include_in_schema=False)
SessionDep = Annotated[Session, Depends(get_session)]

# Notebook lookup-or-404 — shared by the notebooks, diamond, ACH and report routes.
_get_notebook = notebook_service.get_or_404


def _require_writer(user: User) -> None:
    ensure_role(user, Role.ANALYST, Role.REVIEWER, detail="Read-only user")


def _require_submitter(user: User) -> None:
    """Requirements are submitted by stakeholders (or admins)."""
    ensure_role(
        user, Role.STAKEHOLDER, detail="Only stakeholders can submit requirements"
    )


def _require_admin(user: User) -> None:
    """Taxonomy curation is admin-only (controlled vocabulary)."""
    ensure_role(user, Role.ADMIN, detail="Admin role required")


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


def _open_requirements(
    session: Session, already_linked: list[Requirement]
) -> list[Requirement]:
    """Requirements offerable for linking: the open/in-progress backlog plus any
    already linked (so a linked-then-closed requirement still shows as ticked),
    ordered by priority then age."""
    rows = session.exec(
        select(Requirement).where(
            col(Requirement.status).in_(
                [RequirementStatus.OPEN, RequirementStatus.IN_PROGRESS]
            )
        )
    ).all()
    merged = {r.id: r for r in rows}
    for r in already_linked:
        merged[r.id] = r
    return sorted(
        merged.values(),
        key=lambda r: (-board_rank(r), -kind_rank(r.kind), r.created_at),
    )
