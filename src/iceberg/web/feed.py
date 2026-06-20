"""Dissemination feed, preferences & help portal routes."""

from sqlmodel import select
from typing import Annotated

from fastapi import (
    Form,
    Query,
    Request,
)

from .. import help_content
from ..auth.dependencies import CurrentUser
from ..models import (
    DisseminationEvent,
    IntelLevel,
    Role,
    utcnow,
)
from ..templating import templates
from .common import (
    SessionDep,
    _redirect,
    router,
)

@router.get("/feed")
def feed_view(request: Request, session: SessionDep, user: CurrentUser):
    events = list(
        session.exec(
            select(DisseminationEvent)
            .where(DisseminationEvent.stakeholder_id == user.id)
            .order_by(DisseminationEvent.created_at.desc())
        ).all()
    )
    unread_ids = {e.id for e in events if e.read_at is None}
    items = [{"event": e, "report": e.report} for e in events]
    # Viewing the feed marks everything read.
    for e in events:
        if e.read_at is None:
            e.read_at = utcnow()
            session.add(e)
    session.commit()
    return templates.TemplateResponse(
        request,
        "feed.html",
        {"user": user, "items": items, "unread_ids": unread_ids},
    )


@router.get("/preferences")
def preferences_view(request: Request, session: SessionDep, user: CurrentUser):
    return templates.TemplateResponse(
        request, "preferences.html", {"user": user}
    )


def _coerce_role(role: str | None, *, default: Role) -> Role:
    """Parse a ``?role=`` query value, falling back to the viewer's own role on
    anything unrecognised (so ``/help?role=BOGUS`` never 500s)."""
    if not role:
        return default
    try:
        return Role(role.upper())
    except ValueError:
        return default


@router.get("/help")
def help_view(
    request: Request,
    user: CurrentUser,
    role: Annotated[str | None, Query()] = None,
):
    active = _coerce_role(role, default=user.role)
    return templates.TemplateResponse(
        request,
        "help.html",
        {
            "user": user,
            "active_role": active,
            "guides": help_content.ROLE_GUIDES,
            "active_guide": help_content.guide_for(active),
            "concepts": help_content.CONCEPTS,
        },
    )


@router.post("/preferences")
def preferences_save(
    session: SessionDep,
    user: CurrentUser,
    preferred_intel_level: Annotated[str, Form()] = "",
):
    user.preferred_intel_level = (
        IntelLevel(preferred_intel_level) if preferred_intel_level else None
    )
    session.add(user)
    session.commit()
    return _redirect("/preferences")


