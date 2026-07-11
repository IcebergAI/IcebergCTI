"""Fail-closed audience-group and report-scope mutations.

Both the JSON API and the portal use these helpers so an invalid group id can
never silently turn a restricted product into an unscoped one.
"""

from typing import TYPE_CHECKING

from fastapi import HTTPException, status
from sqlmodel import Session, col, select

from ..models import AudienceGroup, AuditCategory, Report, ReportAudienceGroup, User
from . import audit

if TYPE_CHECKING:  # avoid importing framework types at runtime for services
    from fastapi import BackgroundTasks
    from starlette.requests import Request


def resolve_groups(session: Session, group_ids: list[int]) -> list[AudienceGroup]:
    """Resolve every requested group before a report relationship is changed.

    An empty list is a deliberate unscoping operation.  Any unknown value makes
    the whole request invalid; no partial set is ever returned to a caller that
    could then accidentally broaden a report's visibility.
    """

    requested = list(dict.fromkeys(group_ids))
    if not requested:
        return []
    groups = list(
        session.exec(
            select(AudienceGroup).where(col(AudienceGroup.id).in_(requested))
        ).all()
    )
    by_id = {group.id: group for group in groups}
    if any(group_id not in by_id for group_id in requested):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "One or more audience groups do not exist",
        )
    return [by_id[group_id] for group_id in requested]


def set_report_audience(
    session: Session,
    report: Report,
    group_ids: list[int],
    *,
    actor: User | None = None,
    request: "Request | None" = None,
    background_tasks: "BackgroundTasks | None" = None,
) -> list[AudienceGroup]:
    """Atomically replace a report's audience scope and audit success.

    ``[]`` intentionally remains supported for an administrator who wants to
    make a report broadly visible.  Invalid non-empty input raises before the
    existing relationship is touched.
    """

    groups = resolve_groups(session, group_ids)
    report.audience_groups = groups
    session.add(report)
    session.commit()
    session.refresh(report)

    if actor is not None:
        audit.record_and_emit(
            session,
            background_tasks=background_tasks,
            action="AUDIENCE_SCOPE_UPDATED",
            category=AuditCategory.ADMIN,
            actor=actor,
            request=request,
            resource_type="report",
            resource_id=report.id,
            detail={
                "group_ids": [group.id for group in groups],
                "unscoped": not groups,
            },
        )
        # The audit commit expires relationship state before FastAPI serialises
        # the response, so reload the scalar relationship while the session is
        # still alive.
        session.refresh(report)
    return list(report.audience_groups)


def delete_group(session: Session, group: AudienceGroup) -> None:
    """Delete an unreferenced group, preserving any report scope on conflict."""

    referenced = session.exec(
        select(ReportAudienceGroup.report_id)
        .where(ReportAudienceGroup.group_id == group.id)
        .limit(1)
    ).first()
    if referenced is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Audience group is assigned to one or more reports",
        )
    session.delete(group)
    session.commit()
