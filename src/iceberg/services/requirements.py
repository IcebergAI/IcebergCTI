"""Requirement linking and status helpers (traceability + analyst tasking)."""

from datetime import date

from sqlmodel import Session, col, select
from fastapi import HTTPException

from ..models import (
    IntelLevel,
    Notebook,
    Priority,
    Report,
    Requirement,
    RequirementKind,
    RequirementStatus,
    utcnow,
)
from ..schemas import (
    StakeholderIdentityResponse,
    StakeholderRequirementResponse,
)
from . import reports as report_service


def create_requirement(
    session: Session,
    *,
    stakeholder_id: int,
    title: str,
    description: str = "",
    intel_level: IntelLevel = IntelLevel.STRATEGIC,
    priority: Priority = Priority.MEDIUM,
    kind: RequirementKind = RequirementKind.RFI,
    decision_context: str = "",
    review_by: date | None = None,
) -> Requirement:
    """Create a stakeholder requirement. Shared by the JSON API and the portal.

    The PIR-only time-bound fields (``decision_context`` / ``review_by``) are
    blanked for GIR/RFI so non-PIR rows never carry stray collection-planning
    data.
    """
    if RequirementKind(kind) is not RequirementKind.PIR:
        decision_context, review_by = "", None
    req = Requirement(
        stakeholder_id=stakeholder_id,
        title=title,
        description=description,
        intel_level=intel_level,
        priority=priority,
        kind=kind,
        decision_context=decision_context,
        review_by=review_by,
    )
    session.add(req)
    session.commit()
    session.refresh(req)
    return req


def _requirements_by_id(session: Session, ids: list[int]) -> list[Requirement]:
    if not ids:
        return []
    return list(
        session.exec(
            select(Requirement).where(col(Requirement.id).in_(ids))
        ).all()
    )


def set_report_requirements(
    session: Session, report: Report, requirement_ids: list[int]
) -> list[Requirement]:
    """Replace the set of requirements a report satisfies."""
    report.requirements = _requirements_by_id(session, requirement_ids)
    report.updated_at = utcnow()
    session.add(report)
    session.commit()
    session.refresh(report)
    return list(report.requirements)


def set_notebook_requirements(
    session: Session, notebook: Notebook, requirement_ids: list[int]
) -> list[Requirement]:
    """Replace the set of requirements a notebook addresses."""
    notebook.requirements = _requirements_by_id(session, requirement_ids)
    notebook.updated_at = utcnow()
    session.add(notebook)
    session.commit()
    session.refresh(notebook)
    return list(notebook.requirements)


def set_status(
    session: Session, requirement: Requirement, status: RequirementStatus
) -> Requirement:
    requirement.status = RequirementStatus(status)
    requirement.updated_at = utcnow()
    session.add(requirement)
    session.commit()
    session.refresh(requirement)
    return requirement


def stakeholder_requirement_summary(requirement: Requirement) -> dict:
    """Serialize a stakeholder-owned requirement without directory metadata."""

    return StakeholderRequirementResponse(
        id=requirement.id,
        title=requirement.title,
        description=requirement.description,
        intel_level=requirement.intel_level,
        priority=requirement.priority,
        kind=requirement.kind,
        decision_context=requirement.decision_context,
        review_by=requirement.review_by,
        status=requirement.status,
        created_at=requirement.created_at,
        updated_at=requirement.updated_at,
    ).model_dump()


def stakeholder_traceability(requirement: Requirement, stakeholder) -> dict:
    """Build a stakeholder-safe requirement response shared by API and portal.

    Notebook links are collection-only and are omitted.  Each linked report is
    individually visibility-checked, so a draft or a product hidden by audience
    scope cannot leak title, body, or relationship metadata through tasking.
    """

    reports: list[dict] = []
    for report in requirement.reports:
        try:
            reports.append(report_service.report_summary(
                report_service.ensure_visible(report, stakeholder)
            ))
        except HTTPException:
            continue
    return {
        "requirement": stakeholder_requirement_summary(requirement),
        "stakeholder": StakeholderIdentityResponse(
            id=stakeholder.id,
            display_name=stakeholder.display_name,
        ).model_dump(),
        "reports": reports,
    }


def stakeholder_report_requirements(report: Report, stakeholder) -> list[dict]:
    """Return only the current stakeholder's own traceability links."""

    return [
        stakeholder_requirement_summary(requirement)
        for requirement in report.requirements
        if requirement.stakeholder_id == stakeholder.id
    ]


def pir_coverage(session: Session) -> dict:
    """PIR collection-coverage/gap aggregation for the tasking board.

    Considers only **active** (OPEN / IN_PROGRESS) PIRs — a SATISFIED PIR's gap
    is moot and a CLOSED one is no longer collected against, so surfacing either
    would be false "act now" noise. Returns the uncovered PIRs (no linked report
    *and* no linked notebook — a real collection gap, via the existing
    traceability relationships) and the overdue PIRs (past ``review_by``).
    """
    active = {RequirementStatus.OPEN, RequirementStatus.IN_PROGRESS}
    pirs = list(
        session.exec(
            select(Requirement).where(
                Requirement.kind == RequirementKind.PIR,
                col(Requirement.status).in_(active),
            )
        ).all()
    )
    today = date.today()
    gaps = [r for r in pirs if not r.reports and not r.notebooks]
    overdue = [r for r in pirs if r.review_by and r.review_by < today]
    return {"gaps": gaps, "overdue": overdue, "total_active": len(pirs)}
