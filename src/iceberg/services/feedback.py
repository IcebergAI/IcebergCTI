"""Stakeholder product feedback / RFI-satisfaction — the intelligence-cycle
feedback loop (backlog D).

Rules live here (raising ``HTTPException``) so the JSON API and the portal share
one source of truth, mirroring ``services/requirements.py``. Feedback is only
accepted on *delivered* products (a ``DisseminationEvent`` must exist for the
stakeholder), and a ``MET`` verdict from the owning stakeholder on a linked
requirement auto-closes it via ``requirements.set_status``.
"""

from fastapi import HTTPException, status
from sqlmodel import Session, col, func, select

from ..models import (
    DisseminationEvent,
    ProductFeedback,
    ProductUsefulness,
    Report,
    Requirement,
    RequirementStatus,
    RfiSatisfaction,
    User,
    utcnow,
)
from . import requirements as req_service


def was_delivered(session: Session, report: Report, stakeholder: User) -> bool:
    """True if this report was disseminated to this stakeholder's feed."""
    return (
        session.exec(
            select(DisseminationEvent).where(
                DisseminationEvent.report_id == report.id,
                DisseminationEvent.stakeholder_id == stakeholder.id,
            )
        ).first()
        is not None
    )


def linked_requirements(report: Report, stakeholder: User) -> list[Requirement]:
    """The stakeholder's *own* requirements this report satisfies — the choices
    offered in the feedback form's satisfaction dropdown."""
    return [r for r in report.requirements if r.stakeholder_id == stakeholder.id]


def existing_feedback(
    session: Session, report: Report, stakeholder: User
) -> ProductFeedback | None:
    return session.exec(
        select(ProductFeedback).where(
            ProductFeedback.report_id == report.id,
            ProductFeedback.stakeholder_id == stakeholder.id,
        )
    ).first()


def submit_feedback(
    session: Session,
    *,
    report: Report,
    stakeholder: User,
    usefulness,
    requirement_id: int | None = None,
    satisfaction: RfiSatisfaction | None = None,
    comment: str = "",
) -> ProductFeedback:
    """Record (or update) a stakeholder's feedback on a delivered product.

    Upserts the single ``(report, stakeholder)`` row. A ``MET`` verdict on a
    requirement the stakeholder owns *and* the report satisfies advances that
    requirement to ``SATISFIED`` (closing the loop). Other verdicts only signal.
    """
    if not was_delivered(session, report, stakeholder):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Feedback is only available on products disseminated to you",
        )

    requirement: Requirement | None = None
    if requirement_id is not None:
        requirement = session.get(Requirement, requirement_id)
        if (
            requirement is None
            or requirement.stakeholder_id != stakeholder.id
            or requirement not in report.requirements
        ):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Requirement must be your own and addressed by this report",
            )

    fb = existing_feedback(session, report, stakeholder)
    if fb is None:
        fb = ProductFeedback(report_id=report.id, stakeholder_id=stakeholder.id)
    fb.usefulness = usefulness
    fb.requirement_id = requirement.id if requirement else None
    fb.satisfaction = satisfaction if requirement else None
    fb.comment = comment
    fb.updated_at = utcnow()
    session.add(fb)
    session.commit()
    session.refresh(fb)

    # Close the loop: a "Met" verdict from the owner auto-satisfies the requirement.
    if (
        requirement is not None
        and satisfaction == RfiSatisfaction.MET
        and RequirementStatus(requirement.status) is not RequirementStatus.SATISFIED
    ):
        req_service.set_status(session, requirement, RequirementStatus.SATISFIED)

    return fb


def feedback_for_report(session: Session, report: Report) -> list[ProductFeedback]:
    """All feedback received on a report (writer-facing panel)."""
    return list(
        session.exec(
            select(ProductFeedback)
            .where(ProductFeedback.report_id == report.id)
            .order_by(col(ProductFeedback.created_at).desc())
        ).all()
    )


def feedback_for_requirement(
    session: Session, requirement: Requirement
) -> list[ProductFeedback]:
    """All feedback tied to a requirement (analyst-facing detail section)."""
    return list(
        session.exec(
            select(ProductFeedback)
            .where(ProductFeedback.requirement_id == requirement.id)
            .order_by(col(ProductFeedback.created_at).desc())
        ).all()
    )


def _pct(part: int, whole: int) -> float:
    return round(part / whole, 3) if whole else 0.0


def feedback_effectiveness(session: Session) -> dict:
    """Program-level feedback metrics for the maturity dashboard.

    ``response_rate`` is feedback rows per distinct (report, stakeholder)
    dissemination delivery — the share of deliveries that drew a response.
    """
    rows = list(session.exec(select(ProductFeedback)).all())
    total = len(rows)
    deliveries = session.exec(
        select(func.count(col(DisseminationEvent.id)))
    ).one()
    verdicts = [r for r in rows if r.satisfaction is not None]
    met = sum(1 for r in verdicts if r.satisfaction == RfiSatisfaction.MET)
    useful = sum(
        1
        for r in rows
        if r.usefulness in (ProductUsefulness.USEFUL, ProductUsefulness.HIGHLY_USEFUL)
    )
    return {
        "responses": total,
        "deliveries": deliveries,
        "response_rate": _pct(total, deliveries),
        "verdicts": len(verdicts),
        "satisfaction_rate": _pct(met, len(verdicts)),
        "useful_rate": _pct(useful, total),
    }
