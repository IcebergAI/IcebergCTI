"""Stakeholder product feedback / RFI-satisfaction (intel-cycle feedback loop)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from ..auth.dependencies import CurrentUser, require_role
from ..db import get_session
from ..models import ProductFeedback, Report, Role
from ..schemas import FeedbackSubmit
from ..services import feedback as feedback_service

router = APIRouter(prefix="/reports", tags=["feedback"])

SessionDep = Annotated[Session, Depends(get_session)]
Stakeholder = Annotated[object, Depends(require_role(Role.STAKEHOLDER))]
Writer = Annotated[object, Depends(require_role(Role.ANALYST, Role.REVIEWER))]


def _get_report(session: Session, report_id: int) -> Report:
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found")
    return report


@router.post("/{report_id}/feedback", status_code=status.HTTP_201_CREATED)
def submit_feedback(
    report_id: int,
    body: FeedbackSubmit,
    session: SessionDep,
    user: CurrentUser,
    _s: Stakeholder,
) -> ProductFeedback:
    report = _get_report(session, report_id)
    return feedback_service.submit_feedback(
        session,
        report=report,
        stakeholder=user,
        usefulness=body.usefulness,
        requirement_id=body.requirement_id,
        satisfaction=body.satisfaction,
        comment=body.comment,
    )


@router.get("/{report_id}/feedback")
def list_feedback(
    report_id: int,
    session: SessionDep,
    _w: Writer,
) -> list[ProductFeedback]:
    report = _get_report(session, report_id)
    return feedback_service.feedback_for_report(session, report)
