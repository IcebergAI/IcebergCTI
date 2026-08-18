"""Isolated, resettable demo workspaces built from ordinary Iceberg domain rows."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta, timezone
from typing import Any, cast

from fastapi import HTTPException, status
from sqlalchemy import update
from sqlalchemy.engine import CursorResult
from sqlmodel import Session, col, select

from .. import demo_content as content
from ..models import (
    AudienceGroup,
    DemoWorkspace,
    DisseminationEvent,
    IntelLevel,
    Notebook,
    NotebookRequirement,
    Priority,
    ProductFeedback,
    PublicationSnapshot,
    Report,
    ReportAudienceGroup,
    ReportRequirement,
    ReportSource,
    ReportStatus,
    Requirement,
    RequirementKind,
    RequirementStatus,
    Role,
    Source,
    SourceCredibility,
    SourceGradingOrigin,
    SourceReliability,
    TLP,
    User,
    UserAudienceGroup,
    utcnow,
)
from . import storage_deletions


@dataclass(frozen=True)
class StartedWorkspace:
    workspace: DemoWorkspace
    join_code: str


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _new_join_code() -> str:
    return secrets.token_urlsafe(24)


def _id(workspace: DemoWorkspace) -> int:
    if workspace.id is None:
        raise RuntimeError("Demo workspace has not been persisted")
    return workspace.id


def _expired(value) -> bool:
    # SQLite returns a naive value for timezone-aware DateTime columns; production
    # PostgreSQL preserves the offset. Treat persisted naive values as UTC.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value < utcnow()


def by_owner(session: Session, owner: User) -> DemoWorkspace | None:
    return session.exec(
        select(DemoWorkspace).where(DemoWorkspace.owner_id == owner.id)
    ).first()


def get(session: Session, public_id: str) -> DemoWorkspace:
    workspace = session.exec(
        select(DemoWorkspace).where(DemoWorkspace.public_id == public_id)
    ).first()
    if workspace is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Demo workspace not found")
    return workspace


def ensure_visible(workspace: DemoWorkspace, user: User) -> None:
    if user.role == Role.ADMIN or user.id in {
        workspace.owner_id,
        workspace.stakeholder_id,
    }:
        return
    if user.role == Role.REVIEWER:
        return
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Demo workspace not available")


def ensure_not_reset_managed(row: object) -> None:
    """Keep marked demo roots intact so only the isolated reset can replace them."""

    if getattr(row, "demo_workspace_id", None) is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Use the demo workspace reset to replace synthetic demo records",
        )


def start(session: Session, owner: User) -> StartedWorkspace:
    if owner.role not in {Role.ANALYST, Role.ADMIN}:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "An analyst or administrator must start the demo workspace",
        )
    existing = by_owner(session, owner)
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This account already owns a demo workspace",
        )
    code = _new_join_code()
    workspace = DemoWorkspace(
        public_id=secrets.token_urlsafe(18),
        owner_id=owner.id,
        join_code_digest=_digest(code),
        join_expires_at=utcnow() + timedelta(hours=24),
        scenario_version=content.SCENARIO_VERSION,
    )
    session.add(workspace)
    session.commit()
    session.refresh(workspace)
    return StartedWorkspace(workspace, code)


def join(
    session: Session,
    *,
    public_id: str,
    join_code: str,
    stakeholder: User,
    background_tasks=None,
) -> DemoWorkspace:
    if stakeholder.role != Role.STAKEHOLDER:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only a stakeholder can join a demo workspace"
        )
    workspace = get(session, public_id)
    supplied = _digest(join_code.strip())
    if (
        _expired(workspace.join_expires_at)
        or not workspace.join_code_digest
        or not hmac.compare_digest(supplied, workspace.join_code_digest)
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Demo workspace not found")
    result = cast(
        CursorResult[Any],
        session.execute(
            update(DemoWorkspace)
            .where(
                col(DemoWorkspace.id) == _id(workspace),
                col(DemoWorkspace.stakeholder_id).is_(None),
                col(DemoWorkspace.join_code_digest) == workspace.join_code_digest,
            )
            .values(
                stakeholder_id=stakeholder.id,
                join_code_digest="",
                updated_at=utcnow(),
            )
            .execution_options(synchronize_session=False)
        ),
    )
    if result.rowcount != 1:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Demo workspace already joined")
    session.expire(workspace)
    session.refresh(workspace)
    _replace_scenario(session, workspace)
    session.commit()
    storage_deletions.schedule_worker(background_tasks)
    session.refresh(workspace)
    return workspace


def _roots(
    session: Session, workspace_id: int
) -> tuple[Notebook | None, Requirement | None, Report | None, AudienceGroup | None]:
    return (
        session.exec(
            select(Notebook).where(Notebook.demo_workspace_id == workspace_id)
        ).first(),
        session.exec(
            select(Requirement).where(Requirement.demo_workspace_id == workspace_id)
        ).first(),
        session.exec(
            select(Report).where(Report.demo_workspace_id == workspace_id)
        ).first(),
        session.exec(
            select(AudienceGroup).where(AudienceGroup.demo_workspace_id == workspace_id)
        ).first(),
    )


def _delete_scenario(session: Session, workspace: DemoWorkspace) -> None:
    """Drop the workspace-owned rows, tombstoning their objects in the same
    transaction so a reset can never strand or over-delete stored bytes.

    A demo object is disposable synthetic material, so its tombstone skips the
    operator backup grace window that protects real user uploads.
    """

    notebook, requirement, report, audience = _roots(session, _id(workspace))
    if notebook is not None:
        for attachment in notebook.attachments:
            storage_deletions.enqueue(session, attachment, "attachment", grace_seconds=0)
        for figure in notebook.figures:
            storage_deletions.enqueue(session, figure, "figure", grace_seconds=0)
    if report is not None:
        for rendered in report.rendered_products:
            storage_deletions.enqueue(session, rendered, "render", grace_seconds=0)
    # The notebook owns the report; explicit report deletion keeps ordering clear
    # for databases that defer relationship-cascade bookkeeping until flush.
    if report is not None:
        session.delete(report)
        session.flush()
    for row in (notebook, requirement, audience):
        if row is not None:
            session.delete(row)
    session.flush()


def _replace_scenario(session: Session, workspace: DemoWorkspace) -> None:
    if workspace.stakeholder_id is None:
        return
    _delete_scenario(session, workspace)
    notebook = Notebook(
        title=content.NOTEBOOK_TITLE,
        topic=content.NOTEBOOK_TOPIC,
        owner_id=workspace.owner_id,
        demo_workspace_id=workspace.id,
    )
    requirement = Requirement(
        stakeholder_id=workspace.stakeholder_id,
        demo_workspace_id=workspace.id,
        title=content.REQUIREMENT_TITLE,
        description=content.REQUIREMENT_DESCRIPTION,
        intel_level=IntelLevel.STRATEGIC,
        priority=Priority.HIGH,
        kind=RequirementKind.PIR,
        decision_context="Fictional continuity-planning decision",
    )
    audience = AudienceGroup(
        name=f"Synthetic demo audience · {workspace.public_id[:8]}",
        slug=f"demo-{workspace.public_id.lower()}",
        description=content.NOTICE,
        demo_workspace_id=workspace.id,
    )
    session.add(notebook)
    session.add(requirement)
    session.add(audience)
    session.flush()
    source = Source(
        notebook_id=notebook.id,
        title=content.SOURCE_TITLE,
        reference=content.SOURCE_REFERENCE,
        summary=content.SOURCE_SUMMARY,
        content_md=content.SOURCE_BODY,
        tlp=TLP.CLEAR,
        reliability=SourceReliability.A,
        credibility=SourceCredibility.PROBABLY_TRUE,
        grading_origin=SourceGradingOrigin.MANUAL,
        grading_rationale="Synthetic demo fixture",
    )
    report = Report(
        notebook_id=notebook.id,
        demo_workspace_id=workspace.id,
        title=content.REPORT_TITLE,
        body_md=content.REPORT_BODY,
        key_judgements=content.KEY_JUDGEMENTS,
        key_assumptions=content.KEY_ASSUMPTIONS,
        intelligence_gaps=content.INTELLIGENCE_GAPS,
        intel_level=IntelLevel.STRATEGIC,
        tlp=TLP.CLEAR,
        author_id=workspace.owner_id,
    )
    session.add(source)
    session.add(report)
    session.flush()
    session.add(
        NotebookRequirement(notebook_id=notebook.id, requirement_id=requirement.id)
    )
    session.add(ReportRequirement(report_id=report.id, requirement_id=requirement.id))
    session.add(ReportSource(report_id=report.id, source_id=source.id))
    session.add(ReportAudienceGroup(report_id=report.id, group_id=audience.id))
    session.add(
        UserAudienceGroup(user_id=workspace.stakeholder_id, group_id=audience.id)
    )
    session.flush()


def reset(
    session: Session,
    *,
    workspace: DemoWorkspace,
    actor: User,
    expected_generation: int,
    background_tasks=None,
) -> DemoWorkspace:
    if actor.role != Role.ADMIN and actor.id != workspace.owner_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only the demo owner can reset it"
        )
    result = cast(
        CursorResult[Any],
        session.execute(
            update(DemoWorkspace)
            .where(
                col(DemoWorkspace.id) == _id(workspace),
                col(DemoWorkspace.generation) == expected_generation,
            )
            .values(
                generation=expected_generation + 1,
                scenario_version=content.SCENARIO_VERSION,
                updated_at=utcnow(),
            )
            .execution_options(synchronize_session=False)
        ),
    )
    if result.rowcount != 1:
        session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Demo workspace changed; reload and retry"
        )
    session.expire(workspace)
    session.refresh(workspace)
    _replace_scenario(session, workspace)
    session.commit()
    storage_deletions.schedule_worker(background_tasks)
    session.refresh(workspace)
    return workspace


def progress(session: Session, workspace: DemoWorkspace) -> dict[str, object]:
    notebook, requirement, report, _audience = _roots(session, _id(workspace))
    snapshot = None
    delivered = False
    feedback = False
    if report is not None:
        snapshot = session.exec(
            select(PublicationSnapshot).where(
                PublicationSnapshot.report_id == report.id
            )
        ).first()
        if workspace.stakeholder_id is not None:
            delivered = (
                session.exec(
                    select(DisseminationEvent).where(
                        DisseminationEvent.report_id == report.id,
                        DisseminationEvent.stakeholder_id == workspace.stakeholder_id,
                    )
                ).first()
                is not None
            )
            feedback = (
                session.exec(
                    select(ProductFeedback).where(
                        ProductFeedback.report_id == report.id,
                        ProductFeedback.stakeholder_id == workspace.stakeholder_id,
                    )
                ).first()
                is not None
            )
    report_status = ReportStatus(report.status) if report is not None else None
    published = (
        report is not None
        and report_status == ReportStatus.PUBLISHED
        and snapshot is not None
        and bool(report.publication_snapshot_hash)
    )
    return {
        "joined": workspace.stakeholder_id is not None,
        "notebook": notebook,
        "requirement": requirement,
        "report": report,
        "requirement_started": requirement is not None
        and requirement.status != RequirementStatus.OPEN,
        "submitted": report_status
        in {ReportStatus.IN_REVIEW, ReportStatus.APPROVED, ReportStatus.PUBLISHED},
        "approved": report_status in {ReportStatus.APPROVED, ReportStatus.PUBLISHED},
        "published": published,
        "delivered": delivered,
        "feedback": feedback,
        # Any persisted stakeholder feedback closes the representative demo
        # cycle. A truthful NOT_MET/PARTIALLY_MET verdict must not trap a user
        # in an artificial tutorial state; requirement satisfaction remains an
        # ordinary product outcome shown on its own screen.
        "complete": feedback and published,
    }
