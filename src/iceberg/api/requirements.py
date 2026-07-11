"""Stakeholder intelligence requirements (PIR/RFI) + analyst tasking.

Stakeholders create and manage their own requirements; analysts/reviewers see
the full backlog, drive status, and link reports/notebooks that satisfy them.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from ..auth.dependencies import CurrentUser, require_role
from ..db import get_session
from ..models import (
    IntelLevel,
    Priority,
    Requirement,
    RequirementKind,
    RequirementStatus,
    Role,
    utcnow,
)
from ..schemas import (
    RequirementCreate,
    RequirementStatusUpdate,
    RequirementUpdate,
)
from ..services import requirements as req_service

router = APIRouter(prefix="/requirements", tags=["requirements"])

SessionDep = Annotated[Session, Depends(get_session)]
# Stakeholders (and admins) submit requirements.
Submitter = Annotated[object, Depends(require_role(Role.STAKEHOLDER))]
# Analysts/reviewers (and admins) triage them.
Analyst = Annotated[object, Depends(require_role(Role.ANALYST, Role.REVIEWER))]


def _get(session: Session, requirement_id: int) -> Requirement:
    req = session.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Requirement not found")
    return req


def _can_view(req: Requirement, user) -> bool:
    if user.role == Role.STAKEHOLDER:
        return req.stakeholder_id == user.id
    return True


def _can_edit_fields(req: Requirement, user) -> bool:
    return user.role == Role.ADMIN or req.stakeholder_id == user.id


@router.get("")
def list_requirements(
    session: SessionDep,
    user: CurrentUser,
    status_filter: RequirementStatus | None = None,
    priority: Priority | None = None,
    intel_level: IntelLevel | None = None,
) -> list[Requirement]:
    stmt = select(Requirement).order_by(Requirement.created_at.desc())
    if user.role == Role.STAKEHOLDER:
        stmt = stmt.where(Requirement.stakeholder_id == user.id)
    if status_filter:
        stmt = stmt.where(Requirement.status == status_filter)
    if priority:
        stmt = stmt.where(Requirement.priority == priority)
    if intel_level:
        stmt = stmt.where(Requirement.intel_level == intel_level)
    return list(session.exec(stmt).all())


@router.post("", status_code=status.HTTP_201_CREATED)
def create_requirement(
    body: RequirementCreate, session: SessionDep, user: CurrentUser, _s: Submitter
) -> Requirement:
    return req_service.create_requirement(
        session,
        stakeholder_id=user.id,
        title=body.title,
        description=body.description,
        intel_level=body.intel_level,
        priority=body.priority,
        kind=body.kind,
        decision_context=body.decision_context,
        review_by=body.review_by,
    )


@router.get("/{requirement_id}")
def get_requirement(
    requirement_id: int, session: SessionDep, user: CurrentUser
) -> dict:
    req = _get(session, requirement_id)
    if not _can_view(req, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your requirement")
    if user.role == Role.STAKEHOLDER:
        return req_service.stakeholder_traceability(req, user)
    return {
        "requirement": req,
        "stakeholder": req.stakeholder,
        "reports": req.reports,
        "notebooks": req.notebooks,
    }


@router.patch("/{requirement_id}")
def update_requirement(
    requirement_id: int,
    body: RequirementUpdate,
    session: SessionDep,
    user: CurrentUser,
) -> Requirement:
    req = _get(session, requirement_id)
    if not _can_edit_fields(req, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cannot edit this requirement")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(req, field, value)
    # Keep the PIR-only fields consistent: GIR/RFI never carry collection-planning
    # data (mirrors the service rule on create).
    if RequirementKind(req.kind) is not RequirementKind.PIR:
        req.decision_context, req.review_by = "", None
    req.updated_at = utcnow()
    session.add(req)
    session.commit()
    session.refresh(req)
    return req


@router.post("/{requirement_id}/status")
def set_status(
    requirement_id: int,
    body: RequirementStatusUpdate,
    session: SessionDep,
    _a: Analyst,
) -> Requirement:
    req = _get(session, requirement_id)
    return req_service.set_status(session, req, body.status)


@router.delete("/{requirement_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_requirement(
    requirement_id: int, session: SessionDep, user: CurrentUser
):
    req = _get(session, requirement_id)
    if not _can_edit_fields(req, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cannot delete this requirement")
    session.delete(req)
    session.commit()
