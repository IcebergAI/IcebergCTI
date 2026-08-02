"""Personalized dissemination feed helpers shared by the API and portal."""

from typing import TypedDict

from fastapi import HTTPException
from sqlalchemy.orm import selectinload
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

    kind: str  # tag | audience | level | level_changed | all
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
    """The strongest reason the feed can honestly give for this delivery.

    Routing is a **conjunction** — a report reaches a stakeholder only when the
    audience-group gate, the TLP ceiling and the level/tag preference all permit
    it — so this is not a "first rule that fired" ordering. It reports the most
    specific thing still true *now*, preferring tag → audience → level.

    "Now" is the point. Delivery is never retracted, so a reader whose
    preferences changed after delivery still sees the item; claiming it
    "matches" would be a false present-tense statement. The level branch already
    said so when the preference had moved on, but the tag branch did not — a
    report whose tags the reader has since unsubscribed from still showed a
    positive "Matches your … interest" chip (#282). Both are honest now.
    """
    subscribed = {t.id for t in user.tag_subscriptions}
    matched_tags = [t for t in report.tags if t.id in subscribed]
    if matched_tags:
        return {"kind": "tag", "label": f"Matches your {matched_tags[0].label} interest"}
    # Subscribed to tags, but none of them are on this report. The subscription
    # acts as a hard filter in ``matched_stakeholders`` — a subscribed reader
    # with no tag overlap is skipped whether the report is tagged or UNTAGGED —
    # so this fires on the bare ``subscribed`` state, not only when the report
    # carries other tags. Requiring ``report.tags`` here would let an untagged
    # report fall through to a positive "Matches your … preference" chip that
    # the reader's current filters would never deliver (post-#282 review).
    if subscribed:
        return {
            "kind": "tag_changed",
            "label": "Outside your current tag interests",
        }
    user_groups = {g.id for g in user.audience_groups}
    matched_groups = [g for g in report.audience_groups if g.id in user_groups]
    if matched_groups:
        return {"kind": "audience", "label": f"For {matched_groups[0].name}"}
    if user.preferred_intel_level is not None:
        # Compare, don't assume. A product delivered under a preference the
        # reader has since changed is still in their feed (delivery is not
        # retracted), and claiming it "matches" would be exactly the false
        # present-tense statement this split was meant to avoid.
        if user.preferred_intel_level == report.intel_level:
            return {
                "kind": "level",
                "label": f"Matches your {report.intel_level.value} preference",
            }
        return {
            "kind": "level_changed",
            "label": (
                f"{report.intel_level.value} · outside your current "
                f"{user.preferred_intel_level.value} preference"
            ),
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
    """Current feed items visible to ``user``, newest first.

    Eager-loads exactly what ``delivery_context`` and the visibility gate touch.
    Without this each event lazy-loaded its report's tags, audience groups and
    requirements one query at a time — O(N) round-trips to render one page, the
    same shape ``dissemination.matched_stakeholders`` already avoids (#282).
    """
    events = session.exec(
        select(DisseminationEvent)
        .where(DisseminationEvent.stakeholder_id == user.id)
        .order_by(DisseminationEvent.created_at.desc())
        .options(
            selectinload(DisseminationEvent.report).selectinload(Report.tags),
            selectinload(DisseminationEvent.report).selectinload(
                Report.audience_groups
            ),
            selectinload(DisseminationEvent.report).selectinload(
                Report.requirements
            ),
        )
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


def mark_visible_read(session: Session, user: User, items: list[FeedItem] | None = None) -> int:
    """Mark only currently visible unread feed events as read.

    Accepts an already-computed ``items`` list: the feed view builds one to
    render, and re-deriving it here meant every delivery context was computed
    twice per GET for a value this function throws away (#282).
    """
    marked = 0
    for item in items if items is not None else visible_items(session, user):
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
