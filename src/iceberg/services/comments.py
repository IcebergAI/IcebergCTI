"""Section-anchored editorial comments and suggested edits (#306).

Review already records *decisions*; this records the conversation that produces
them, precisely enough to act on. A comment anchors to a section of the product
**and** to the revision it was written against, so it can say honestly whether
the passage it refers to still exists. A comment may carry a **suggestion** — a
bounded "replace exactly this with that" — which is applied through the ordinary
optimistic-lock write, so it is refused rather than clobbering a concurrent edit.

Threads are internal editorial material: they are writer-only and additionally
pass ``reports.ensure_visible``, so they never reach a stakeholder or leak from a
product the reader has no access to.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlmodel import Session, col, select

from ..config import get_settings
from ..models import (
    CommentStatus,
    Report,
    ReportComment,
    ReportSection,
    Role,
    User,
    utcnow,
)

# Section → the report field it anchors to. ``None`` marks a section whose text
# lives outside the report row (citations, attachments, figures, indicators):
# those anchor to the section as a whole and never carry a suggestion.
_SECTION_FIELDS: dict[ReportSection, str | None] = {
    ReportSection.BODY: "body_md",
    ReportSection.KEY_JUDGEMENTS: "key_judgements",
    ReportSection.KEY_ASSUMPTIONS: "key_assumptions",
    ReportSection.INTELLIGENCE_GAPS: "intelligence_gaps",
    ReportSection.SOURCES: None,
    ReportSection.ATTACHMENTS: None,
    ReportSection.INDICATORS: None,
    ReportSection.FIGURES: None,
}

_MENTION = re.compile(r"@([A-Za-z0-9._-]{2,64})")
_MAX_BODY = 4000
_MAX_ANCHOR = 2000


class CommentError(HTTPException):
    def __init__(self, detail: str, code: int = status.HTTP_422_UNPROCESSABLE_CONTENT):
        super().__init__(code, detail)


def section_text(report: Report, section: ReportSection) -> str | None:
    field = _SECTION_FIELDS[ReportSection(section)]
    return None if field is None else str(getattr(report, field) or "")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def require_writer(user: User) -> None:
    """Editorial threads are internal: writers only, never a stakeholder."""

    if user.role not in {Role.ANALYST, Role.REVIEWER, Role.ADMIN}:
        raise CommentError(
            "Editorial threads are visible to analysts and reviewers only",
            status.HTTP_403_FORBIDDEN,
        )


# --------------------------------------------------------------------------- #
# Anchors
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AnchorState:
    """How well a thread's anchor still describes the product it points at."""

    section: str
    anchor_version: int
    current_version: int
    section_changed: bool
    anchor_present: bool
    occurrences: int

    @property
    def stale(self) -> bool:
        return self.section_changed

    @property
    def label(self) -> str:
        if not self.section_changed:
            return "current"
        if self.anchor_present:
            return "section edited — quoted text still present"
        return "quoted text no longer in this section"


def anchor_state(report: Report, comment: ReportComment) -> AnchorState:
    text = section_text(report, ReportSection(comment.section))
    if text is None:
        # A section without report-row text (citations, attachments, figures):
        # only the revision it was raised against can go stale.
        changed = report.version != comment.anchor_version
        return AnchorState(
            section=str(comment.section),
            anchor_version=comment.anchor_version,
            current_version=report.version,
            section_changed=changed,
            anchor_present=True,
            occurrences=0,
        )
    occurrences = text.count(comment.anchor_text) if comment.anchor_text else 0
    return AnchorState(
        section=str(comment.section),
        anchor_version=comment.anchor_version,
        current_version=report.version,
        section_changed=_digest(text) != comment.anchor_sha256,
        anchor_present=bool(comment.anchor_text) and occurrences > 0,
        occurrences=occurrences,
    )


# --------------------------------------------------------------------------- #
# Mentions
# --------------------------------------------------------------------------- #
def _mention_handle(user: User) -> str:
    return (user.email or "").split("@")[0].lower()


def resolve_mentions(session: Session, body: str) -> list[int]:
    """Writers named as ``@handle`` (the local part of their address).

    Only writers can be mentioned: a stakeholder has no access to the thread, so
    notifying them would be an invitation to something they cannot open.
    """

    handles = {match.lower() for match in _MENTION.findall(body or "")}
    if not handles:
        return []
    writers = session.exec(
        select(User).where(col(User.role).in_([Role.ANALYST, Role.REVIEWER, Role.ADMIN]))
    ).all()
    return [
        user.id
        for user in writers
        if user.id is not None and _mention_handle(user) in handles
    ]


