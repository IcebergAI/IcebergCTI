"""Personalized dissemination feed for stakeholders."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlmodel import Session

from ..auth.dependencies import CurrentUser
from ..db import get_session
from ..services import feed as feed_service
from ..services.reports import report_summary

router = APIRouter(tags=["feed"])
SessionDep = Annotated[Session, Depends(get_session)]


@router.get("/feed")
def get_feed(session: SessionDep, user: CurrentUser) -> list[dict]:
    return [
        {"event": item["event"], "report": report_summary(item["report"])}
        for item in feed_service.visible_items(session, user)
    ]


@router.post("/feed/read")
def mark_feed_read(session: SessionDep, user: CurrentUser) -> dict:
    return {"marked_read": feed_service.mark_visible_read(session, user)}
