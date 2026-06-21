"""Analyst feed reader (FR #50 inbound collection).

Writer-only: a merged stream of articles fetched from the admin-configured RSS
feeds, with a "Send to notebook" action that captures an article into an existing
or new notebook as a Source. Stakeholders never see collection material, so this
is gated by ``_require_writer`` (distinct from the stakeholder ``/feed``).
"""

from typing import Annotated

from fastapi import Form, Query, Request
from sqlmodel import col, select

from ..auth.dependencies import CurrentUser
from ..models import Notebook
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
        },
    )


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