def _notify_mentions(session: Session, comment: ReportComment, report: Report) -> int:
    """Enqueue one durable notification per mentioned writer (never inline mail)."""

    from . import jobs

    queued = 0
    for user_id in comment.mentions:
        if user_id == comment.author_id:
            continue  # nobody needs telling they mentioned themselves
        jobs.enqueue(
            session,
            kind=jobs.JobKind.EDITORIAL_MENTION,
            payload={"comment_id": comment.id, "report_id": report.id, "user_id": user_id},
            idempotency_key=f"comment:{comment.id}:mention:{user_id}",
        )
        queued += 1
    return queued


# --------------------------------------------------------------------------- #
# Threads
# --------------------------------------------------------------------------- #
def threads(session: Session, report: Report) -> list[dict]:
    """Every thread on a product, newest first, with replies and anchor state."""

    rows = list(
        session.exec(
            select(ReportComment)
            .where(ReportComment.report_id == report.id)
            .order_by(col(ReportComment.id))
        ).all()
    )
    replies: dict[int, list[ReportComment]] = {}
    for row in rows:
        if row.thread_id is not None:
            replies.setdefault(row.thread_id, []).append(row)
    roots = [row for row in rows if row.thread_id is None]
    roots.sort(key=lambda row: row.id or 0, reverse=True)
    return [
        {
            "comment": root,
            "replies": replies.get(root.id or 0, []),
            "anchor": anchor_state(report, root),
        }
        for root in roots
    ]


def open_blocking(session: Session, report: Report) -> list[ReportComment]:
    return list(
        session.exec(
            select(ReportComment).where(
                ReportComment.report_id == report.id,
                col(ReportComment.thread_id).is_(None),
                col(ReportComment.blocking).is_(True),
                ReportComment.status == CommentStatus.OPEN,
            )
        ).all()
    )


def publication_blocked_by(session: Session, report: Report) -> list[ReportComment]:
    """Open blocking threads, when the deployment gates publication on them."""

    if not get_settings().publish_requires_resolved_threads:
        return []
    return open_blocking(session, report)


def get_or_404(session: Session, report: Report, comment_id: int) -> ReportComment:
    comment = session.get(ReportComment, comment_id)
    if comment is None or comment.report_id != report.id:
        raise CommentError("Comment not found", status.HTTP_404_NOT_FOUND)
    return comment


def create_thread(
    session: Session,
    report: Report,
    *,
    author: User,
    section: ReportSection,
    body: str,
    anchor_text: str = "",
    suggestion: str = "",
    blocking: bool = False,
) -> ReportComment:
    require_writer(author)
    body = (body or "").strip()
    anchor_text = (anchor_text or "").strip()
    suggestion = (suggestion or "").strip()
    if not body and not suggestion:
        raise CommentError("A comment needs a message or a suggested edit")
    if len(body) > _MAX_BODY or len(suggestion) > _MAX_BODY:
        raise CommentError(f"A comment is limited to {_MAX_BODY} characters")
    if len(anchor_text) > _MAX_ANCHOR:
        raise CommentError(f"A quoted passage is limited to {_MAX_ANCHOR} characters")

    section = ReportSection(section)
    text = section_text(report, section)
    if suggestion:
        if text is None:
            raise CommentError(
                f"The {section.value.replace('_', ' ').lower()} section holds no editable "
                "text, so it cannot carry a suggested edit"
            )
        if not anchor_text:
            raise CommentError("A suggested edit must quote the passage it replaces")
    if anchor_text and text is not None:
        occurrences = text.count(anchor_text)
        if occurrences == 0:
            raise CommentError("The quoted passage is not in that section of the report")
        if suggestion and occurrences > 1:
            raise CommentError(
                "The quoted passage appears more than once in that section — quote "
                "more of the surrounding text so the edit is unambiguous"
            )

    comment = ReportComment(
        report_id=report.id,
        author_id=author.id,
        section=section,
        anchor_version=report.version,
        anchor_text=anchor_text,
        anchor_sha256=_digest(text) if text is not None else "",
        body=body,
        suggestion=suggestion,
        blocking=blocking,
        mentions=resolve_mentions(session, body),
    )
    session.add(comment)
    session.commit()
    session.refresh(comment)
    _notify_mentions(session, comment, report)
    session.commit()
    return comment


