"""Inbound collection API: RSS/Atom source config and triage items."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from ..auth.dependencies import require_role
from ..db import get_session
from ..models import IngestedItem, IngestionSource, Role
from ..schemas import IngestionPromote, IngestionSourceCreate, IngestionSourceUpdate
from ..services import ingestion as ingestion_service

router = APIRouter(prefix="/ingestion", tags=["ingestion"])
SessionDep = Annotated[Session, Depends(get_session)]
Writer = Annotated[object, Depends(require_role(Role.ANALYST, Role.REVIEWER))]


@router.get("/sources")
def sources(session: SessionDep, _w: Writer) -> list[IngestionSource]:
    return ingestion_service.list_sources(session)


@router.post("/sources", status_code=status.HTTP_201_CREATED)
def create_source(
    body: IngestionSourceCreate, session: SessionDep, _w: Writer
) -> IngestionSource:
    return ingestion_service.create_source(session, name=body.name, url=body.url)


def _source_or_404(session: Session, source_id: int) -> IngestionSource:
    source = session.get(IngestionSource, source_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ingestion source not found")
    return source


@router.patch("/sources/{source_id}")
def update_source(
    source_id: int,
    body: IngestionSourceUpdate,
    session: SessionDep,
    _w: Writer,
) -> IngestionSource:
    source = _source_or_404(session, source_id)
    return ingestion_service.update_source(
        session, source, name=body.name, url=body.url, active=body.active
    )


@router.delete("/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_source(source_id: int, session: SessionDep, _w: Writer) -> None:
    ingestion_service.delete_source(session, _source_or_404(session, source_id))


@router.post("/pull")
def pull(session: SessionDep, _w: Writer) -> dict:
    return {"created": ingestion_service.pull_all(session)}


@router.post("/sources/{source_id}/pull")
def pull_one(source_id: int, session: SessionDep, _w: Writer) -> dict:
    source = _source_or_404(session, source_id)
    return {"created": ingestion_service.pull_source(session, source)}


@router.get("/items")
def items(session: SessionDep, _w: Writer, status: str = "NEW") -> list[IngestedItem]:
    return ingestion_service.list_items(session, status)


@router.get("/items/{item_id}")
def item_detail(item_id: int, session: SessionDep, _w: Writer) -> IngestedItem:
    item = session.get(IngestedItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ingested item not found")
    return item


@router.post("/items/{item_id}/promote")
def promote(
    item_id: int, body: IngestionPromote, session: SessionDep, _w: Writer
) -> dict:
    item = session.get(IngestedItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ingested item not found")
    source = ingestion_service.promote_item(session, item, body.notebook_id)
    return {"source_id": source.id}


@router.post("/items/{item_id}/discard")
def discard(item_id: int, session: SessionDep, _w: Writer) -> dict:
    item = session.get(IngestedItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ingested item not found")
    item.status = "DISCARDED"
    session.add(item)
    session.commit()
    return {"ok": True}
