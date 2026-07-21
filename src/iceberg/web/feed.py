"""Dissemination feed, preferences & help portal routes."""

from datetime import timedelta
from typing import Annotated

from fastapi import (
    Form,
    Query,
    Request,
)

from .. import help_content
from ..auth.dependencies import CurrentUser
from ..models import (
    IntelLevel,
    Role,
    TagKind,
    utcnow,
)
from ..services import feed as feed_service
from ..services import tags as tag_service
from ..templating import templates
from .common import (
    SessionDep,
    _redirect,
    router,
)

def _buckets(items: list, now) -> list[tuple[str, list]]:
    """Group feed items into Today / This week / Earlier, dropping empty
    buckets. Computed server-side so the grouping survives without Alpine."""
    today = now.date()
    week_ago = today - timedelta(days=7)
    grouped: dict[str, list] = {"Today": [], "This week": [], "Earlier": []}
    for item in items:
        created = item["event"].created_at.date()
        if created >= today:
            grouped["Today"].append(item)
        elif created > week_ago:
            grouped["This week"].append(item)
        else:
            grouped["Earlier"].append(item)
    return [(label, rows) for label, rows in grouped.items() if rows]


@router.get("/feed")
def feed_view(request: Request, session: SessionDep, user: CurrentUser):
    items = feed_service.visible_items(session, user)
    unread_ids = {item["event"].id for item in items if item["event"].read_at is None}
    # Viewing the feed marks currently visible items read.
    feed_service.mark_visible_read(session, user)
    return templates.TemplateResponse(
        request,
        "feed.html",
        {
            "user": user,
            "items": items,
            "unread_ids": unread_ids,
            "buckets": _buckets(items, utcnow()),
        },
    )


@router.get("/preferences")
def preferences_view(request: Request, session: SessionDep, user: CurrentUser):
    return templates.TemplateResponse(
        request,
        "preferences.html",
        {
            "user": user,
            "tags_by_kind": _tags_by_kind(
                tag_service.list_tags(session, include_inactive=False)
            ),
            "subscribed_tag_ids": {t.id for t in user.tag_subscriptions},
        },
    )


def _tags_by_kind(tags) -> dict[TagKind, list]:
    grouped: dict[TagKind, list] = {k: [] for k in TagKind}
    for tag in tags:
        grouped[tag.kind].append(tag)
    return {k: v for k, v in grouped.items() if v}


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
    subscribed_tag_ids: Annotated[list[int], Form()] = [],
):
    user.preferred_intel_level = (
        IntelLevel(preferred_intel_level) if preferred_intel_level else None
    )
    session.add(user)
    session.commit()
    tag_service.set_user_subscriptions(session, user, subscribed_tag_ids)
    return _redirect("/preferences")
