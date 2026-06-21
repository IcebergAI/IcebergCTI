"""Admin-only RSS feed configuration console (FR #50 inbound collection).

Mirrors ``/admin/audit`` / ``/admin/tags`` (inline ``_require_admin`` guard,
design-system template, no JSON API). Admins are the only actors who supply a
feed URL — the SSRF-containment boundary for the reintroduced fetcher.
"""

from typing import Annotated

from fastapi import BackgroundTasks, Form, Request

from ..auth.dependencies import CurrentUser
from ..models import AuditAction, AuditCategory, AuditSeverity
from ..services import audit
from ..services import feeds as feeds_service
from ..templating import templates
from .common import SessionDep, _redirect, _require_admin, router


@router.get("/admin/feeds")
def admin_feeds_view(request: Request, session: SessionDep, user: CurrentUser):
    _require_admin(user)
    return templates.TemplateResponse(
        request,
        "admin_feeds.html",
        {"user": user, "feeds": feeds_service.list_feeds(session)},
    )


@router.post("/admin/feeds")
def admin_feeds_create(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
    url: Annotated[str, Form()],
    title: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    enabled: Annotated[bool, Form()] = False,
):
    _require_admin(user)
    feed = feeds_service.create_feed(
        session, url=url, title=title, description=description, enabled=enabled
    )
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=AuditAction.FEED_CREATED,
        category=AuditCategory.ADMIN,
        severity=AuditSeverity.WARNING,
        actor=user,
        request=request,
        resource_type="feed",
        resource_id=feed.id,
        detail={"url": feed.url},
    )
    return _redirect("/admin/feeds")


@router.post("/admin/feeds/fetch")
def admin_feeds_fetch(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
):
    """Fetch all enabled feeds now (so an admin can verify without waiting for
    the poll interval). Declared before ``/admin/feeds/{feed_id}`` so the literal
    path wins over the int path-param."""
    _require_admin(user)
    count = feeds_service.fetch_all_enabled(session)
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=AuditAction.FEED_FETCHED,
        category=AuditCategory.ADMIN,
        actor=user,
        request=request,
        detail={"new_items": count},
    )
    return _redirect("/admin/feeds")


@router.post("/admin/feeds/{feed_id}")
def admin_feeds_update(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
    feed_id: int,
    url: Annotated[str, Form()] = "",
    title: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    enabled: Annotated[bool, Form()] = False,
):
    _require_admin(user)
    feed = feeds_service.get_or_404(session, feed_id)
    feeds_service.update_feed(
        session,
        feed,
        url=url,
        title=title,
        description=description,
        enabled=enabled,
    )
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=AuditAction.FEED_UPDATED,
        category=AuditCategory.ADMIN,
        severity=AuditSeverity.WARNING,
        actor=user,
        request=request,
        resource_type="feed",
        resource_id=feed_id,
    )
    return _redirect("/admin/feeds")


@router.post("/admin/feeds/{feed_id}/delete")
def admin_feeds_delete(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
    feed_id: int,
):
    _require_admin(user)
    feed = feeds_service.get_or_404(session, feed_id)
    feeds_service.delete_feed(session, feed)
    audit.record_and_emit(
        session,
        background_tasks=background_tasks,
        action=AuditAction.FEED_DELETED,
        category=AuditCategory.ADMIN,
        severity=AuditSeverity.WARNING,
        actor=user,
        request=request,
        resource_type="feed",
        resource_id=feed_id,
    )
    return _redirect("/admin/feeds")
