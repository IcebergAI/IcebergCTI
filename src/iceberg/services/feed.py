"""Personalized dissemination feed helpers shared by the API and portal."""

from typing import TypedDict

from fastapi import HTTPException
from sqlmodel import Session, select

from ..models import DisseminationEvent, Report, User, utcnow
from . import reports as report_service


class FeedItem(TypedDict):
    event: DisseminationEvent
    report: Report


def visible_items(session: Session, user: User) -> list[FeedItem]:
    """Current feed items visible to ``user``, newest first."""
    events = session.exec(
        select(DisseminationEvent)
        .where(DisseminationEvent.stakeholder_id == user.id)
        .order_by(DisseminationEvent.created_at.desc())
    ).all()
    items: list[FeedItem] = []
    for event in events:
        try:
            items.append(
                {
                    "event": event,
                    "report": report_service.ensure_visible(event.report, user),
                }
            )
        except HTTPException:
            continue
    return items


def mark_visible_read(session: Session, user: User) -> int:
    """Mark only currently visible unread feed events as read."""
    marked = 0
    for item in visible_items(session, user):
        event = item["event"]
        if event.read_at is None:
            event.read_at = utcnow()
            session.add(event)
            marked += 1
    if marked:
        session.commit()
    return marked


def visible_unread_count(session: Session, user: User) -> int:
    """Unread feed count after applying the same visibility gate as /feed."""
    return sum(
        1 for item in visible_items(session, user) if item["event"].read_at is None
    )
