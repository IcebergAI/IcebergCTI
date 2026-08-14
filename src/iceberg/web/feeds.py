"""Analyst feed reader (FR #50 inbound collection).

Writer-only: a merged stream of articles fetched from the admin-configured RSS
feeds, with a "Send to notebook" action that captures an article into an existing
or new notebook as a Source. Stakeholders never see collection material, so this
is gated by ``_require_writer`` (distinct from the stakeholder ``/feed``).
"""

from typing import Annotated

from fastapi import BackgroundTasks, Form, Query, Request
from sqlmodel import col, select

from ..auth.dependencies import CurrentUser
from ..models import AuditAction, AuditCategory, FeedCandidateStatus, IOC, Notebook
from ..services import audit, feed_indicators
from ..services import feeds as feeds_service
from ..services import notebooks as notebook_service
from ..templating import templates
from .common import SessionDep, _redirect, _require_writer, router


@router.get("/feeds")
def feeds_reader(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    feed: Annotated[int | None, Query()] = None,
    only_unsent: Annotated[bool, Query()] = False,
):
    _require_writer(user)
    feeds = feeds_service.list_feeds(session)
    items = feeds_service.list_items(
        session, feed_id=feed, only_unsent=only_unsent
    )
    feed_titles = {f.id: f.title for f in feeds}
    notebooks = list(
        session.exec(
            select(Notebook).order_by(col(Notebook.updated_at).desc())
        ).all()
    )
    candidates_by_item = {
        item.id: feed_indicators.list_candidates(session, item.id) for item in items
    }
    iocs_by_notebook: dict[int, list[IOC]] = {}
    for ioc in session.exec(select(IOC).order_by(col(IOC.created_at))).all():
        iocs_by_notebook.setdefault(ioc.notebook_id, []).append(ioc)
    return templates.TemplateResponse(
        request,
        "feeds_reader.html",
        {
            "user": user,
            "feeds": feeds,
            "items": items,
            "feed_titles": feed_titles,
            "notebooks": notebooks,
            "active_feed": feed,
            "only_unsent": only_unsent,
            "candidates_by_item": candidates_by_item,
            "iocs_by_notebook": iocs_by_notebook,
        },
    )


def _audit_candidate(session, background_tasks, request, user, action, candidate):
    audit.record_and_emit(
        session, background_tasks=background_tasks, action=action,
        category=AuditCategory.DATA_ACCESS, actor=user, request=request,
        resource_type="feed_indicator_candidate", resource_id=candidate.id,
        detail={"feed_item_id": candidate.feed_item_id, "status": str(candidate.status)},
    )


@router.post("/feeds/items/{item_id}/indicators/extract")
def feeds_extract_indicators(
    item_id: int, request: Request, session: SessionDep, user: CurrentUser,
    background_tasks: BackgroundTasks,
):
    _require_writer(user)
    item = feed_indicators.get_item_or_404(session, item_id)
    candidates = feed_indicators.extract(session, item)
    audit.record_and_emit(
        session, background_tasks=background_tasks,
        action=AuditAction.FEED_INDICATORS_EXTRACTED, category=AuditCategory.DATA_ACCESS,
        actor=user, request=request, resource_type="feed_item", resource_id=item.id,
        detail={"candidate_count": len(candidates), "failed": bool(item.indicator_extraction_error)},
    )
    return _redirect("/feeds")


@router.post("/feeds/items/{item_id}/indicators/{candidate_id}/decision")
def feeds_decide_indicator(
    item_id: int, candidate_id: int, request: Request, session: SessionDep,
    user: CurrentUser, background_tasks: BackgroundTasks,
    decision: Annotated[FeedCandidateStatus, Form()],
    notebook_id: Annotated[int | None, Form()] = None,
    duplicate_ioc_id: Annotated[int | None, Form()] = None,
):
    _require_writer(user)
    candidate = feed_indicators.get_candidate_or_404(session, item_id, candidate_id)
    candidate = feed_indicators.decide(
        session, candidate, decision=decision, actor=user, notebook_id=notebook_id,
        duplicate_ioc_id=duplicate_ioc_id,
    )
    action = {
        FeedCandidateStatus.ACCEPTED: AuditAction.FEED_INDICATOR_ACCEPTED,
        FeedCandidateStatus.REJECTED: AuditAction.FEED_INDICATOR_REJECTED,
        FeedCandidateStatus.DUPLICATE: AuditAction.FEED_INDICATOR_DUPLICATE,
    }[candidate.status]
    _audit_candidate(session, background_tasks, request, user, action, candidate)
    return _redirect("/feeds")


@router.post("/feeds/items/{item_id}/indicators/{candidate_id}/reopen")
def feeds_reopen_indicator(
    item_id: int, candidate_id: int, request: Request, session: SessionDep,
    user: CurrentUser, background_tasks: BackgroundTasks,
):
    _require_writer(user)
    candidate = feed_indicators.get_candidate_or_404(session, item_id, candidate_id)
    candidate = feed_indicators.reopen(session, candidate, actor=user)
    _audit_candidate(session, background_tasks, request, user, AuditAction.FEED_INDICATOR_REOPENED, candidate)
    return _redirect("/feeds")


@router.post("/feeds/items/{item_id}/send")
def feeds_send_to_notebook(
    session: SessionDep,
    user: CurrentUser,
    item_id: int,
    notebook_id: Annotated[str, Form()] = "",
    new_title: Annotated[str, Form()] = "",
    new_topic: Annotated[str, Form()] = "",
):
    _require_writer(user)
    item = feeds_service.get_item_or_404(session, item_id)
    if notebook_id:
        notebook = notebook_service.get_or_404(session, int(notebook_id))
    else:
        notebook = notebook_service.create_notebook(
            session,
            title=new_title.strip() or item.title or "Untitled notebook",
            topic=new_topic.strip(),
            owner_id=user.id,
        )
    feeds_service.send_item_to_notebook(session, item, notebook)
    return _redirect(f"/notebooks/{notebook.id}?updated=source-added#sources")
