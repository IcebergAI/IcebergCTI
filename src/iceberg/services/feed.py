"""Personalized dissemination feed helpers shared by the API and portal."""

from typing import TypedDict

from fastapi import HTTPException
from sqlmodel import Session, select

from ..models import DisseminationEvent, Report, User, utcnow
from . import reports as report_service


class DeliveryReason(TypedDict):
    """Why this product reached this reader — the answer to "why am I seeing
    this?", which the feed could previously never give."""

    kind: str  # requirement | tag | audience | level | all
    label: str
    requirement_id: int | None


class FeedItem(TypedDict):
    event: DisseminationEvent
    report: Report
    reason: DeliveryReason


def delivery_reason(user: User, report: Report) -> DeliveryReason:
    """Re-derive why ``report`` matched ``user``, in the order the reader cares
    about it.

    This mirrors the match rules in ``dissemination.matched_stakeholders`` (the
    only place delivery is decided) rather than storing a second copy of them on
    ``DisseminationEvent`` — no schema change, and no way for a stored reason to
    drift from the rule that actually routed the product.
    """
    own = [r for r in report.requirements if r.stakeholder_id == user.id]
    if own:
        return {
            "kind": "requirement",
            "label": "Answers your RFI",
            "requirement_id": own[0].id,
        }
    subscribed = {t.id: t for t in user.tag_subscriptions}
    matched_tags = [t for t in report.tags if t.id in subscribed]
    if matched_tags:
        return {
            "kind": "tag",
            "label": matched_tags[0].label,
            "requirement_id": None,
        }
    user_groups = {g.id for g in user.audience_groups}
    matched_groups = [g for g in report.audience_groups if g.id in user_groups]
    if matched_groups:
        return {
            "kind": "audience",
            "label": matched_groups[0].name,
            "requirement_id": None,
        }
    if user.preferred_intel_level is not None:
        return {
            "kind": "level",
            "label": f"Matched level · {report.intel_level.value}",
            "requirement_id": None,
        }
    return {"kind": "all", "label": "All levels", "requirement_id": None}


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
            report = report_service.ensure_visible(event.report, user)
        except HTTPException:
            continue
        items.append(
            {
                "event": event,
                "report": report,
                "reason": delivery_reason(user, report),
            }
        )
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
