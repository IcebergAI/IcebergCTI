"""Inbound reporting ingestion portal routes."""

from typing import Annotated

from fastapi import Form, HTTPException, Request, status
from sqlmodel import Session, select

from ..auth.dependencies import CurrentUser
from ..models import IngestedItem, IngestionSource, Notebook
from ..services import ingestion as ingestion_service
from ..templating import templates
from .common import SessionDep, _redirect, _require_writer, router


@router.get("/ingestion")
def ingestion_view(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    status_filter: str = "NEW",
):
    _require_writer(user)
    sources = ingestion_service.list_sources(session)
    return templates.TemplateResponse(
        request,
        "ingestion.html",
        {
            "user": user,
            "sources": sources,
            "source_names": {source.id: source.name for source in sources},
            "items": ingestion_service.list_items(session, status_filter),
            "notebooks": list(
                session.exec(select(Notebook).order_by(Notebook.updated_at.desc())).all()
            ),
            "status_filter": status_filter,
        },
    )


@router.post("/ingestion/sources")
def ingestion_source_create(
    session: SessionDep,
    user: CurrentUser,
    name: Annotated[str, Form()],
    url: Annotated[str, Form()],
):
    _require_writer(user)
    ingestion_service.create_source(session, name=name, url=url)
    return _redirect("/ingestion")


def _source_or_404(session: Session, source_id: int) -> IngestionSource:
    source = session.get(IngestionSource, source_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ingestion source not found")
    return source


def _item_or_404(session: Session, item_id: int) -> IngestedItem:
    item = session.get(IngestedItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ingested item not found")
    return item


@router.post("/ingestion/sources/{source_id}")
def ingestion_source_update(
    source_id: int,
    session: SessionDep,
    user: CurrentUser,
    name: Annotated[str, Form()],
    url: Annotated[str, Form()],
    active: Annotated[bool, Form()] = False,
):
    _require_writer(user)
    ingestion_service.update_source(
        session, _source_or_404(session, source_id), name=name, url=url, active=active
    )
    return _redirect("/ingestion")


@router.post("/ingestion/sources/{source_id}/pull")
def ingestion_source_pull(source_id: int, session: SessionDep, user: CurrentUser):
    _require_writer(user)
    ingestion_service.pull_source(session, _source_or_404(session, source_id))
    return _redirect("/ingestion")


@router.post("/ingestion/pull")
def ingestion_pull_all(session: SessionDep, user: CurrentUser):
    _require_writer(user)
    ingestion_service.pull_all(session)
    return _redirect("/ingestion")


@router.post("/ingestion/sources/{source_id}/delete")
def ingestion_source_delete(source_id: int, session: SessionDep, user: CurrentUser):
    _require_writer(user)
    ingestion_service.delete_source(session, _source_or_404(session, source_id))
    return _redirect("/ingestion")


@router.post("/ingestion/items/{item_id}/promote")
def ingestion_item_promote(
    item_id: int,
    session: SessionDep,
    user: CurrentUser,
    notebook_id: Annotated[int, Form()],
):
    _require_writer(user)
    source = ingestion_service.promote_item(
        session, _item_or_404(session, item_id), notebook_id
    )
    return _redirect(f"/notebooks/{source.notebook_id}#sources")


@router.post("/ingestion/items/{item_id}/discard")
def ingestion_item_discard(item_id: int, session: SessionDep, user: CurrentUser):
    _require_writer(user)
    item = _item_or_404(session, item_id)
    item.status = "DISCARDED"
    session.add(item)
    session.commit()
    return _redirect("/ingestion")


@router.post("/ingestion/items/{item_id}/restore")
def ingestion_item_restore(item_id: int, session: SessionDep, user: CurrentUser):
    _require_writer(user)
    item = _item_or_404(session, item_id)
    if item.status == "DISCARDED":
        item.status = "NEW"
        session.add(item)
        session.commit()
    return _redirect("/ingestion")
