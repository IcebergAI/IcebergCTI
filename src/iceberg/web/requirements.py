"""Requirements & tasking-board portal routes."""

from sqlmodel import Session, select
from datetime import date
from typing import Annotated

from fastapi import (
    Form,
    HTTPException,
    Request,
    status,
)

from ..auth.dependencies import CurrentUser
from ..models import (
    IntelLevel,
    Priority,
    Requirement,
    RequirementKind,
    RequirementStatus,
    Role,
    board_rank,
    kind_rank,
    utcnow,
)
from ..services import (
    feedback as feedback_service,
    requirements as req_service,
    reports as report_service,
)
from ..templating import templates
from .common import (
    SessionDep,
    _redirect,
    _require_submitter,
    _require_writer,
    router,
)

def _get_requirement(session: Session, requirement_id: int) -> Requirement:
    req = session.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Requirement not found")
    return req


@router.get("/requirements")
def requirements_view(request: Request, session: SessionDep, user: CurrentUser):
    if user.role == Role.STAKEHOLDER:
        mine = list(
            session.exec(
                select(Requirement)
                .where(Requirement.stakeholder_id == user.id)
                .order_by(Requirement.created_at.desc())
            ).all()
        )
        return templates.TemplateResponse(
            request,
            "requirements_mine.html",
            {"user": user, "requirements": mine},
        )

    # Analyst/reviewer/admin: aggregated tasking board grouped by status.
    # Ordering blends urgency and kind: a PIR is floored to at least HIGH so it
    # leads standing/ad-hoc work, but a true CRITICAL item still tops the column;
    # kind breaks ties within equal effective priority (FR #42).
    rows = list(session.exec(select(Requirement)).all())
    board = {s: [] for s in RequirementStatus}
    for r in sorted(
        rows, key=lambda r: (-board_rank(r), -kind_rank(r.kind), r.created_at)
    ):
        board[RequirementStatus(r.status)].append(r)
    return templates.TemplateResponse(
        request,
        "tasking_board.html",
        {
            "user": user,
            "board": board,
            "statuses": list(RequirementStatus),
            "coverage": req_service.pir_coverage(session),
        },
    )


def _parse_review_by(raw: str) -> date | None:
    """Coerce an ``<input type="date">`` value to a date. The input posts ``""``
    when empty (not absent), which a typed ``date`` Form param would reject."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


@router.post("/requirements")
def requirement_create(
    session: SessionDep,
    user: CurrentUser,
    title: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    intel_level: Annotated[IntelLevel, Form()] = IntelLevel.STRATEGIC,
    priority: Annotated[Priority, Form()] = Priority.MEDIUM,
    kind: Annotated[RequirementKind, Form()] = RequirementKind.RFI,
    decision_context: Annotated[str, Form()] = "",
    review_by: Annotated[str, Form()] = "",
):
    _require_submitter(user)
    req = req_service.create_requirement(
        session,
        stakeholder_id=user.id,
        title=title,
        description=description,
        intel_level=intel_level,
        priority=priority,
        kind=kind,
        decision_context=decision_context,
        review_by=_parse_review_by(review_by),
    )
    return _redirect(f"/requirements/{req.id}")


@router.get("/requirements/{requirement_id}")
def requirement_detail(
    requirement_id: int, request: Request, session: SessionDep, user: CurrentUser
):
    req = _get_requirement(session, requirement_id)
    if user.role == Role.STAKEHOLDER and req.stakeholder_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your requirement")
    reports = list(req.reports)
    notebooks = list(req.notebooks)
    feedback = feedback_service.feedback_for_requirement(session, req)
    stakeholder = req.stakeholder
    if user.role == Role.STAKEHOLDER:
        traceability = req_service.stakeholder_traceability(req, user)
        reports = traceability["reports"]
        notebooks = []
        stakeholder = user
        # Audience scope can change after feedback was submitted.  Retain only
        # feedback whose report is still visible, so this supporting panel
        # cannot reintroduce a hidden report title.
        feedback = [
            item
            for item in feedback
            if _feedback_report_visible(item, user)
        ]
    return templates.TemplateResponse(
        request,
        "requirement_detail.html",
        {
            "user": user,
            "req": req,
            "stakeholder": stakeholder,
            "reports": reports,
            "notebooks": notebooks,
            "feedback": feedback,
            "can_edit": user.role == Role.ADMIN or req.stakeholder_id == user.id,
            "can_triage": user.role in (Role.ANALYST, Role.REVIEWER, Role.ADMIN),
            "today": date.today(),
        },
    )


def _feedback_report_visible(feedback, user: CurrentUser) -> bool:
    try:
        report_service.ensure_visible(feedback.report, user)
    except HTTPException:
        return False
    return True


@router.post("/requirements/{requirement_id}")
def requirement_update(
    requirement_id: int,
    session: SessionDep,
    user: CurrentUser,
    title: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    intel_level: Annotated[IntelLevel, Form()] = IntelLevel.STRATEGIC,
    priority: Annotated[Priority, Form()] = Priority.MEDIUM,
    kind: Annotated[RequirementKind, Form()] = RequirementKind.RFI,
    decision_context: Annotated[str, Form()] = "",
    review_by: Annotated[str, Form()] = "",
):
    req = _get_requirement(session, requirement_id)
    if user.role != Role.ADMIN and req.stakeholder_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cannot edit this requirement")
    req.title = title
    req.description = description
    req.intel_level = intel_level
    req.priority = priority
    req.kind = kind
    # PIR-only fields: store them for a PIR, blank them otherwise (FR #42).
    if kind is RequirementKind.PIR:
        req.decision_context = decision_context
        req.review_by = _parse_review_by(review_by)
    else:
        req.decision_context = ""
        req.review_by = None
    req.updated_at = utcnow()
    session.add(req)
    session.commit()
    return _redirect(f"/requirements/{requirement_id}")


@router.post("/requirements/{requirement_id}/status")
def requirement_status(
    requirement_id: int,
    session: SessionDep,
    user: CurrentUser,
    status_value: Annotated[RequirementStatus, Form(alias="status")],
):
    _require_writer(user)  # analyst/reviewer/admin only (not read-only stakeholder)
    req = _get_requirement(session, requirement_id)
    req_service.set_status(session, req, status_value)
    return _redirect(f"/requirements/{requirement_id}")


@router.post("/requirements/{requirement_id}/delete")
def requirement_delete(
    requirement_id: int, session: SessionDep, user: CurrentUser
):
    req = _get_requirement(session, requirement_id)
    if user.role != Role.ADMIN and req.stakeholder_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cannot delete this requirement")
    session.delete(req)
    session.commit()
    return _redirect("/requirements")

