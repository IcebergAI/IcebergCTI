"""Personalized dissemination feed for stakeholders."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, col, select

from ..auth.dependencies import CurrentUser
from ..db import get_session
from ..models import DisseminationEvent, utcnow
from ..services import reports as report_service

router = APIRouter(tags=["feed"])
SessionDep = Annotated[Session, Depends(get_session)]


@router.get("/feed")
def get_feed(session: SessionDep, user: CurrentUser) -> list[dict]:
    events = session.exec(
        select(DisseminationEvent)
        .where(DisseminationEvent.stakeholder_id == user.id)
        .order_by(DisseminationEvent.created_at.desc())
    ).all()
    items = []
    for event in events:
        try:
            items.append({"event": event, "report": report_service.ensure_visible(event.report, user)})
        except HTTPException:
            continue
    return items


@router.post("/feed/read")
def mark_feed_read(session: SessionDep, user: CurrentUser) -> dict:
    unread = session.exec(
        select(DisseminationEvent).where(
            DisseminationEvent.stakeholder_id == user.id,
            col(DisseminationEvent.read_at).is_(None),
        )
    ).all()
    for event in unread:
        event.read_at = utcnow()
        session.add(event)
    session.commit()
    return {"marked_read": len(unread)}
