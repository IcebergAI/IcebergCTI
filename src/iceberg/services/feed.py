"""Personalized dissemination feed helpers shared by the API and portal."""

from typing import TypedDict

from fastapi import HTTPException
from sqlmodel import Session, select

from ..models import DisseminationEvent, Report, User, utcnow
from . import reports as report_service


class FeedMatch(TypedDict):
    """How this product **currently** matches the reader's routing preferences.

    Deliberately *present tense*. ``DisseminationEvent`` stores no routing
    metadata, so the publish-time cause cannot be reconstructed: a subscription
    added after delivery, or a preference since changed, would make any
    "this is why it was sent" claim false. What can be stated truthfully is the
    relationship as it stands now, and that is what the chip says.

    (If historical fidelity is ever needed — "why did this arrive in March?" —
    the honest fix is to persist the match on the event at publish time, not to
    infer harder here.)
    """

    kind: str  # tag | audience | level | all
    label: str


class FeedContext(TypedDict):
    """The two independent things a feed row can say about a product: how it
    matches the reader's preferences, and whether it answers a requirement the
    reader raised. They are separate on purpose — requirements are **not** a
    predicate in ``dissemination.matched_stakeholders``, so an RFI link is never
    the reason a product was routed, however useful it is to surface."""

    match: FeedMatch
    answers_requirement_id: int | None


class FeedItem(TypedDict):
    event: DisseminationEvent
    report: Report
    context: FeedContext


def _match(user: User, report: Report) -> FeedMatch:
    """The strongest current match, in the order ``matched_stakeholders`` gates
    on: tag subscription → audience group → level preference → no preference."""
    subscribed = {t.id for t in user.tag_subscriptions}
    matched_tags = [t for t in report.tags if t.id in subscribed]
    if matched_tags:
        return {"kind": "tag", "label": f"Matches your {matched_tags[0].label} interest"}
    user_groups = {g.id for g in user.audience_groups}
    matched_groups = [g for g in report.audience_groups if g.id in user_groups]
    if matched_groups:
        return {"kind": "audience", "label": f"For {matched_groups[0].name}"}
    if user.preferred_intel_level is not None:
        return {
            "kind": "level",
            "label": f"Matches your {report.intel_level.value} preference",
        }
    return {"kind": "all", "label": "You receive all levels"}


def delivery_context(user: User, report: Report) -> FeedContext:
    """What the feed can honestly tell this reader about this product."""
    own = [r for r in report.requirements if r.stakeholder_id == user.id]
    return {
        "match": _match(user, report),
        "answers_requirement_id": own[0].id if own else None,
    }


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
                "context": delivery_context(user, report),
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
