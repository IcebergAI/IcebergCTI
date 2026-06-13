"""Requirement linking and status helpers (traceability + analyst tasking)."""

from sqlmodel import Session, col, select

from ..models import (
    IntelLevel,
    Notebook,
    Priority,
    Report,
    Requirement,
    RequirementStatus,
    utcnow,
)


def create_requirement(
    session: Session,
    *,
    stakeholder_id: int,
    title: str,
    description: str = "",
    intel_level: IntelLevel = IntelLevel.STRATEGIC,
    priority: Priority = Priority.MEDIUM,
) -> Requirement:
    """Create a stakeholder requirement. Shared by the JSON API and the portal."""
    req = Requirement(
        stakeholder_id=stakeholder_id,
        title=title,
        description=description,
        intel_level=intel_level,
        priority=priority,
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