def reply(
    session: Session, report: Report, thread: ReportComment, *, author: User, body: str
) -> ReportComment:
    require_writer(author)
    if thread.thread_id is not None:
        raise CommentError("Reply to the thread, not to a reply")
    body = (body or "").strip()
    if not body:
        raise CommentError("A reply needs a message")
    if len(body) > _MAX_BODY:
        raise CommentError(f"A reply is limited to {_MAX_BODY} characters")
    comment = ReportComment(
        report_id=report.id,
        thread_id=thread.id,
        author_id=author.id,
        section=thread.section,
        anchor_version=report.version,
        body=body,
        mentions=resolve_mentions(session, body),
    )
    session.add(comment)
    session.commit()
    session.refresh(comment)
    _notify_mentions(session, comment, report)
    session.commit()
    return comment


def _close(
    session: Session,
    thread: ReportComment,
    *,
    actor: User,
    outcome: CommentStatus,
    applied_version: int | None = None,
) -> ReportComment:
    thread.status = outcome
    thread.resolved_at = utcnow()
    thread.resolved_by_id = actor.id
    thread.applied_version = applied_version
    thread.updated_at = utcnow()
    session.add(thread)
    session.commit()
    session.refresh(thread)
    return thread


def resolve(session: Session, thread: ReportComment, *, actor: User) -> ReportComment:
    require_writer(actor)
    if thread.thread_id is not None:
        raise CommentError("Only a thread can be resolved")
    if thread.status is not CommentStatus.OPEN:
        raise CommentError("Thread is already closed", status.HTTP_409_CONFLICT)
    return _close(session, thread, actor=actor, outcome=CommentStatus.RESOLVED)


def reject(session: Session, thread: ReportComment, *, actor: User) -> ReportComment:
    require_writer(actor)
    if thread.thread_id is not None:
        raise CommentError("Only a thread can be rejected")
    if thread.status is not CommentStatus.OPEN:
        raise CommentError("Thread is already closed", status.HTTP_409_CONFLICT)
    return _close(session, thread, actor=actor, outcome=CommentStatus.REJECTED)


def reopen(session: Session, thread: ReportComment, *, actor: User) -> ReportComment:
    require_writer(actor)
    if thread.status is CommentStatus.ACCEPTED:
        raise CommentError(
            "An accepted suggestion is part of the product's history and cannot be "
            "reopened — raise a new thread",
            status.HTTP_409_CONFLICT,
        )
    thread.status = CommentStatus.OPEN
    thread.resolved_at = None
    thread.resolved_by_id = None
    thread.updated_at = utcnow()
    session.add(thread)
    session.commit()
    session.refresh(thread)
    return thread


def accept_suggestion(
    session: Session,
    report: Report,
    thread: ReportComment,
    *,
    actor: User,
    expected_version: int,
) -> ReportComment:
    """Write a suggested edit into the product under an optimistic-lock check.

    Three things must hold, and each failure is a 409 rather than a silent
    overwrite: the caller must have loaded the revision they are acting on, the
    product must still be editable, and the quoted passage must still appear
    exactly once in its section.
    """

    require_writer(actor)
    from .reports import ensure_editable

    # Reviewers propose; the analyst who owns the product decides. Applying a
    # suggestion is a content edit, so it follows the same author-and-not-
    # published rule as any other edit rather than inventing a second one.
    if thread.thread_id is not None:
        raise CommentError("Only a thread can carry a suggested edit")
    if not thread.suggestion:
        raise CommentError("This comment proposes no replacement text")
    if thread.status is not CommentStatus.OPEN:
        raise CommentError("Thread is already closed", status.HTTP_409_CONFLICT)
    if report.version != expected_version:
        raise CommentError(
            "The report changed while this suggestion was open — reload and review "
            "it against the current text",
            status.HTTP_409_CONFLICT,
        )
    ensure_editable(report, actor)

    section = ReportSection(thread.section)
    field = _SECTION_FIELDS[section]
    text = section_text(report, section)
    if field is None or text is None:  # pragma: no cover - create() forbids it
        raise CommentError("That section holds no editable text")
    occurrences = text.count(thread.anchor_text)
    if occurrences != 1:
        raise CommentError(
            "The passage this suggestion replaces is no longer uniquely present in "
            "the report; resolve the thread and re-quote the current text",
            status.HTTP_409_CONFLICT,
        )

    setattr(report, field, text.replace(thread.anchor_text, thread.suggestion, 1))
    report.updated_at = utcnow()
    session.add(report)
    session.commit()
    session.refresh(report)
    return _close(
        session,
        thread,
        actor=actor,
        outcome=CommentStatus.ACCEPTED,
        applied_version=report.version,
    )
