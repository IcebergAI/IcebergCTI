"""Report lifecycle state machine: DRAFT -> IN_REVIEW -> APPROVED -> PUBLISHED.

Transitions are explicit and role-guarded. Publishing stamps ``published_at``
and is the point at which (in a later milestone) dissemination is triggered.
"""

from sqlmodel import Session

from ..models import Report, ReportStatus, Role, User, utcnow

# Allowed status transitions (forward progression + sending back for rework).
_ALLOWED: dict[ReportStatus, set[ReportStatus]] = {
    ReportStatus.DRAFT: {ReportStatus.IN_REVIEW},
    ReportStatus.IN_REVIEW: {ReportStatus.APPROVED, ReportStatus.DRAFT},
    ReportStatus.APPROVED: {ReportStatus.PUBLISHED, ReportStatus.IN_REVIEW},
    ReportStatus.PUBLISHED: set(),
}


class LifecycleError(Exception):
    """Raised when a transition is not allowed or the actor lacks permission."""


def _can_review(user: User) -> bool:
    return user.role in {Role.REVIEWER, Role.ADMIN}


def transition(
    session: Session,
    report: Report,
    target: ReportStatus,
    *,
    actor: User,
    commit: bool = True,
) -> Report:
    target = ReportStatus(target)
    current = ReportStatus(report.status)

    if target not in _ALLOWED[current]:
        raise LifecycleError(
            f"Cannot move report from {current.value} to {target.value}"
        )

    # Role guards per transition.
    if current == ReportStatus.DRAFT and target == ReportStatus.IN_REVIEW:
        # Author submits their own draft for review.
        if actor.id != report.author_id and actor.role != Role.ADMIN:
            raise LifecycleError("Only the author can submit this report for review")
    else:
        # Approving, publishing or sending back requires reviewer privileges.
        if not _can_review(actor):
            raise LifecycleError("Reviewer or admin role required for this action")

    if target == ReportStatus.APPROVED:
        report.reviewer_id = actor.id
    if target == ReportStatus.PUBLISHED:
        report.published_at = utcnow()
    if target == ReportStatus.DRAFT:
        # Sent back for rework; clear the prior approval.
        report.reviewer_id = None

    report.status = target
    report.updated_at = utcnow()
    session.add(report)
    if commit:
        session.commit()
        session.refresh(report)
    return report
